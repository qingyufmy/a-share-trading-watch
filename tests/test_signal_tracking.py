import sqlite3
import unittest

from core import opportunity_rating, signal_tracking
import realtime_signal_engine as engine


def signal(scenario="V2_WAIT", allowed=False, blockers=None, price=10.0, updated="2026-08-25 10:00:00"):
    return {
        "trading_date": "2026-08-25",
        "symbol": "002821",
        "name": "凯莱英",
        "scenario": scenario,
        "external_status": "立即处理" if allowed else "观察",
        "current_price": price,
        "trigger_price": price if allowed else None,
        "updated_at": updated,
        "timing_v2": {
            "entry_allowed": allowed,
            "regime": "TREND",
            "location": "TIER1_SUPPORT",
            "setup_15m": "E2_STRUCTURAL_SUPPORT_REVERSAL" if allowed else "WAIT",
            "execution_5m": "VWAP_RECLAIM" if allowed else "WAIT",
            "path_state": "NORMAL",
            "extension_state": "NORMAL",
            "blockers": blockers or ["15分钟Setup未完成", "5分钟未形成抬高低点"],
            "data_quality": {"closed_5m_bars": 12, "closed_15m_bars": 30, "minute_available": True, "market_data": {"entry_ready": True}},
            "execution_gates": {"volume_confirmed": allowed, "vwap_distance_ok": allowed},
            "room_risk": {"reward_risk": 2.0, "room_atr": 1.5},
            "source_bar_close": {"5m": "10:00:00"},
            "config_hash": "cfg",
        },
    }


class SignalTrackingTests(unittest.TestCase):
    def test_late_candidate_without_formal_entry_is_not_a_missed_order(self):
        signal = {
            "trading_date": "2026-08-27", "symbol": "002916", "scenario": "V2_WAIT",
            "external_status": "观察", "current_price": 341.0, "updated_at": "2026-08-27 14:50:00",
            "timing_v2": {
                "trigger_missed": True, "trigger_missed_reason": "14:45后不新开仓",
                "candidate_entry_pattern": "V2_E5_LEADER_SECOND_LEG", "setup_15m": "E5_LEADER_SECOND_LEG",
                "data_quality": {"minute_available": True, "closed_5m_bars": 48, "closed_15m_bars": 36},
            },
        }
        rating = opportunity_rating.rate_signal(signal)
        self.assertNotEqual(signal_tracking.state_for(signal, rating), "TRIGGERED_BUT_MISSED")

    def setUp(self):
        self.con = sqlite3.connect(":memory:")
        signal_tracking.ensure_schema(self.con)

    def tearDown(self):
        self.con.close()

    def test_wait_near_trigger_is_one_track(self):
        wait = signal()
        rating = opportunity_rating.rate_signal(wait)
        signal_tracking.upsert_track(self.con, wait, rating, ["SETUP_FORMING"])
        near = signal(blockers=["5分钟未形成抬高低点"], updated="2026-08-25 10:05:00")
        near_rating = opportunity_rating.rate_signal(near)
        signal_tracking.upsert_track(self.con, near, near_rating, ["NEAR_TRIGGER"])
        fired = signal("STRATEGY_520_ENTRY", True, [], 10.1, "2026-08-25 10:10:00")
        fired_rating = opportunity_rating.rate_signal(fired)
        signal_tracking.upsert_track(self.con, fired, fired_rating, ["TRIGGERED"])
        tracks = self.con.execute("SELECT COUNT(*), MIN(track_id), MAX(track_id), state FROM signal_tracks").fetchone()
        self.assertEqual(tracks[0], 1)
        self.assertEqual(tracks[1], tracks[2])
        self.assertEqual(tracks[3], "TRIGGERED")

    def test_scenario_change_does_not_restart_from_idle(self):
        first = signal()
        rating = opportunity_rating.rate_signal(first)
        signal_tracking.upsert_track(self.con, first, rating, ["SETUP_FORMING"])
        fired = signal("STRATEGY_520_ENTRY", True, [], 10.1, "2026-08-25 10:10:00")
        fired_rating = opportunity_rating.rate_signal(fired)
        events = signal_tracking.upsert_track(self.con, fired, fired_rating, ["TRIGGERED"])
        self.assertEqual(events[-1]["from"], "SETUP_FORMING")
        self.assertEqual(events[-1]["to"], "TRIGGERED")

    def test_price_jitter_does_not_duplicate_material_event(self):
        first = signal(price=10.0)
        rating = opportunity_rating.rate_signal(first)
        signal_tracking.upsert_track(self.con, first, rating, ["SETUP_FORMING"])
        jitter = signal(price=10.01, updated="2026-08-25 10:01:00")
        events = signal_tracking.upsert_track(self.con, jitter, opportunity_rating.rate_signal(jitter), ["SETUP_FORMING"])
        self.assertEqual(events, [])
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM signal_track_events").fetchone()[0], 1)

    def test_data_block_recovers_to_setup_forming(self):
        blocked = signal(blockers=["已收盘5m/15m不足：0/3、0/20"])
        blocked["timing_v2"]["data_quality"].update({"closed_5m_bars": 0, "closed_15m_bars": 0, "minute_available": False})
        blocked_rating = opportunity_rating.rate_signal(blocked)
        self.assertEqual(signal_tracking.state_for(blocked, blocked_rating), "DATA_BLOCKED")
        signal_tracking.upsert_track(self.con, blocked, blocked_rating, ["DATA_BLOCKED"])
        recovered = signal(updated="2026-08-25 10:15:00")
        recovered_rating = opportunity_rating.rate_signal(recovered)
        events = signal_tracking.upsert_track(self.con, recovered, recovered_rating, ["SETUP_FORMING"])
        self.assertEqual(events[-1]["from"], "DATA_BLOCKED")

    def test_trigger_records_order_sequence_without_scenario_restart(self):
        fired = signal("STRATEGY_520_ENTRY", True, [], 10.1, "2026-08-25 10:10:00")
        rating = opportunity_rating.rate_signal(fired)
        states = signal_tracking.states_for_tick(fired, rating, {"status": "UNFILLED"})
        events = signal_tracking.upsert_track(self.con, fired, rating, states)
        self.assertEqual([item["to"] for item in events], ["TRIGGERED", "ORDER_PENDING", "UNFILLED"])

    def test_retrigger_after_wait_is_an_immediate_event_but_stable_trigger_is_not(self):
        self.assertTrue(engine.formal_entry_retriggered([
            {"from": "NEAR_TRIGGER", "to": "TRIGGERED"}
        ]))
        self.assertFalse(engine.formal_entry_retriggered([
            {"from": "TRIGGERED", "to": "TRIGGERED"}
        ]))

    def test_retired_raw_e7_is_not_tracked_as_triggered(self):
        fired = signal("V2_E7_FLAT_BASE_BREAKOUT", True, [], 10.1, "2026-08-25 10:10:00")
        rating = opportunity_rating.rate_signal(fired)
        self.assertNotEqual(signal_tracking.state_for(fired, rating), "TRIGGERED")


if __name__ == "__main__":
    unittest.main()
