import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import paper_trading
import realtime_signal_engine as engine
from core import intraday_timing_v2, observation_strategy_router, signal_contract


class ThreeMethodRuntimeAcceptanceTests(unittest.TestCase):
    def test_public_entry_surface_contains_only_three_named_methods(self):
        expected = {
            "STRATEGY_LEADER_ENTRY",
            "STRATEGY_520_ENTRY",
            "STRATEGY_MA5_ENTRY",
        }
        self.assertEqual(intraday_timing_v2.ENTRY_SCENARIOS, expected)
        self.assertEqual(observation_strategy_router.STRATEGY_ENTRY_SCENARIO_SET, expected)

    def test_raw_timing_patterns_cannot_reach_contract_or_paper_order(self):
        for scenario in intraday_timing_v2.TIMING_PATTERNS:
            with self.subTest(scenario=scenario):
                side, action = signal_contract.infer_side_action({"scenario": scenario})
                self.assertEqual(side, "NONE")
                self.assertEqual(action, "TIMING_EVIDENCE")
                self.assertIsNone(paper_trading.signal_side({"scenario": scenario}))

    def test_retired_same_day_radar_entrypoint_fails_explicitly(self):
        self.assertEqual(engine.evaluate_market_opportunity_signals([], 3), [])
        with self.assertRaisesRegex(RuntimeError, "retired entrypoint"):
            engine.make_market_opportunity_signal({})

    def test_realtime_markdown_uses_three_method_contract_language(self):
        with tempfile.TemporaryDirectory() as tmp:
            report = Path(tmp) / "realtime.md"
            with patch.object(engine, "MARKDOWN_REPORT", report), patch.object(
                engine.dashboard, "publish_report", return_value={"ok": True}
            ):
                engine.write_markdown(
                    [],
                    {"engine_status": "test", "global_risk": {}},
                    [],
                    paper_snapshot={},
                )
            text = report.read_text(encoding="utf-8")
            self.assertIn("三策略实时执行总览", text)
            self.assertIn("原始E1-E7仅作内部证据", text)
            self.assertNotIn("当前V2状态", text)


if __name__ == "__main__":
    unittest.main()
