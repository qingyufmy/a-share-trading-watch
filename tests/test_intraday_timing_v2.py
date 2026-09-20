import unittest
from datetime import datetime, timedelta
from tempfile import TemporaryDirectory
from unittest.mock import patch

from core import intraday_timing_v2 as timing
import paper_trading


def minutes(start, count, prices, volumes=None):
    rows = []
    volumes = volumes or [1000] * count
    base = datetime(2026, 8, 13, 9, 30)
    for index in range(count):
        point = base.replace(minute=30 + index)
        rows.append({"m": point.strftime("%H:%M:%S"), "p": prices[index], "v": volumes[index]})
    return rows


def row(price=10.1, high=10.2, low=9.95):
    return {
        "state": "强趋势延续",
        "defense": 10.0,
        "quote": {"close": price, "open": 10.0, "prev_close": 10.0, "high": high, "low": low},
        "rt_features": {"vwap": 10.02, "amount_ratio_1m": 1.8, "amount_ratio_5m": 1.8, "atr5m": 0.05},
    }


def history_120m():
    return [
        {"open": 9.0 + index * 0.01, "high": 9.1 + index * 0.01, "low": 8.9 + index * 0.01, "close": 9.0 + index * 0.01, "volume": 1000}
        for index in range(240)
    ]


def repair_history_120m():
    bars = [
        {"open": 12.0, "high": 12.1, "low": 11.9, "close": 12.0, "volume": 1000}
        for _ in range(200)
    ]
    bars.extend(
        {"open": 8.0 + index * 0.05, "high": 8.1 + index * 0.05, "low": 7.9 + index * 0.05,
         "close": 8.0 + index * 0.05, "volume": 1000}
        for index in range(40)
    )
    return bars


class IntradayTimingV2Tests(unittest.TestCase):
    def test_e5b_accepts_first_closed_bar_only_for_qualified_leader(self):
        subject = row(price=10.35, high=10.40, low=10.18)
        subject["pressure"] = 10.30
        subject["quote"].update({
            "open": 10.20, "prev_close": 10.0, "pct": 3.5,
            "amount_wan": 20000, "turnover": 1.0,
        })
        subject["sector_momentum"] = {"emotion_ok": True, "board_name": "液冷", "board_pct": 2.0}
        subject["sector_rotation"] = {"sustained": True, "leader_healthy": True}
        subject["strategy_contract"] = {
            "key": "LEADER_EMOTION", "is_observation_strategy": True,
            "daily_qualified": True,
            "allowed_patterns": ["V2_E5B_LEADER_OPENING_HOLD", "V2_E5_LEADER_SECOND_LEG"],
        }
        detail = timing._leader_opening_hold_state(
            subject,
            [{
                "open": 10.20, "high": 10.40, "low": 10.18, "close": 10.35,
                "volume": 10000, "bar_end": "2026-09-04 09:34:00",
            }],
            10.25,
            0.10,
            10.35,
            "TREND",
            "GAP_HOLD",
            datetime(2026, 9, 4, 9, 35),
            timing.load_config(),
        )
        self.assertTrue(detail["eligible"], detail)
        self.assertTrue(detail["opening_hold"])
        self.assertGreaterEqual(detail["projected_amount_yi"], 8.0)
        self.assertEqual(detail["target"], 11.0)

    def test_15m_setup_is_retained_while_5m_trigger_is_still_waiting(self):
        bars15 = [
            {
                "open": 10.0,
                "high": 10.10,
                "low": 9.98,
                "close": 10.05,
                "volume": 1000,
            }
            for _ in range(20)
        ]
        bars15[-2]["volume"] = 1200
        bars15[-1].update({"low": 9.99, "close": 10.08, "volume": 900})
        setup, blockers = timing._setup_state(
            "REPAIR",
            "NORMAL",
            "NORMAL",
            [],
            bars15,
            {"ma5_15": 10.04, "tier2_ma20": 9.95, "tier1_support": 10.0},
            10.08,
            False,
            False,
            0.10,
            timing.load_config(),
        )
        self.assertEqual(setup, "E2_STRUCTURAL_SUPPORT_REVERSAL")
        self.assertEqual(blockers, [])

    def test_named_strategy_cannot_be_preempted_by_an_unowned_setup(self):
        bars15 = [
            {"open": 10.0, "high": 10.10, "low": 9.98, "close": 10.05, "volume": 1000}
            for _ in range(20)
        ]
        setup, blockers = timing._setup_state(
            "REPAIR",
            "NORMAL",
            "NORMAL",
            [],
            bars15,
            {"ma5_15": 10.04, "tier2_ma20": 9.95, "tier1_support": 10.0},
            10.05,
            True,
            True,
            0.10,
            timing.load_config(),
            {"V2_E3_MA20_STRUCTURAL_RECLAIM"},
        )
        self.assertEqual(setup, "WAIT")
        self.assertIn("不属于当前方法允许路径", blockers[0])

    def test_retired_e6_cannot_create_a_new_entry(self):
        bars5 = [
            {"open": 10.36, "high": 10.42, "low": 10.34, "close": 10.41, "volume": 1000, "bar_end": "2026-08-27 10:19:00"},
            {"open": 10.40, "high": 10.45, "low": 10.36, "close": 10.44, "volume": 1100, "bar_end": "2026-08-27 10:24:00"},
            {"open": 10.43, "high": 10.50, "low": 10.39, "close": 10.48, "volume": 1400, "bar_end": "2026-08-27 10:29:00"},
        ]
        intraday15 = [
            {"open": 10.00, "high": 10.35, "low": 9.98, "close": 10.30, "volume": 3000, "bar_end": "2026-08-27 09:44:00"},
            {"open": 10.30, "high": 10.60, "low": 10.25, "close": 10.55, "volume": 4000, "bar_end": "2026-08-27 09:59:00"},
            {"open": 10.54, "high": 10.45, "low": 10.28, "close": 10.34, "volume": 2200, "bar_end": "2026-08-27 10:14:00"},
            {"open": 10.34, "high": 10.50, "low": 10.32, "close": 10.48, "volume": 2300, "bar_end": "2026-08-27 10:29:00"},
        ]
        prior15 = [
            {"open": 10.4, "high": 10.48, "low": 10.32, "close": 10.4, "volume": 1000, "bar_end": f"2026-08-26 {9 + index // 4:02d}:{(index % 4) * 15:02d}:00"}
            for index in range(24)
        ]
        subject = row(price=10.48, high=10.60, low=9.98)
        subject.update({"pressure": 11.50})
        subject["quote"].update({"pct": 3.0, "amount_wan": 90000})
        subject["rt_features"].update({"vwap": 10.38, "atr5m": 0.10, "amount_ratio_1m": 1.4, "amount_ratio_5m": 1.3})
        subject["sector_rotation"] = {"sustained": True, "leader_healthy": True}
        subject["sector_momentum"] = {"emotion_ok": True, "board_name": "测试板块", "board_pct": 2.1}
        market = {"entry_ready": True, "a_share_market_regime": {"state": "growth_lead"}}
        with patch.object(timing, "closed_bars", side_effect=[bars5, intraday15]):
            decision = timing.evaluate(
                subject, [{"m": "10:30:00", "p": 10.48, "v": 100}],
                now=datetime(2026, 8, 27, 10, 30), history_120m=repair_history_120m(),
                history_15m=prior15, market_data=market,
            )
        self.assertFalse(decision["entry_allowed"], decision)
        self.assertIsNone(decision["entry_pattern"])
        self.assertEqual(decision['contract_readiness']['status'], 'CONTRACT_MISSING')
        self.assertIn("缺少盘前策略合同", "；".join(decision["blockers"]))

    def test_lunch_is_not_resampled_into_a_tradable_bar(self):
        rows = [
            {"m": "11:29:00", "p": 10, "v": 1},
            {"m": "13:00:00", "p": 10.1, "v": 1},
            {"m": "13:01:00", "p": 10.2, "v": 1},
        ]
        bars = timing.closed_bars(rows, 5, datetime(2026, 8, 13, 13, 10))
        self.assertEqual(bars, [])

    def test_future_minute_rows_are_not_used_after_a_restart(self):
        rows = [
            {"m": "09:30:00", "p": 10, "v": 1},
            {"m": "09:31:00", "p": 10.1, "v": 1},
            {"m": "09:32:00", "p": 10.2, "v": 1},
            {"m": "09:33:00", "p": 10.3, "v": 1},
            {"m": "09:34:00", "p": 10.4, "v": 1},
            {"m": "09:35:00", "p": 11.0, "v": 1},
        ]
        bars = timing.closed_bars(rows, 5, datetime(2026, 8, 13, 9, 34))
        self.assertEqual(bars, [])

    def test_completed_bars_remain_available_at_lunch_and_after_close(self):
        rows = [
            {"m": f"09:3{index}:00", "p": 10 + index * 0.01, "v": 1}
            for index in range(5)
        ]
        lunch = timing.closed_bars(rows, 5, datetime(2026, 8, 13, 12, 0))
        closed = timing.closed_bars(rows, 5, datetime(2026, 8, 13, 15, 5))
        self.assertEqual(len(lunch), 1)
        self.assertEqual(len(closed), 1)

    def test_closed_bars_preserve_minute_ohlc(self):
        rows = [
            {"m": f"09:3{index}:00", "o": 10, "h": 10.5 + index, "l": 9.5 - index, "c": 10.1, "v": 1}
            for index in range(5)
        ]
        bars = timing.closed_bars(rows, 5, datetime(2026, 8, 13, 9, 35))
        self.assertEqual(bars[0]["high"], 14.5)
        self.assertEqual(bars[0]["low"], 5.5)

    def test_duplicate_minute_cannot_hide_a_missing_slot(self):
        rows = [
            {"m": "09:30:00", "p": 10.0, "v": 1},
            {"m": "09:30:00", "p": 10.1, "v": 1},
            {"m": "09:31:00", "p": 10.1, "v": 1},
            {"m": "09:32:00", "p": 10.2, "v": 1},
            {"m": "09:34:00", "p": 10.4, "v": 1},
        ]
        bars = timing.closed_bars(rows, 5, datetime(2026, 8, 13, 9, 35))
        self.assertEqual(bars, [])

    def test_terminal_1500_quote_does_not_create_an_extra_bar(self):
        rows = []
        for start, count in ((datetime(2026, 8, 13, 9, 30), 120), (datetime(2026, 8, 13, 13, 0), 120)):
            rows.extend(
                {"m": (start + timedelta(minutes=index)).strftime("%H:%M:%S"), "p": 10, "v": 1}
                for index in range(count)
            )
        rows.append({"m": "15:00:00", "p": 10, "v": 1})
        self.assertEqual(len(timing.closed_bars(rows, 5, datetime(2026, 8, 13, 15, 1))), 48)
        self.assertEqual(len(timing.closed_bars(rows, 15, datetime(2026, 8, 13, 15, 1))), 16)

    def test_tencent_end_stamped_minutes_close_afternoon_bars(self):
        rows = [{"source": "tencent", "m": "09:30:00", "p": 4.12, "v": 1}]
        for start in (datetime(2026, 8, 28, 9, 31), datetime(2026, 8, 28, 13, 1)):
            rows.extend({
                "source": "tencent",
                "m": (start + timedelta(minutes=index)).strftime("%H:%M:%S"),
                "p": 4.2,
                "v": 1,
            } for index in range(120))
        bars5 = timing.closed_bars(rows, 5, datetime(2026, 8, 28, 15, 1))
        bars15 = timing.closed_bars(rows, 15, datetime(2026, 8, 28, 15, 1))
        self.assertEqual(len(bars5), 48)
        self.assertEqual(len(bars15), 16)
        self.assertEqual(bars15[-1]["close_time"], "15:00:00")
        self.assertEqual(bars15[-1]["timestamp_convention"], "end")

    def test_distribution_shock_is_a_hard_block(self):
        prices = [10.0 + index * 0.03 for index in range(20)] + [10.35, 10.2, 10.05, 9.98, 9.96]
        decision = timing.evaluate(row(price=9.96, high=10.6, low=9.94), minutes("09:30", 25, prices), now=datetime(2026, 8, 13, 10, 0))
        self.assertTrue(decision["path_hard_block"])
        self.assertFalse(decision["entry_allowed"])
        self.assertEqual(decision["path_state"], "DISTRIBUTION_SHOCK")

    def test_missing_120m_and_15m_history_fails_closed(self):
        prices = [9.98, 9.99, 10.00, 10.01, 10.02, 10.00, 10.01, 10.02, 10.03, 10.04, 10.02, 10.03, 10.04, 10.05, 10.08]
        subject = row(price=10.08, high=10.10, low=9.98)
        decision = timing.evaluate(subject, minutes("09:30", 15, prices), now=datetime(2026, 8, 13, 10, 0))
        self.assertEqual(decision["mode"], "EXECUTION_GUARD")
        self.assertFalse(decision["entry_allowed"], decision)
        self.assertIn("120分钟历史不足", "；".join(decision["blockers"] + decision["reasons"]))
        self.assertIn("已收盘5m/15m不足", "；".join(decision["blockers"]))

    def test_mid_air_blocks_chasing_even_with_vwap_reclaim(self):
        prices = [10.45 + index * 0.01 for index in range(15)]
        decision = timing.evaluate(row(price=10.59, high=10.60, low=10.45), minutes("09:30", 15, prices), now=datetime(2026, 8, 13, 10, 0))
        self.assertFalse(decision["entry_allowed"])
        self.assertEqual(decision["location"], "MID_AIR")
        self.assertIn("位置处于MID_AIR，非支撑/结构收复入场", decision["blockers"])

    def test_executor_refuses_a_signal_blocked_by_v2(self):
        signal = {
            "trading_date": "2026-08-13",
            "symbol": "000001",
            "scenario": "V2_E1_TREND_PULLBACK_RECLAIM",
            "timing_v2": {"entry_allowed": False, "blockers": ["路径硬否决：DISTRIBUTION_SHOCK"]},
        }
        with TemporaryDirectory() as tmp:
            result = paper_trading.maybe_execute_signal(tmp, signal, {}, datetime(2026, 8, 13, 10, 0))
        self.assertEqual(result["status"], "NO_ORDER")
        self.assertIn("不对应模拟下单场景", result["reason"])

    def test_executor_refuses_v2_default_position_when_plan_is_missing(self):
        signal = {
            "trading_date": "2026-08-13",
            "symbol": "000001",
            "scenario": "V2_E1_TREND_PULLBACK_RECLAIM",
            "strategy_family": "V2_ONLY",
            "current_price": 10.0,
            "position_multiplier": 0.33,
            "premarket_plan_allows_entry": True,
        }
        with TemporaryDirectory() as tmp:
            con = paper_trading.init_db(tmp)
            plan = paper_trading.buy_position_plan(con, signal, account={"cash": 1_000_000, "total_assets_estimate": 1_000_000})
        self.assertFalse(plan["passed"])
        self.assertIn("不能直接下单", plan["reason"])

    def test_full_120m_structure_and_data_degradation_are_applied(self):
        prices = [9.98, 9.99, 10.00, 10.01, 10.02, 10.00, 10.01, 10.02, 10.03, 10.04, 10.02, 10.03, 10.04, 10.05, 10.08]
        subject = row(price=10.08, high=10.10, low=9.98)
        subject["defense"] = 10.07
        full = timing.evaluate(subject, minutes("09:30", 15, prices), now=datetime(2026, 8, 13, 10, 0), history_120m=history_120m())
        self.assertEqual(full["mode"], "FULL_120M")
        blocked = timing.evaluate(
            subject,
            minutes("09:30", 15, prices),
            now=datetime(2026, 8, 13, 10, 0),
            history_120m=history_120m(),
            market_data={"entry_ready": False, "blockers": ["腾讯与新浪60分钟收盘交叉校验不一致"]},
        )
        self.assertFalse(blocked["entry_allowed"])
        self.assertIn("腾讯与新浪60分钟收盘交叉校验不一致", blocked["blockers"])

    def test_weak_volume_or_extended_vwap_stays_wait_inside_v2(self):
        prices = [10.00 + index * 0.01 for index in range(25)]
        subject = row(price=10.24, high=10.25, low=10.00)
        subject["rt_features"].update({"vwap": 10.02, "amount_ratio_1m": 0.74, "amount_ratio_5m": 0.88})
        with patch.object(timing, "_setup_state", return_value=("E2_STRUCTURAL_SUPPORT_REVERSAL", [])):
            decision = timing.evaluate(
                subject,
                minutes("09:30", 25, prices),
                now=datetime(2026, 8, 13, 10, 0),
                history_120m=history_120m(),
            )
        self.assertFalse(decision["entry_allowed"])
        blockers = "；".join(decision["blockers"])
        self.assertIn("量能未形成可执行确认", blockers)
        self.assertIn("模式either", blockers)
        self.assertIn("现价距VWAP过远", blockers)
        self.assertFalse(decision["execution_gates"]["volume_confirmed"])
        self.assertFalse(decision["execution_gates"]["vwap_distance_ok"])

    def test_provider_15m_history_without_close_time_keeps_engine_running(self):
        prices = [10.0 + index * 0.01 for index in range(25)]
        provider_history = [
            {
                "open": 9.8 + index * 0.01,
                "high": 9.9 + index * 0.01,
                "low": 9.7 + index * 0.01,
                "close": 9.8 + index * 0.01,
                "volume": 1000,
                "bar_end": f"2026-08-13 {9 + index // 4:02d}:{(index % 4) * 15:02d}:00",
            }
            for index in range(24)
        ]
        decision = timing.evaluate(
            row(price=10.24, high=10.26, low=9.98),
            minutes("09:30", 25, prices),
            now=datetime(2026, 8, 13, 10, 0),
            history_120m=history_120m(),
            history_15m=provider_history,
        )
        self.assertEqual(decision["source_bar_close"]["15m"], "09:44:00")
        self.assertEqual(decision["data_quality"]["intraday_closed_15m_bars"], 1)

    def test_e5_waits_for_pullback_then_accepts_liquid_sector_leader_resume(self):
        intraday15 = [
            {"open": 156.48, "high": 162.65, "low": 154.63, "close": 161.15, "volume": 41032, "bar_end": "2026-08-25 09:44:00"},
            {"open": 161.33, "high": 165.98, "low": 160.61, "close": 165.06, "volume": 30315, "bar_end": "2026-08-25 09:59:00"},
            {"open": 165.11, "high": 166.99, "low": 163.74, "close": 166.55, "volume": 19875, "bar_end": "2026-08-25 10:14:00"},
            {"open": 166.55, "high": 166.59, "low": 163.30, "close": 163.31, "volume": 11388, "bar_end": "2026-08-25 10:29:00"},
            {"open": 163.32, "high": 164.20, "low": 162.87, "close": 163.50, "volume": 6587, "bar_end": "2026-08-25 10:44:00"},
            {"open": 163.39, "high": 166.49, "low": 163.21, "close": 166.39, "volume": 13000, "bar_end": "2026-08-25 10:59:00"},
        ]
        bars5 = [
            {"open": 163.53, "high": 164.12, "low": 163.53, "close": 164.00, "volume": 1760, "bar_end": "2026-08-25 10:49:00"},
            {"open": 163.94, "high": 164.88, "low": 163.94, "close": 164.88, "volume": 1768, "bar_end": "2026-08-25 10:54:00"},
            {"open": 164.75, "high": 166.39, "low": 164.75, "close": 166.39, "volume": 4252, "bar_end": "2026-08-25 10:59:00"},
        ]
        prior15 = [
            {"open": 150 + index * 0.05, "high": 150.3 + index * 0.05, "low": 149.7 + index * 0.05,
             "close": 150 + index * 0.05, "volume": 1000, "bar_end": f"2026-08-24 {9 + index // 4:02d}:{(index % 4) * 15:02d}:00"}
            for index in range(24)
        ]
        subject = row(price=165.53, high=166.99, low=154.63)
        subject.update({"repair": 165.77, "pressure": 172.23})
        subject["quote"].update({"prev_close": 156.57, "pct": 5.72, "amount_wan": 180000})
        subject["rt_features"].update({"vwap": 162.73, "amount_ratio_1m": 1.7, "amount_ratio_5m": 2.4, "atr5m": 0.6})
        subject["sector_momentum"] = {"emotion_ok": True, "board_name": "CRO概念", "board_pct": 3.1}
        subject['code'] = '002821'
        subject["sector_rotation"] = {"sustained": True, "leader_healthy": True,
                                      'leader': {'code': '002821', 'pct': 5.72}}
        subject["strategy_contract"] = {
            "key": "LEADER_EMOTION", "is_observation_strategy": True, "daily_qualified": True,
            "allowed_patterns": ["V2_E5_LEADER_SECOND_LEG"],
        }
        with patch.object(timing, "closed_bars", side_effect=[bars5, intraday15]):
            decision = timing.evaluate(
                subject,
                [{"m": "11:00:00", "p": 165.53, "v": 605}],
                now=datetime(2026, 8, 25, 11, 0),
                history_120m=history_120m(),
                history_15m=prior15,
            )
        self.assertTrue(decision["entry_allowed"], decision)
        self.assertEqual(decision["entry_pattern"], "V2_E5_LEADER_SECOND_LEG")
        self.assertEqual(decision["location"], "LEADER_SECOND_LEG")
        self.assertGreater(decision["room_risk"]["reward_risk"], 1.5)

    def test_e5_flydragon_replay_locks_first_peak_and_confirms_by_1115(self):
        intraday15 = [
            {"open": 58.07, "high": 60.98, "low": 57.42, "close": 60.09, "volume": 273484, "bar_end": "2026-09-03 09:44:00"},
            {"open": 60.09, "high": 60.25, "low": 59.48, "close": 59.75, "volume": 77084, "bar_end": "2026-09-03 09:59:00"},
            {"open": 59.75, "high": 60.10, "low": 59.14, "close": 59.98, "volume": 56072, "bar_end": "2026-09-03 10:14:00"},
            {"open": 59.98, "high": 60.08, "low": 59.63, "close": 59.88, "volume": 20205, "bar_end": "2026-09-03 10:29:00"},
            {"open": 59.88, "high": 60.11, "low": 59.72, "close": 60.00, "volume": 22228, "bar_end": "2026-09-03 10:44:00"},
            {"open": 60.00, "high": 60.13, "low": 59.80, "close": 60.01, "volume": 15279, "bar_end": "2026-09-03 10:59:00"},
            {"open": 60.01, "high": 61.28, "low": 59.96, "close": 60.65, "volume": 74124, "bar_end": "2026-09-03 11:14:00"},
        ]
        bars5 = [
            {"open": 60.02, "high": 60.18, "low": 59.96, "close": 60.11, "volume": 11000},
            {"open": 60.11, "high": 60.35, "low": 60.08, "close": 60.28, "volume": 17000},
            {"open": 60.28, "high": 61.28, "low": 60.22, "close": 60.65, "volume": 46124},
        ]
        subject = row(price=60.65, high=61.28, low=57.42)
        subject.update({"repair": 60.20, "pressure": 63.88})
        subject["quote"].update({"prev_close": 58.07, "pct": 4.44, "amount_wan": 220000})
        subject["sector_momentum"] = {"emotion_ok": True, "board_name": "液冷服务器", "board_pct": 2.1}
        subject["sector_rotation"] = {"sustained": True, "leader_healthy": True}
        state = timing._leader_second_leg_state(
            subject, intraday15, bars5, 59.75, 0.70, 60.65,
            "TREND", "NORMAL", True, True, timing.load_config(),
        )
        self.assertTrue(state["eligible"], state)
        self.assertEqual(state["impulse_index"], 0)
        self.assertEqual(state["impulse_bar_end"], "2026-09-03 09:44:00")
        self.assertTrue(state["five_minute_breakout"])
        self.assertGreaterEqual(state["resume_volume_ratio"], 1.2)

    def test_e5a_failed_limit_reversal_can_confirm_after_two_closed_five_minute_bars(self):
        bars5 = [
            {"open": 4.77, "high": 5.05, "low": 4.75, "close": 5.03, "volume": 52000, "bar_end": "2026-09-04 09:34:00"},
            {"open": 5.04, "high": 5.19, "low": 5.08, "close": 5.18, "volume": 61000, "bar_end": "2026-09-04 09:39:00"},
        ]
        prior15 = [
            {"open": 4.40 + index * 0.01, "high": 4.45 + index * 0.01,
             "low": 4.35 + index * 0.01, "close": 4.41 + index * 0.01,
             "volume": 1000, "bar_end": f"2026-09-03 {9 + index // 4:02d}:{(index % 4) * 15:02d}:00"}
            for index in range(24)
        ]
        trend120 = [
            {"open": 4.0 + index * 0.004, "high": 4.04 + index * 0.004,
             "low": 3.97 + index * 0.004, "close": 4.01 + index * 0.004, "volume": 1000}
            for index in range(240)
        ]
        subject = row(price=5.18, high=5.19, low=4.75)
        subject.update({"defense": 4.70, "repair": 5.05, "pressure": 5.59})
        subject["quote"].update({
            "open": 4.77, "prev_close": 5.08, "pct": 1.97,
            "amount_wan": 32000, "turnover": 8.2,
        })
        subject["rt_features"].update({"vwap": 5.08, "atr5m": 0.08, "amount_ratio_1m": 1.5, "amount_ratio_5m": 1.4})
        # Synthetic positive case: the candidate leads this observed sample.
        subject['code'] = '000892'
        subject["sector_momentum"] = {"emotion_ok": True, "board_name": "影视院线", "board_pct": 1.6}
        subject["sector_rotation"] = {"sustained": True, "leader_healthy": True,
                                      'leader': {'code': '000892', 'pct': 1.97}}
        subject["strategy_contract"] = {
            "key": "LEADER_EMOTION", "is_observation_strategy": True,
            "daily_qualified": True,
            "allowed_patterns": ["V2_E5_LEADER_SECOND_LEG", "V2_E5A_LEADER_OPENING_REVERSAL"],
            "daily_metrics": {
                "leader_profile": "FAILED_LIMIT_REVERSAL",
                "emotion_pool_cross_verified": True,
                "prior_day_touched_limit": True,
                "prior_day_closed_limit": False,
            },
        }
        with patch.object(timing, "closed_bars", side_effect=[bars5, []]):
            decision = timing.evaluate(
                subject,
                [{"m": "09:40:00", "p": 5.18, "v": 1000}],
                now=datetime(2026, 9, 4, 9, 40),
                history_120m=trend120,
                history_15m=prior15,
            )
        self.assertTrue(decision["entry_allowed"], decision)
        self.assertEqual(decision["entry_pattern"], "V2_E5A_LEADER_OPENING_REVERSAL")
        self.assertEqual(decision["location"], "LEADER_OPENING_REVERSAL")
        self.assertEqual(decision["data_quality"]["closed_5m_bars"], 2)
        self.assertGreaterEqual(decision["room_risk"]["reward_risk"], 1.5)

    def test_e5_rejects_weak_resume_volume_after_valid_pullback(self):
        bars15 = [
            {"high": 10.80, "low": 10.00, "close": 10.70, "volume": 9000},
            {"high": 10.65, "low": 10.45, "close": 10.50, "volume": 4000},
            {"high": 10.72, "low": 10.50, "close": 10.68, "volume": 4300},
        ]
        bars5 = [
            {"high": 10.55, "close": 10.52},
            {"high": 10.62, "close": 10.60},
            {"high": 10.72, "close": 10.68},
        ]
        subject = row(price=10.68, high=10.80, low=10.00)
        subject.update({"repair": 10.60, "pressure": 11.50})
        subject["quote"].update({"prev_close": 10.20, "pct": 4.71, "amount_wan": 100000})
        subject["sector_momentum"] = {"emotion_ok": True, "board_name": "测试板块", "board_pct": 2.0}
        subject["sector_rotation"] = {"sustained": True, "leader_healthy": True}
        state = timing._leader_second_leg_state(
            subject, bars15, bars5, 10.40, 0.30, 10.68,
            "TREND", "NORMAL", True, True, timing.load_config(),
        )
        self.assertFalse(state["eligible"])
        self.assertIn("转强量比", state["reason"])

    def test_retired_e7_flat_base_breakout_cannot_create_a_new_entry(self):
        bars5 = [
            {"open": 4.24, "high": 4.28, "low": 4.22, "close": 4.27, "volume": 1800, "bar_end": "2026-08-28 13:05:00"},
            {"open": 4.27, "high": 4.31, "low": 4.24, "close": 4.30, "volume": 2300, "bar_end": "2026-08-28 13:10:00"},
            {"open": 4.30, "high": 4.36, "low": 4.27, "close": 4.34, "volume": 4900, "bar_end": "2026-08-28 13:15:00"},
        ]
        intraday15 = [
            {"open": 4.12, "high": 4.26, "low": 4.12, "close": 4.22, "volume": 6200, "bar_end": "2026-08-28 09:45:00"},
            {"open": 4.22, "high": 4.30, "low": 4.18, "close": 4.24, "volume": 5100, "bar_end": "2026-08-28 10:00:00"},
            {"open": 4.24, "high": 4.24, "low": 4.17, "close": 4.20, "volume": 2700, "bar_end": "2026-08-28 10:15:00"},
            {"open": 4.20, "high": 4.23, "low": 4.16, "close": 4.21, "volume": 2400, "bar_end": "2026-08-28 10:30:00"},
            {"open": 4.21, "high": 4.24, "low": 4.18, "close": 4.22, "volume": 2300, "bar_end": "2026-08-28 10:45:00"},
            {"open": 4.22, "high": 4.24, "low": 4.18, "close": 4.21, "volume": 2100, "bar_end": "2026-08-28 11:00:00"},
            {"open": 4.21, "high": 4.23, "low": 4.17, "close": 4.21, "volume": 1900, "bar_end": "2026-08-28 11:15:00"},
            {"open": 4.21, "high": 4.24, "low": 4.19, "close": 4.22, "volume": 2200, "bar_end": "2026-08-28 11:30:00"},
            {"open": 4.22, "high": 4.36, "low": 4.19, "close": 4.34, "volume": 9000, "bar_end": "2026-08-28 13:15:00"},
        ]
        prior15 = [
            {"open": 4.00 + index * 0.003, "high": 4.03 + index * 0.003,
             "low": 3.98 + index * 0.003, "close": 4.01 + index * 0.003,
             "volume": 1000, "bar_end": f"2026-08-27 {9 + index // 4:02d}:{(index % 4) * 15:02d}:00"}
            for index in range(24)
        ]
        trend120 = [
            {"open": 3.2 + index * 0.004, "high": 3.23 + index * 0.004,
             "low": 3.18 + index * 0.004, "close": 3.21 + index * 0.004, "volume": 1000}
            for index in range(240)
        ]
        subject = row(price=4.34, high=4.36, low=4.12)
        subject.update({"code": "600227", "name": "赤天化", "repair": 4.29, "pressure": 4.29, "defense": 4.01})
        subject["quote"].update({"open": 4.12, "prev_close": 4.16, "pct": 4.33, "amount_wan": 100000})
        subject["rt_features"].update({"vwap": 4.22, "atr5m": 0.05, "amount_ratio_1m": 1.25, "amount_ratio_5m": 1.55})
        subject["sector_momentum"] = {"emotion_ok": True, "board_name": "氮肥", "board_pct": 7.33}
        subject["sector_rotation"] = {"available": True, "sustained": False, "leader_healthy": False}
        market = {
            "entry_ready": True,
            "market_gate": {
                "allowed": False,
                "stage": "CORE_SECTOR_ROTATION_BLOCKED",
                "reason": "板块持续性样本尚未完成",
            },
        }
        with patch.object(timing, "closed_bars", side_effect=[bars5, intraday15]):
            decision = timing.evaluate(
                subject,
                [{"source": "tencent", "m": "13:15:00", "p": 4.34, "v": 100}],
                now=datetime(2026, 8, 28, 13, 15),
                history_120m=trend120,
                history_15m=prior15,
                market_data=market,
            )
        self.assertFalse(decision["entry_allowed"], decision)
        self.assertIsNone(decision["entry_pattern"])
        self.assertFalse(decision["market_gate_override"])
        self.assertGreater(decision["levels"]["structural_invalidation"], 0)
        self.assertGreater(decision["levels"]["entry_invalidation"], decision["levels"]["structural_invalidation"])

        hard_gate_market = {
            "entry_ready": False,
            "market_gate": {
                "allowed": False,
                "stage": "CORE_A_SHARE_TECH_REGIME_BLOCKED",
                "reason": "科技风险偏好与大盘环境不支持新仓",
            },
        }
        with patch.object(timing, "closed_bars", side_effect=[bars5, intraday15]):
            hard_gate_decision = timing.evaluate(
                subject,
                [{"source": "tencent", "m": "13:15:00", "p": 4.34, "v": 100}],
                now=datetime(2026, 8, 28, 13, 15),
                history_120m=trend120,
                history_15m=prior15,
                market_data=hard_gate_market,
            )
        self.assertFalse(hard_gate_decision["entry_allowed"], hard_gate_decision)
        self.assertFalse(hard_gate_decision["market_gate_override"])
        self.assertTrue(
            any("CORE_A_SHARE_TECH_REGIME_BLOCKED" in blocker for blocker in hard_gate_decision["blockers"]),
            hard_gate_decision,
        )


if __name__ == "__main__":
    unittest.main()
