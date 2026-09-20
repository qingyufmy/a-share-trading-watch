from datetime import datetime, timedelta
import unittest

import realtime_signal_engine as engine
from core import observation_strategy_router


def candidate(**overrides):
    signal = {
        "trading_date": "2026-09-03",
        "symbol": "000001",
        "name": "临界候选",
        "scenario": "V2_WAIT",
        "priority": "P2",
        "external_status": "观察",
        "current_price": 10.00,
        "distance_pct": 0.0,
        "opportunity_grade": "B",
        "opportunity_score": 76,
        "strategy_key": observation_strategy_router.TREND_MA5,
        "strategy_name": "趋势5日线法则",
        "strategy_daily_qualified": True,
        "strategy_gate_ok": True,
        "premarket_plan_allows_entry": True,
        "premarket_plan_complete": True,
        "sector_resonance_ok": True,
        "sector_resonance_reason": "板块共振通过：测试板块 1.20% + 连续刷新 + 领涨承接",
        "strategy_contract": {
            "key": observation_strategy_router.TREND_MA5,
            "name": "趋势5日线法则",
            "is_observation_strategy": True,
            "allowed_patterns": ["V2_E1_TREND_PULLBACK_RECLAIM", "V2_E4_BREAKOUT_RETEST"],
        },
        "tracking": {"state": "NEAR_TRIGGER"},
        "timing_v2": {
            "regime": "TREND",
            "location": "TIER1_SUPPORT",
            "setup_15m": "E1_TREND_PULLBACK_RECLAIM",
            "candidate_entry_pattern": "V2_E1_TREND_PULLBACK_RECLAIM",
            "execution_5m": "VWAP_HOLD",
            "blockers": ["5分钟未形成抬高低点"],
            "blocker_diagnostics": [
                {"reason": "5分钟未形成抬高低点", "severity": "TEMPORARY", "retryable": True}
            ],
        },
    }
    signal.update(overrides)
    return signal


class CriticalWatchTests(unittest.TestCase):
    def test_only_authorized_near_ready_strategy_waits_are_candidates(self):
        eligible = candidate()
        unclassified = candidate(
            symbol="000002",
            strategy_key=observation_strategy_router.OBSERVE,
            strategy_daily_qualified=False,
            strategy_contract={
                "key": observation_strategy_router.OBSERVE,
                "name": "待分类观察",
                "is_observation_strategy": True,
            },
        )
        no_premarket_authorization = candidate(
            symbol="000003",
            premarket_plan_allows_entry=False,
        )
        hard_block = candidate(
            symbol="000004",
            timing_v2={
                "regime": "BEAR",
                "location": "MID_AIR",
                "setup_15m": "WAIT",
                "execution_5m": "VWAP_HOLD",
                "blockers": ["120分钟BEAR，不允许新开多"],
                "blocker_diagnostics": [
                    {"reason": "120分钟BEAR，不允许新开多", "severity": "HARD", "retryable": False}
                ],
            },
        )

        candidates = engine.critical_watch_candidates(
            [eligible, unclassified, no_premarket_authorization, hard_block]
        )

        self.assertEqual([item["symbol"] for item in candidates], ["000001"])

    def test_candidate_requires_persistence_then_respects_symbol_cooldown(self):
        signal = candidate()
        now = datetime(2026, 9, 3, 10, 0)
        selected, state = engine.critical_watch_to_send([signal], {}, now)
        self.assertEqual(selected, [])

        selected, state = engine.critical_watch_to_send(
            [signal], state, now + timedelta(seconds=engine.CRITICAL_WATCH_MIN_PERSISTENCE_SECONDS + 1)
        )
        self.assertEqual(selected, [signal])

        state["candidates"][signal["symbol"]] = {
            "fingerprint": engine.critical_watch_fingerprint(signal),
            "last_sent_at": now.strftime("%Y-%m-%d %H:%M:%S"),
            "first_eligible_at": now.strftime("%Y-%m-%d %H:%M:%S"),
            "active": True,
        }
        selected, _ = engine.critical_watch_to_send([signal], state, now + timedelta(minutes=5))
        self.assertEqual(selected, [])

        changed = candidate()
        changed["opportunity_grade"] = "A"
        changed["timing_v2"] = {
            **changed["timing_v2"],
            "blockers": ["5分钟已收盘VWAP收复未完成"],
            "blocker_diagnostics": [
                {"reason": "5分钟已收盘VWAP收复未完成", "severity": "TEMPORARY", "retryable": True}
            ],
        }
        selected, _ = engine.critical_watch_to_send([changed], state, now + timedelta(minutes=5))
        self.assertEqual(selected, [])

        selected, state = engine.critical_watch_to_send([changed], state, now + timedelta(minutes=16))
        self.assertEqual(selected, [changed])

    def test_global_cooldown_batches_different_symbols(self):
        now = datetime(2026, 9, 3, 10, 0)
        first = candidate()
        _, state = engine.critical_watch_to_send([first], {}, now)
        _, state = engine.critical_watch_to_send(
            [first], state, now + timedelta(seconds=engine.CRITICAL_WATCH_MIN_PERSISTENCE_SECONDS + 1)
        )
        state["last_global_sent_at"] = (now + timedelta(minutes=1)).strftime("%Y-%m-%d %H:%M:%S")
        second = candidate(symbol="000002", name="第二只候选")
        _, state = engine.critical_watch_to_send([second], state, now + timedelta(minutes=2))
        selected, _ = engine.critical_watch_to_send([second], state, now + timedelta(minutes=4))
        self.assertEqual(selected, [])

    def test_daily_or_strategy_gap_is_not_interruptive_alert(self):
        weekly = candidate()
        weekly["timing_v2"] = {
            **weekly["timing_v2"],
            "blockers": ["周线闭合确认未完成"],
            "blocker_diagnostics": [
                {"reason": "周线闭合确认未完成", "severity": "TEMPORARY", "retryable": True}
            ],
        }
        self.assertEqual(engine.critical_watch_candidates([weekly]), [])

    def test_score_jitter_does_not_change_alert_fingerprint(self):
        low = candidate(opportunity_score=75)
        high = candidate(opportunity_score=79)
        self.assertEqual(
            engine.critical_watch_fingerprint(low),
            engine.critical_watch_fingerprint(high),
        )

    def test_watch_text_is_explicitly_not_an_order(self):
        rendered = engine.critical_watch_signal_brief(candidate())

        self.assertIn("趋势5日线法则", rendered)
        self.assertIn("趋势5日线法则日线资格", rendered)
        self.assertIn("盘中板块共振", rendered)
        self.assertIn("下一关", rendered)
        self.assertIn("尚未生成订单", rendered)
        self.assertIn("绿色正式买入信号", rendered)

    def test_legacy_core_watch_does_not_claim_named_daily_strategy_qualification(self):
        core = candidate(
            strategy_key=observation_strategy_router.CORE_V2,
            strategy_name="核心池V2路径",
            strategy_contract={
                "key": observation_strategy_router.CORE_V2,
                "name": "核心池V2路径",
                "is_observation_strategy": False,
            },
        )
        rendered = engine.critical_watch_signal_brief(core)
        self.assertIn("核心池盘前计划", rendered)
        self.assertIn("120m TREND结构", rendered)
        self.assertNotIn("日线资格", rendered)


if __name__ == "__main__":
    unittest.main()
