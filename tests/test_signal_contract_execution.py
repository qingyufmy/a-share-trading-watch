import unittest
from datetime import datetime

from core import signal_contract, sim_broker, strategy_discipline


def entry_signal(**updates):
    signal = {
        "trading_date": "2026-08-26",
        "symbol": "600000",
        "name": "测试股份",
        "scenario": "STRATEGY_520_ENTRY",
        "strategy_family": "THREE_METHOD",
        "strategy_contract": {
            "key": "TREND_520", "name": "520战法", "is_observation_strategy": True,
            "daily_qualified": True,
            "allowed_patterns": ["V2_E1_TREND_PULLBACK_RECLAIM", "V2_E3_MA20_STRUCTURAL_RECLAIM", "V2_E4_BREAKOUT_RETEST"],
        },
        "sector_resonance_ok": True,
        "sector_resonance_reason": "板块共振通过",
        "strategy_daily_qualified": True,
        "strategy_daily_evidence": "520日线资格通过",
        "strategy_gate_ok": True,
        "strategy_gate_reason": "E1证据匹配",
        "priority": "P1",
        "external_status": "立即处理",
        "current_price": 10.00,
        "prev_close": 9.80,
        "trigger_price": 10.00,
        "execution_band_low": 9.99,
        "execution_band_high": 10.01,
        "invalid_price": 9.70,
        "nearest_resistance": 10.90,
        "confirm_rule": "已收盘V2确认",
        "cancel_rule": "跌破9.70取消",
        "updated_at": "2026-08-26 10:00:00",
        "atr5m": 0.20,
        "atr1m": 0.03,
        "spread": 0.01,
        "amount_ratio_1m": 1.60,
        "last_volume": 1000,
        "limit_up": 10.78,
        "limit_down": 8.82,
        "timing_v2": {
            "entry_allowed": True,
            "entry_pattern": "V2_E1_TREND_PULLBACK_RECLAIM",
            "regime": "TREND",
            "location": "TIER1_SUPPORT",
            "setup_15m": "E1_TREND_PULLBACK_RECLAIM",
            "execution_5m": "VWAP_RECLAIM",
            "source_bar_close": {"5m": "09:59:00", "15m": "09:59:00"},
            "room_risk": {"reward_risk": 2.5, "execution_band_low": 9.99, "execution_band_high": 10.01},
            "execution_gates": {"volume_confirmed": True, "vwap_distance_ok": True},
        },
        "opportunity_rating": {"grade": "A", "score": 86, "hard_vetoed": False, "components": {}},
    }
    signal.update(updates)
    return signal


class SignalContractExecutionTests(unittest.TestCase):
    def test_raw_timing_pattern_cannot_create_buy_contract(self):
        signal = entry_signal(
            scenario="V2_E1_TREND_PULLBACK_RECLAIM",
            strategy_family="V2_ONLY",
        )

        contract = signal_contract.from_signal(signal)

        self.assertEqual(contract.side, "NONE")
        self.assertEqual(contract.action_code, "TIMING_EVIDENCE")
        self.assertFalse(contract.sim_allowed)

    def test_contract_widens_realistic_band_and_freezes_net_rr(self):
        contract = signal_contract.from_signal(entry_signal())
        self.assertGreaterEqual(contract.exec_high - 10.00, 0.06 - 1e-9)
        self.assertLessEqual(contract.exec_low, 9.94)
        self.assertGreater(contract.net_reward_risk, 1.5)
        self.assertTrue(contract.sim_allowed)
        self.assertTrue(contract.contract_id)
        self.assertEqual(contract.strategy_evidence["source_bar_close"]["5m"], "09:59:00")

    def test_dynamic_rotation_budget_requires_a_or_b_without_hard_veto(self):
        contract = signal_contract.from_signal(entry_signal(
            rotation_pilot=True,
            opportunity_rating={"grade": "C", "score": 65, "hard_vetoed": False},
        ))
        self.assertFalse(contract.sim_allowed)
        self.assertIn("A/B", contract.sim_reason)

    def test_all_new_entries_require_a_or_b_rating(self):
        contract = signal_contract.from_signal(entry_signal(
            opportunity_rating={"grade": "C", "score": 68, "hard_vetoed": False},
        ))
        self.assertFalse(contract.sim_allowed)
        self.assertIn("A/B", contract.sim_reason)

    def test_missing_target_cannot_bypass_cost_after_rr(self):
        contract = signal_contract.from_signal(entry_signal(nearest_resistance=None))
        self.assertFalse(contract.sim_allowed)
        self.assertIsNone(contract.net_reward_risk)
        self.assertIn("成本后RR", contract.sim_reason)

    def test_executor_does_not_reinterpret_frozen_v2_strategy(self):
        signal = entry_signal(amount_ratio_1m=0.20)
        signal.update(signal_contract.attach_contract(signal))
        discipline = strategy_discipline.build_discipline(signal, "BUY", now_session_allows=True)
        self.assertTrue(discipline["passed"], discipline)
        self.assertNotIn("量能必须确认", [item["name"] for item in discipline["checks"]])

    def test_expired_contract_cannot_fill(self):
        signal = entry_signal(updated_at="2026-08-26 10:00:00", stale_after_sec=90)
        result = sim_broker.simulate_signal(signal, {}, datetime(2026, 8, 26, 10, 2), buy_qty=100)
        self.assertEqual(result.status, "EXPIRED")

    def test_valid_contract_can_reach_simulated_fill(self):
        signal = entry_signal()
        result = sim_broker.simulate_signal(signal, {}, datetime(2026, 8, 26, 10, 0, 30), buy_qty=100)
        self.assertEqual(result.status, "FILLED")
        self.assertLessEqual(result.fill_price, signal_contract.from_signal(signal).exec_high)


if __name__ == "__main__":
    unittest.main()
