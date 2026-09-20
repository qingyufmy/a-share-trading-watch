import subprocess
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

import run_trading_job as scheduler
import install_launch_agent as installer


class SchedulerRetryTests(unittest.TestCase):
    def test_failed_child_retries_once_then_returns_success_output(self):
        results = [
            subprocess.CompletedProcess(args=["python", "intraday_report.py"], returncode=1, stdout="temporary failure", stderr="timeout"),
            subprocess.CompletedProcess(args=["python", "intraday_report.py"], returncode=0, stdout="report complete", stderr=""),
        ]
        events = []

        with patch.object(scheduler.subprocess, "run", side_effect=results) as run, patch.object(
            scheduler, "log_event", side_effect=lambda **payload: events.append(payload)
        ), patch.object(scheduler.time, "sleep") as sleep:
            output = scheduler.run_child(
                "intraday",
                skip_feishu=True,
                now=datetime(2026, 8, 3, 10, 0),
            )

        self.assertEqual(output, "report complete")
        self.assertEqual(run.call_count, 2)
        sleep.assert_called_once_with(5)
        self.assertEqual(
            [event["event"] for event in events],
            ["child_finished", "child_retry", "child_finished"],
        )
        self.assertEqual(events[-1]["attempt"], 2)

    def test_auto_schedule_runs_premarket_plan_then_qualitycheck(self):
        self.assertEqual(scheduler.resolve_mode("auto", datetime(2026, 8, 12, 8, 30)), "premarket")
        self.assertEqual(scheduler.resolve_mode("auto", datetime(2026, 8, 12, 9, 0)), "qualitycheck")
        self.assertEqual(scheduler.resolve_mode("auto", datetime(2026, 8, 12, 9, 15)), "auctionpath")
        self.assertEqual(scheduler.resolve_mode("auto", datetime(2026, 8, 12, 9, 25)), "auction")
        self.assertEqual(scheduler.resolve_mode("auto", datetime(2026, 8, 12, 10, 0)), "intraday")
        self.assertEqual(scheduler.resolve_mode("auto", datetime(2026, 8, 12, 16, 30)), "afterclose")

    def test_existing_premarket_draft_does_not_skip_scheduled_refresh(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            report = base / "web_dashboard" / "data" / "reports" / "premarket_20260812.json"
            report.parent.mkdir(parents=True)
            report.write_text("{}", encoding="utf-8")
            with patch.object(scheduler, "BASE_DIR", base), patch.object(scheduler, "LOG_DIR", base / "logs"), patch.object(
                scheduler, "LOCK_PATH", base / "logs" / "job.lock"
            ), patch.object(scheduler, "is_trading_day", return_value=True), patch.object(
                scheduler, "run_child", return_value="refreshed"
            ) as run_child, patch.object(scheduler, "log_event"), patch.object(
                scheduler.sys, "argv", ["run_trading_job.py", "--mode", "premarket", "--now", "2026-08-12 08:30", "--skip-feishu"]
            ):
                result = scheduler.main()
        self.assertEqual(result, 0)
        run_child.assert_called_once()

    def test_intraday_launch_schedule_uses_full_hours(self):
        times = installer.trading_times()

        self.assertEqual(
            [item for item in times if item[0] in (10, 11, 13, 14, 15)],
            [(10, 0), (11, 0), (13, 0), (14, 0), (15, 0)],
        )
        self.assertNotIn((10, 30), times)
        self.assertNotIn((14, 30), times)

    def test_exchange_holiday_is_not_a_trading_day(self):
        self.assertFalse(scheduler.is_trading_day(datetime(2026, 10, 2, 10, 0)))
        self.assertTrue(scheduler.is_trading_day(datetime(2026, 8, 26, 10, 0)))

    def test_scheduled_key_node_keeps_feishu_enabled_when_launchd_allows_it(self):
        result = subprocess.CompletedProcess(args=["python", "after_close_report.py"], returncode=0, stdout="report complete", stderr="")
        with patch.dict(scheduler.os.environ, {"A_SHARE_DISABLE_FEISHU": "0"}, clear=False), patch.object(
            scheduler.subprocess, "run", return_value=result
        ) as run, patch.object(scheduler, "log_event"):
            scheduler.run_child("afterclose", skip_feishu=False, now=datetime(2026, 8, 12, 16, 30))
        self.assertNotIn("A_SHARE_SKIP_FEISHU", run.call_args.kwargs["env"])

    def test_scheduler_success_log_records_skip_feishu_audit_flag(self):
        events = []
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            with patch.object(scheduler, "BASE_DIR", base), patch.object(scheduler, "LOG_DIR", base / "logs"), patch.object(
                scheduler, "LOCK_PATH", base / "logs" / "job.lock"
            ), patch.object(scheduler, "is_trading_day", return_value=True), patch.object(
                scheduler, "run_child", return_value="rebuilt"
            ), patch.object(scheduler, "log_event", side_effect=lambda **payload: events.append(payload)), patch.object(
                scheduler.sys, "argv", ["run_trading_job.py", "--mode", "premarket", "--now", "2026-09-04 09:20", "--skip-feishu", "--force"]
            ):
                result = scheduler.main()
        self.assertEqual(result, 0)
        self.assertTrue(events[-1]["skip_feishu"])
