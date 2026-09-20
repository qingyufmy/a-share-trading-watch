import json
import os
import unittest
from unittest.mock import patch

import realtime_signal_engine as engine


class PaperTradeSerializationTests(unittest.TestCase):
    def test_paper_holding_is_visible_to_v2_position_evaluation(self):
        merged = engine.merge_signal_positions(
            {},
            [{"symbol": "000831", "quantity": 500, "sellable": 500, "avg_cost": 56.73}],
        )

        self.assertEqual(merged["000831"]["quantity"], 500)
        self.assertEqual(merged["000831"]["sellable"], 500)
        self.assertEqual(merged["000831"]["cost"], 56.73)
        self.assertEqual(merged["000831"]["position_sources"], ["paper"])

    def test_paper_holding_enriches_manual_position_conservatively(self):
        merged = engine.merge_signal_positions(
            {"000989": {"quantity": 1000, "sellable": 900, "cost": 9.8}},
            [{"symbol": "000989", "quantity": 3100, "sellable": 3100, "avg_cost": 9.59}],
        )

        self.assertEqual(merged["000989"]["quantity"], 3100)
        self.assertEqual(merged["000989"]["sellable"], 3100)
        self.assertEqual(merged["000989"]["position_sources"], ["manual", "paper"])

    def test_compact_result_breaks_order_to_signal_cycle(self):
        signal = {"symbol": "000831", "scenario": "CORE_INITIAL_ENTRY"}
        order_payload = {
            "order_id": "paper-test",
            "status": "CANCELLED",
            "reason": "high_open_cancel",
            "side": "CANCEL",
            "qty": 0,
            "signal_price": 56.73,
            "signal": signal,
        }

        signal["paper_trade"] = engine.compact_paper_trade(order_payload)

        encoded = json.dumps(signal, ensure_ascii=False)
        self.assertIn("paper-test", encoded)
        self.assertNotIn("\"signal\":", encoded)

    def test_dashboard_does_not_hide_watchlist_rows_after_twentieth_symbol(self):
        signals = [
            {
                "symbol": f"{index:06d}", "scenario": "V2_WAIT", "priority": "P2",
                "external_status": "观察", "distance_pct": 0,
            }
            for index in range(40)
        ]
        with patch.dict(os.environ, {"A_SHARE_DASHBOARD_SIGNAL_LIMIT": "80"}):
            visible = engine.visible_signals_for_dashboard(signals)
        self.assertEqual(len(visible), 40)
        self.assertIn("000039", {row["symbol"] for row in visible})

    def test_dashboard_default_covers_current_full_watchlist(self):
        signals = [
            {
                "symbol": f"{index:06d}", "scenario": "V2_WAIT", "priority": "P2",
                "external_status": "观察", "distance_pct": 0,
            }
            for index in range(150)
        ]
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("A_SHARE_DASHBOARD_SIGNAL_LIMIT", None)
            visible = engine.visible_signals_for_dashboard(signals)
        self.assertEqual(len(visible), 150)


if __name__ == "__main__":
    unittest.main()
