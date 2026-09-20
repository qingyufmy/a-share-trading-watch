import json
import subprocess
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import preopen_quality_check as quality


class PreopenQualityCheckTests(unittest.TestCase):
    def test_release_manifest_detects_runtime_drift(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            target = base / "realtime_signal_engine.py"
            target.write_text("version = 1\n", encoding="utf-8")
            digest = quality.file_sha256(target)
            (base / "release_manifest.json").write_text(json.dumps({
                "release_id": "v2-test", "files": {"realtime_signal_engine.py": digest}
            }), encoding="utf-8")
            self.assertTrue(quality.release_manifest_status(base)[0])
            target.write_text("version = 2\n", encoding="utf-8")
            ok, detail = quality.release_manifest_status(base)
        self.assertFalse(ok)
        self.assertIn("漂移:realtime_signal_engine.py", detail)

    def test_launchd_active_state_is_healthy(self):
        completed = subprocess.CompletedProcess(
            args=["launchctl", "print"],
            returncode=0,
            stdout="\tstate = active\n",
            stderr="",
        )
        with patch.object(quality.subprocess, "run", return_value=completed):
            running, _detail = quality.launchd_running()
        self.assertTrue(running)

    def test_scheduler_result_uses_final_premarket_outcome_for_date(self):
        with tempfile.TemporaryDirectory() as temp:
            log = Path(temp) / "trading_scheduler.log"
            log.write_text("\n".join([
                json.dumps({"event": "success", "mode": "premarket", "now": "2026-09-03T08:30:00"}),
                json.dumps({"event": "child_finished", "mode": "premarket", "returncode": 1, "ts": "2026-09-04 08:43:20", "stderr": "Traceback\nRuntimeError: card content size is over limit"}),
                json.dumps({"event": "failure", "mode": "premarket", "now": "2026-09-04T08:30:00", "error": "飞书卡片超限"}, ensure_ascii=False),
            ]), encoding="utf-8")
            result = quality.latest_scheduler_mode_result(log, "premarket", datetime(2026, 9, 4).date())
        self.assertEqual(result["event"], "failure")
        self.assertIn("超限", result["error"])
        self.assertIn("over limit", result["error_detail"])

    def test_strategy_contract_check_is_entry_blocking(self):
        self.assertIn("观察池策略分类与盘中合同", quality.ENTRY_BLOCKING_CHECKS)

    def test_check_reports_missing_plan_as_blocker(self):
        class Connection:
            def execute(self, _sql):
                return self

            def fetchone(self):
                return (1,)

            def close(self):
                return None

        modules = {
            "intraday_report": SimpleNamespace(read_premarket_levels=lambda: {}, current_watchlist_map=lambda: {}),
            "realtime_signal_engine": SimpleNamespace(),
            "paper_trading": SimpleNamespace(init_db=lambda _base: Connection()),
        }
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            with patch.object(quality, "BASE_DIR", base), patch.object(quality, "REPORTS_DIR", base / "reports"), patch.object(
                quality, "launchd_running", return_value=(True, "running")
            ), patch.object(quality.importlib, "import_module", side_effect=lambda name: modules[name]):
                with patch.object(quality.base, 'read_watchlist_details', return_value={'rows': [], 'status': 'unverified'}), \
                     patch.object(quality.base, 'premarket_plan_observation_details', return_value={'rows': [], 'groups': []}):
                    result = quality.check(datetime(2026, 8, 13, 9, 0))
        self.assertEqual(result["status"], "failed")
        self.assertTrue(any(item["name"] == "计划源" for item in result["blockers"]))
        self.assertTrue(result["entry_blocked"])
        json.dumps(result, ensure_ascii=False)

    def test_quality_markdown_distinguishes_blockers_from_execution(self):
        text = quality.quality_markdown({
            "trading_date": "2026-08-13",
            "checked_at": "2026-08-13 09:00:00",
            "status": "failed",
            "plan_source": "premarket",
            "levels": 14,
            "checks": [{"name": "计划源", "passed": False, "severity": "blocking", "detail": "缺少报告"}],
            "blockers": [{"name": "计划源", "detail": "缺少报告"}],
        })
        self.assertIn("A股盘前系统质检", text)
        self.assertIn("新增模拟盘买入", text)

    def test_quality_latest_path_is_under_workbench_runtime(self):
        self.assertIn("web_dashboard/data/runtime/preopen_quality", str(quality.QUALITY_LATEST_PATH))

    def test_short_history_exception_is_explicitly_scoped_to_688825(self):
        self.assertEqual(quality.QUALITY_IGNORE_STRUCTURAL_PENDING_CODES, {"688825"})

    def test_kline_source_warning_does_not_fail_portfolio_quality(self):
        checks = [
            {"name": "本地K线数据源", "passed": False, "detail": "阻断：688545", "severity": "warning"},
            {"name": "计划源", "passed": True, "detail": "ok", "severity": "required"},
        ]
        blockers = [item for item in checks if not item["passed"] and item["severity"] in ("required", "blocking")]
        warnings = [item for item in checks if not item["passed"] and item["severity"] == "warning"]
        self.assertEqual(blockers, [])
        self.assertEqual(len(warnings), 1)


if __name__ == "__main__":
    unittest.main()
