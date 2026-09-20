import unittest
from datetime import datetime
from unittest.mock import patch

from core.global_risk import policy_for_level
from core.signal_contract import from_signal
from paper_trading import realized_pnl_by_order
import realtime_signal_engine as engine


class RiskAndRealizedPnlTests(unittest.TestCase):
    def test_overnight_policy_caps_are_explicit(self):
        self.assertEqual(policy_for_level("red")["overnight_position_cap_pct"], 3.0)
        self.assertEqual(policy_for_level("yellow")["overnight_position_cap_pct"], 6.0)
        self.assertEqual(policy_for_level("green")["overnight_position_cap_pct"], 10.0)

    def test_v2_structural_exit_contract_is_sell(self):
        signal = {
            "trading_date": "2026-07-16",
            "symbol": "600000",
            "name": "测试",
            "scenario": "V2_STRUCTURAL_EXIT",
            "priority": "P0",
            "external_status": "立即处理",
            "internal_state": "FIRED",
            "current_price": 100,
            "trigger_price": 100,
            "invalid_price": 95,
            "execution_band_low": 99.9,
            "execution_band_high": 100.1,
            "confirm_rule": "120分钟结构支撑与MA99/128带同步失守",
            "cancel_rule": "仅在后续已收盘15分钟结构收复后重新评估",
            "action": "V2结构退出",
            "reasons": ["V2结构退出", "120分钟支撑失守"],
            "timing_v2": {"position_action": "STRUCTURAL_EXIT"},
        }
        contract = from_signal(signal).to_dict()
        self.assertEqual(contract["side"], "SELL")
        self.assertEqual(contract["action_code"], "STRUCTURAL_EXIT")

    def test_red_carry_de_risk_requires_confirmed_opening_break(self):
        quote = {"close": 96.0, "pct": -4.0, "open": 100.0, "prev_close": 101.0}
        feat = {
            "or5_low": 97.0,
            "last3_prices": [96.2, 95.8, 96.0],
            "amount_ratio_1m": 1.20,
            "amount_ratio_5m": 1.06,
        }
        confirmed, detail = engine.opening_red_carry_confirmation(quote, feat, 96.0, 99.0, 0.10)
        self.assertTrue(confirmed, detail)
        self.assertIn("破位", detail)

        weak_quote = {**quote, "pct": -2.0}
        confirmed, _ = engine.opening_red_carry_confirmation(weak_quote, feat, 96.0, 99.0, 0.10)
        self.assertFalse(confirmed)

    def test_red_carry_guard_is_sellable_and_time_gated(self):
        row = {
            "paper_sellable": 300,
            "paper_unrealized_pnl_pct": 1.0,
            "rt_features": {
                "vwap": 99.0,
                "epsilon": 0.10,
                "or5_low": 97.0,
                "last3_prices": [96.2, 95.8, 96.0],
                "amount_ratio_1m": 1.20,
                "amount_ratio_5m": 1.06,
            },
        }
        quote = {"close": 96.0, "pct": -4.0, "open": 100.0, "prev_close": 101.0}
        context = {"risk_level": "red", "policy": {}}
        with patch.object(engine, "now_dt", return_value=datetime(2026, 7, 17, 9, 40)):
            result = engine.paper_profit_guard_reason({**row}, quote, context)
        self.assertIsNotNone(result)
        self.assertEqual(result[0], "OPEN_RED_CARRY_DE_RISK")
        self.assertAlmostEqual(result[2], 0.34)

        with patch.object(engine, "now_dt", return_value=datetime(2026, 7, 17, 10, 0)):
            result = engine.paper_profit_guard_reason({**row}, quote, context)
        self.assertIsNone(result)

    def test_realized_pnl_is_fifo_and_does_not_mutate_orders(self):
        orders = [
            {"order_id": "b1", "created_at": "2026-07-15 10:00:00", "symbol": "600000", "side": "BUY", "qty": 100, "fill_price": 10, "status": "FILLED"},
            {"order_id": "b2", "created_at": "2026-07-15 11:00:00", "symbol": "600000", "side": "BUY", "qty": 100, "fill_price": 12, "status": "FILLED"},
            {"order_id": "s1", "created_at": "2026-07-16 10:00:00", "symbol": "600000", "side": "SELL", "qty": 150, "fill_price": 11, "status": "FILLED"},
        ]
        result = realized_pnl_by_order(orders)
        self.assertAlmostEqual(result["s1"]["realized_pnl"], 50.0)
        self.assertEqual(result["s1"]["realized_qty"], 150)
        self.assertAlmostEqual(result["s1"]["realized_cost_basis"], 1600.0)
        self.assertEqual(orders[2]["qty"], 150)


if __name__ == "__main__":
    unittest.main()
