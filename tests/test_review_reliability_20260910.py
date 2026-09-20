import json
import subprocess
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

import after_close_report as after
import run_trading_job as scheduler
import realtime_signal_engine as engine
import intraday_report as intra
from core import emotion_leader_pool as pool, intraday_timing_v2 as timing, method_entry
from core import runtime_reliability as reliability


class RecoveryTests(unittest.TestCase):
    def success(self, mode, stamp="2026-09-10T08:50:00", **extra):
        return {"event": "success", "mode": mode, "now": stamp, **extra}

    def test_late_boot_recovers_plan_before_quality(self):
        now = datetime(2026, 9, 10, 9, 0)
        self.assertEqual(scheduler.automatic_node(now, []), "premarket")
        self.assertEqual(scheduler.automatic_node(now, [self.success("premarket")]), "qualitycheck")

    def test_success_not_repeated_and_local_only_does_not_prove_delivery(self):
        now = datetime(2026, 9, 10, 8, 50)
        self.assertEqual(scheduler.automatic_node(now, [self.success("premarket")]), "skip")
        self.assertEqual(scheduler.automatic_node(now, [self.success("premarket", skip_feishu=True)]), "premarket")

    def test_no_late_plan_or_intraday_report_flood(self):
        for hour, minute in [(9, 5), (9, 20), (9, 35), (10, 1), (14, 59)]:
            self.assertNotEqual(scheduler.automatic_node(datetime(2026, 9, 10, hour, minute), []), "premarket")
        self.assertEqual(scheduler.automatic_node(datetime(2026, 9, 10, 10, 1), []), "skip")
        now = datetime(2026, 9, 10, 10, 0, 30)
        self.assertEqual(scheduler.automatic_node(now, [self.success("intraday", "2026-09-10T10:00:01")]), "skip")
        self.assertEqual(scheduler.automatic_node(now, [self.success("intraday", "2026-09-10T09:00:01")]), "intraday")

    def test_recovery_retry_bounded_and_cooldown(self):
        failure = {"event": "failure", "mode": "afterclose", "ts": "2026-09-10 16:32:00"}
        self.assertEqual(scheduler.automatic_node(datetime(2026, 9, 10, 16, 33), [failure]), "skip")
        self.assertEqual(scheduler.automatic_node(datetime(2026, 9, 10, 16, 40), [failure]), "afterclose")
        self.assertEqual(scheduler.automatic_node(datetime(2026, 9, 10, 16, 40), [failure, failure]), "skip")
        failure.update(mode="premarket", ts="2026-09-10 08:50:00")
        self.assertEqual(scheduler.automatic_node(datetime(2026, 9, 10, 8, 51), [failure]), "skip")
        self.assertEqual(scheduler.automatic_node(datetime(2026, 9, 10, 9, 0), [failure, failure]), "skip")

    def test_partial_delivery_is_not_automatically_repeated(self):
        event = {"event": "child_partial_failure", "mode": "premarket", "ts": "2026-09-10 08:50:00"}
        self.assertEqual(scheduler.automatic_node(datetime(2026, 9, 10, 9, 0), [event]), "skip")

    def test_storage_checks_do_not_clean_existing_files(self):
        with tempfile.TemporaryDirectory() as folder:
            p = Path(folder) / "keep.txt"
            p.write_text("user file")
            self.assertFalse(reliability.storage_readiness(folder, 10**30)["ready"])
            self.assertTrue(reliability.storage_readiness(folder, 0)["ready"])
            self.assertEqual(p.read_text(), "user file")
            self.assertEqual([x.name for x in Path(folder).iterdir()], ["keep.txt"])

    def test_write_failure_is_explicit(self):
        with patch.object(reliability.tempfile, "TemporaryFile", side_effect=OSError("disk full")):
            self.assertIn("disk full", reliability.storage_readiness(Path.cwd())["reason"])

    def test_wrapper_recovers_plan_then_rechecks_once(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            with patch.object(scheduler, "BASE_DIR", root), patch.object(scheduler, "LOG_DIR", root), \
                 patch.object(scheduler, "LOCK_PATH", root / "lock"), patch.object(scheduler, "LOG_PATH", root / "events"), \
                 patch.object(scheduler, "is_trading_day", return_value=True), \
                 patch.object(scheduler, "run_child", return_value="completed") as child:
                for stamp in ("09:00", "09:04", "09:04"):
                    with patch.object(scheduler.sys, "argv", ["job", "--mode", "auto", "--now", "2026-09-10 " + stamp]):
                        self.assertEqual(scheduler.main(), 0)
                self.assertEqual([call.args[0] for call in child.call_args_list], ["premarket", "qualitycheck"])

    def test_wrapper_storage_failure_does_not_start_report(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            with patch.object(scheduler, "BASE_DIR", root), patch.object(scheduler, "LOG_DIR", root), \
                 patch.object(scheduler, "LOCK_PATH", root / "lock"), patch.object(scheduler, "LOG_PATH", root / "events"), \
                 patch.object(scheduler, "is_trading_day", return_value=True), \
                 patch.object(scheduler, "storage_readiness", return_value={"ready": False, "reason": "disk full"}), \
                 patch.object(scheduler, "run_child") as child, \
                 patch.object(scheduler.sys, "argv", ["job", "--mode", "afterclose", "--now", "2026-09-10 16:30"]):
                self.assertEqual(scheduler.main(), 1)
                child.assert_not_called()


class ClosingFallbackTests(unittest.TestCase):
    def quote(self, stamp="20260910150000"):
        return {"close": 10, "open": 9.8, "high": 10.1, "low": 9.5, "datetime": stamp}

    def test_timeout_uses_dated_closing_cache(self):
        with patch.object(after, "parse_tencent_quotes", side_effect=TimeoutError):
            result = after.verified_closing_quotes([("600227", "17")], "2026-09-10", {"600227": self.quote()})
        self.assertEqual(result["600227"]["closing_evidence_source"], "closing_cache")

    def test_reject_old_future_intraday_missing_and_invalid_quotes(self):
        for q in [self.quote("20260909150000"), self.quote("20260911150000"),
                  self.quote("20260910145900"), {**self.quote(), "_stale": True},
                  {**self.quote(), "close": 11}, {}]:
            with self.subTest(q=q), patch.object(after, "parse_tencent_quotes", return_value={}):
                with self.assertRaisesRegex(RuntimeError, "覆盖不足"):
                    after.verified_closing_quotes([("600227", "17")], "2026-09-10", {"600227": q})

    def test_partial_primary_can_merge_cache_without_losing_live_quote(self):
        with patch.object(after, "parse_tencent_quotes", return_value={"1": self.quote()}):
            result = after.verified_closing_quotes([("1", "17"), ("2", "33")], "2026-09-10", {"2": self.quote()})
        self.assertEqual(len(result), 2)
        self.assertEqual(result["1"]["closing_evidence_source"], "live_snapshot")


class CandidateAndAuditTests(unittest.TestCase):
    def test_new_listing_cannot_be_mislabeled_as_limit_leader(self):
        raw = {"SECURITY_CODE": "301689", "SECURITY_SHORT_NAME": "N电科思",
               "NEWEST_PRICE": "54", "CHG": "237.5", "PEAK_PRICE<140>": "59.88",
               "TURNOVER_RATE": "80.84", "TRADING_VOLUMES": "25.25亿",
               "CIRCULATION_MARKET_VALUE<140>": "31.08亿"}
        snapshot = pool.normalize_eastmoney_response({"data": {"result": {"dataList": [raw]}}})
        row = snapshot["rows"][0]
        self.assertFalse(row["touched_limit"])
        self.assertFalse(row["closed_limit"])
        self.assertEqual(pool.select_leader_candidates(snapshot), [])
        # An old persisted snapshot must not bypass the new check either.
        row.update(name="N电科思", touched_limit=True, closed_limit=True, special_trading_state=False)
        self.assertEqual(pool.select_leader_candidates(snapshot), [])
        snapshot["candidates"] = [row]
        plan, metadata = pool.merge_into_plan({"rows": [], "groups": []}, snapshot)
        self.assertFalse(metadata)
        self.assertEqual(plan["rows"], [])

    def test_global_readiness_precedes_technical_plan(self):
        signal = {"symbol": "1", "strategy_family": "THREE_METHOD", "premarket_plan_allows_entry": True,
                  "premarket_plan_complete": True, "strategy_daily_qualified": True}
        health = {"preopen_quality_gate": {"allowed": False}}
        result = engine.entry_pipeline_summary([signal], health)
        self.assertEqual(result["passed"]["plan"], 0)
        self.assertEqual(result["first_blocked_symbols"]["global_readiness"], ["1"])
        self.assertIn("PREOPEN_PLAN_BLOCKED", engine.apply_runtime_quality(health))
        self.assertEqual(health["new_entry_status"], "blocked")

    def test_stale_leader_pool_warns_without_disabling_trend_by_itself(self):
        health = {"leader_pool": {"ready": False, "kaipanla_ready": False}}
        self.assertIn("LEADER_POOL_STALE", engine.apply_runtime_quality(health))
        self.assertEqual(health["new_entry_status"], "conditional")
        self.assertIn("KAIPANLA_CROSSCHECK_UNAVAILABLE", health["quality_warnings"])

    def test_market_green_cannot_override_missing_plan_in_card(self):
        view = intra.market_permission_view({"can_attack": True,
                                            "preopen_quality_gate": {"allowed": False}}, {})
        self.assertFalse(view["can_attack"])
        self.assertTrue(view["preparation_blocked"])
        self.assertIn("BLOCKED", view["permission"])

    def test_method_input_survives_json_and_reproduces_exact_branch(self):
        contract = {"key": "TREND_MA5", "daily_qualified": True, "is_observation_strategy": True,
                    "allowed_patterns": ["V2_E1_TREND_PULLBACK_RECLAIM"],
                    "daily_metrics": {"daily_asof": "2026-09-09", "daily_atr14": 1,
                                      "close_sum4": 40, "close_sum19": 190, "qualification_count": 3}}
        decision = timing.evaluate({"strategy_contract": contract, "quote": {"close": 10.1}}, [],
                                   now=datetime(2026, 9, 10, 10, 0))
        saved = json.loads(json.dumps(engine.json_safe(decision["method_replay_input"])))
        self.assertTrue(saved)
        self.assertEqual(saved["expected"], method_entry.evaluate(saved["contract"], saved["bars5"],
                         saved["price"], datetime.fromisoformat(saved["now"]), saved["config"]))


if __name__ == "__main__":
    unittest.main()
