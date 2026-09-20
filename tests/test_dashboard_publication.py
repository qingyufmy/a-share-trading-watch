import json
import subprocess
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

import render_report_dashboard as dashboard
import run_trading_job as scheduler


class DashboardPublicationTests(unittest.TestCase):
    rows = [{"_market_value": 1100, "_unrealized_pnl": 100, "_day_pnl": 10}]

    def test_explicit_nulls_remain_unknown(self):
        fields = ("market_value", "unrealized_pnl", "unrealized_pnl_pct",
                  "day_pnl", "day_pnl_pct")
        result = dashboard.paper_position_counts(self.rows, dict.fromkeys(fields))
        for field in fields:
            self.assertIsNone(result[field], field)
        self.assertEqual(result["position_day_pnl"], 10)

    def test_legacy_missing_fields_keep_position_fallback(self):
        result = dashboard.paper_position_counts(self.rows)
        self.assertEqual(result["market_value"], 1100)
        self.assertEqual(result["unrealized_pnl"], 100)
        self.assertEqual(result["unrealized_pnl_pct"], 10)
        self.assertEqual(result["day_pnl"], 10)

    def test_zero_account_values_are_not_replaced_by_position_totals(self):
        fields = ("market_value", "unrealized_pnl", "unrealized_pnl_pct",
                  "day_pnl", "day_pnl_pct", "total_assets")
        result = dashboard.paper_position_counts(self.rows, dict.fromkeys(fields, 0))
        for field in fields:
            self.assertEqual(result[field], 0, field)

    def test_numeric_account_rounding_unchanged(self):
        account = {"market_value": 891.123, "unrealized_pnl": -68.123,
                   "unrealized_pnl_pct": -7.091, "day_pnl": -2.123,
                   "day_pnl_pct": -.224, "cash": 983619, "total_assets": 984510}
        result = dashboard.paper_position_counts(self.rows, account)
        for field, value in account.items():
            self.assertEqual(result[field], round(value, 2), field)

    def test_reconciled_account_does_not_claim_position_pnl_as_account_pnl(self):
        result = dashboard.paper_position_counts(self.rows, {
            "day_pnl": None, "day_pnl_pct": None,
            "day_pnl_source": "unavailable_after_reconciliation",
        })
        self.assertIsNone(result["day_pnl"])
        self.assertIsNone(result["day_pnl_pct"])
        self.assertEqual(result["day_pnl_source"], "unavailable_after_reconciliation")

    def test_publish_null_account_writes_valid_report_and_index(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            source = base / "afterclose_20260907.md"
            source.write_text("# 盘后复盘 2026-09-07\n", encoding="utf-8")
            with patch.object(dashboard, "DATA_DIR", base), patch.object(
                dashboard, "REPORT_DIR", base / "reports"
            ), patch.object(dashboard, "INDEX_PATH", base / "index.json"), patch.object(
                dashboard, "load_runtime_paper_account", return_value={
                    "market_value": None, "unrealized_pnl": None, "day_pnl": None}
            ), patch.object(dashboard, "load_runtime_paper_orders", return_value=[]), patch.object(
                dashboard, "load_mira_evidence_summary", return_value=None
            ), patch.object(dashboard, "load_global_risk_context", return_value=None), patch.object(
                dashboard, "load_realtime_health", return_value={}
            ):
                result = dashboard.publish_report(source)
            record = json.loads(Path(result["json"]).read_text())
            self.assertIsNone(record["paper_position_counts"]["market_value"])
            self.assertIsNone(record["paper_position_counts"]["unrealized_pnl"])
            self.assertEqual(json.loads((base / "index.json").read_text())[0]["id"], result["id"])

    def test_final_json_with_diagnostic_prefix(self):
        result = {"dashboard": {"error": "null round"}, "rows": ["x" * 5000]}
        self.assertEqual(scheduler.child_report_result(
            'diagnostic\n{"progress": 1}\n' + json.dumps(result, indent=2)), result)
        self.assertEqual(scheduler.child_report_result("report complete"), {})

    def test_dashboard_failure_never_retries_delivery(self):
        for code in (0, 1):
            with self.subTest(returncode=code):
                payload = {"dashboard": {"error": "null round"},
                           "feishu_result": "success", "padding": "x" * 6000}
                child = subprocess.CompletedProcess([], code, json.dumps(payload), "")
                events = []
                with patch.object(scheduler.subprocess, "run", return_value=child) as run, patch.object(
                    scheduler, "log_event", side_effect=lambda **p: events.append(p)
                ), patch.object(scheduler.time, "sleep") as sleep:
                    with self.assertRaisesRegex(RuntimeError, "dashboard publication failed"):
                        scheduler.run_child("afterclose", False, datetime(2026, 9, 7, 16, 30))
                run.assert_called_once()
                sleep.assert_not_called()
                self.assertEqual(events[-1]["event"], "child_partial_failure")
                self.assertEqual(events[0]["report_result"]["dashboard"]["error"], "null round")

    def test_successful_dashboard_returns_without_retry(self):
        output = json.dumps({"dashboard": {"id": "afterclose_20260908"}})
        child = subprocess.CompletedProcess([], 0, output, "")
        with patch.object(scheduler.subprocess, "run", return_value=child) as run, patch.object(
            scheduler, "log_event"
        ):
            self.assertEqual(scheduler.run_child("afterclose", True, datetime(2026, 9, 7, 16, 30)), output)
        run.assert_called_once()

    def test_main_records_failure_not_success_for_dashboard_error(self):
        child = subprocess.CompletedProcess([], 0, json.dumps({"dashboard": {"error": "null round"}}), "")
        events = []
        with tempfile.TemporaryDirectory() as temp, patch.object(scheduler.subprocess, "run", return_value=child), patch.object(
            scheduler, "LOG_DIR", Path(temp)
        ), patch.object(scheduler, "LOCK_PATH", Path(temp) / "lock"), patch.object(
            scheduler, "log_event", side_effect=lambda **p: events.append(p)
        ), patch.object(scheduler, 'BASE_DIR', Path(temp)), patch.object(scheduler.sys, "argv", ["scheduler", "--mode", "afterclose", "--force", "--skip-feishu"]):
            self.assertEqual(scheduler.main(), 1)
        self.assertEqual(events[-1]["event"], "failure")
        self.assertNotIn("success", [e["event"] for e in events])
