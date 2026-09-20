import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

import realtime_signal_engine as engine


class PreopenQualityGateTests(unittest.TestCase):
    def test_critical_quality_failure_blocks_new_entries_after_auction(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "latest.json"
            path.write_text(json.dumps({
                "trading_date": "2026-08-14",
                "entry_blocked": True,
                "entry_blockers": [{"name": "实时健康状态"}],
            }), encoding="utf-8")
            with patch.object(engine, "PREOPEN_QUALITY_LATEST", path), patch.dict(
                engine.os.environ, {"A_SHARE_ENFORCE_PREOPEN_QUALITY": "1"}, clear=False
            ):
                gate = engine.preopen_quality_entry_gate(datetime(2026, 8, 14, 9, 30))
        self.assertFalse(gate["allowed"])
        self.assertIn("实时健康状态", gate["reason"])

    def test_quote_data_issue_does_not_create_global_entry_block(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "latest.json"
            path.write_text(json.dumps({
                "trading_date": "2026-08-14",
                "entry_blocked": False,
                "blockers": [{"name": "本地K线数据源"}],
            }), encoding="utf-8")
            with patch.object(engine, "PREOPEN_QUALITY_LATEST", path), patch.dict(
                engine.os.environ, {"A_SHARE_ENFORCE_PREOPEN_QUALITY": "1"}, clear=False
            ):
                gate = engine.preopen_quality_entry_gate(datetime(2026, 8, 14, 9, 30))
        self.assertTrue(gate["allowed"])


if __name__ == "__main__":
    unittest.main()
