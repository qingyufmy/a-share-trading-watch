import unittest
from copy import deepcopy
from datetime import datetime, timedelta
from unittest.mock import patch

import realtime_signal_engine as engine


class NontradingQualityTests(unittest.TestCase):
    now = datetime(2026, 9, 15, 10, 0)

    def cache(self):
        return {"daily_contract_revalidation_health": {
            "report_date": "2026-09-15", "targets": 336, "revalidated": 335,
            "pending_retry": 1, "pending_errors": {"600301": {
                "name": "华锡有色", "error": "日线未更新至上一交易日2026-09-14"}}}}

    def quote(self):
        return {"datetime": "20260915095958", "open": 0, "high": 0, "low": 0,
                "volume_lot": 0, "amount_wan": 0, "close": 45.43, "prev_close": 45.43}

    def test_fresh_nontrading_is_warning_not_system_failure(self):
        cache = self.cache()
        result = engine.reconcile_nontrading_contract_health(cache, {"600301": self.quote()}, self.now)
        health = {"daily_contract_revalidation": result, "market_breadth": {"coverage_ready": True}}
        self.assertIsNone(engine.apply_runtime_quality(health))
        self.assertEqual(result["pending_retry"], 1)
        self.assertEqual(result["revalidated"], 335)
        self.assertEqual(result["active_pending_retry"], 0)
        self.assertIn("NONTRADING_STOCK_QUARANTINED", health["quality_warnings"])
        self.assertNotIn("daily_contract_revalidation_overrides", cache)
        _, _, message = engine.engine_health_alert_content("recovery", {}, health)
        self.assertIn("华锡有色(600301)", message)

    def test_invalid_stale_future_or_trading_quotes_cannot_be_excluded(self):
        for update in ({"datetime": "20260914150000"}, {"datetime": "20260915095700"},
                       {"datetime": "20260915100100"}, {"_stale": True}, {"open": None},
                       {"amount_wan": 1}, {"volume_lot": 1}, {"close": 45.44},
                       {"open": float("nan")}):
            with self.subTest(update=update):
                cache = self.cache()
                result = engine.reconcile_nontrading_contract_health(cache, {"600301": {**self.quote(), **update}}, self.now)
                self.assertEqual(result["active_pending_retry"], 1)
                self.assertEqual(engine.apply_runtime_quality({"daily_contract_revalidation": result}), "DAILY_CONTRACT_INCOMPLETE")
        cache = self.cache()
        self.assertEqual(engine.reconcile_nontrading_contract_health(cache, {"600301": {}}, self.now)["active_pending_retry"], 1)

    def test_transport_errors_and_mass_zero_quotes_remain_failures(self):
        cache = self.cache()
        cache["daily_contract_revalidation_health"]["pending_errors"]["600301"]["error"] = "HTTPError"
        self.assertEqual(engine.reconcile_nontrading_contract_health(cache, {"600301": self.quote()}, self.now)["active_pending_retry"], 1)
        cache = self.cache()
        h = cache["daily_contract_revalidation_health"]
        h["pending_errors"] = {str(n): deepcopy(h["pending_errors"]["600301"]) for n in range(4)}
        h["pending_retry"] = 4
        result = engine.reconcile_nontrading_contract_health(cache, {str(n): self.quote() for n in range(4)}, self.now)
        self.assertEqual(result["active_pending_retry"], 4)

    def test_resume_forces_retry_without_granting_permission(self):
        cache = self.cache()
        cache["daily_contract_revalidation_failures"] = {"600301": {"retry_at": 99999999999}}
        engine.reconcile_nontrading_contract_health(cache, {"600301": self.quote()}, self.now)
        cache["daily_contract_revalidation_health"].pop("nontrading_pending")
        result = engine.reconcile_nontrading_contract_health(cache, {"600301": {**self.quote(), "volume_lot": 1}}, self.now)
        self.assertEqual(result["active_pending_retry"], 1)
        self.assertEqual(cache["daily_contract_revalidation_failures"]["600301"]["retry_at"], 0)

    def test_before_open_and_cross_date_do_not_quarantine(self):
        for current in (self.now.replace(hour=9, minute=29), self.now + timedelta(days=1)):
            result = engine.reconcile_nontrading_contract_health(self.cache(), {"600301": self.quote()}, current)
            self.assertEqual(result["active_pending_retry"], 1)

    def test_revalidation_retries_with_backoff_and_denies_stock(self):
        cache, levels = {}, {"600301": {"name": "华锡有色", "premarket_plan_allows_entry": True}}
        with patch.object(engine.emotion_leader_pool, "load_latest_snapshot", return_value={}), \
             patch("core.sector_identity.load_catalog", return_value=[]), \
             patch("core.sector_identity.load_profiles", return_value={}), \
             patch.object(engine.intra.base, "previous_trading_date", return_value="2026-09-14"), \
             patch.object(engine.intra.base, "fetch_sohu_daily", return_value=[{"date": "2026-09-11"}]) as fetch, \
             patch.object(engine.time, "time", side_effect=[1000, 1010, 1030]):
            for i in range(3):
                engine.revalidate_planned_trend_contracts(levels, cache, "2026-09-15")
                self.assertFalse(levels["600301"]["premarket_plan_allows_entry"])
                self.assertFalse(levels["600301"]["strategy_daily_qualified"])
                self.assertEqual(cache["daily_contract_revalidation_health"]["pending_retry"], 1)
                self.assertEqual(fetch.call_count, 1 if i < 2 else 2)
        self.assertEqual(cache["daily_contract_revalidation_failures"]["600301"]["retry_at"], 1090)

    def test_other_critical_dependencies_still_block(self):
        result = engine.reconcile_nontrading_contract_health(self.cache(), {"600301": self.quote()}, self.now)
        health = {"daily_contract_revalidation": result, "storage": {"ready": False},
                  "market_breadth": {"coverage_ready": False}}
        self.assertIn("MARKET_BREADTH_INCOMPLETE", engine.apply_runtime_quality(health))
        self.assertEqual(health["new_entry_status"], "blocked")

    def test_quality_card_names_daily_failure(self):
        h = {"daily_contract_revalidation": {"targets": 336, "pending_retry": 1,
                                             "errors": ["600301(华锡有色):日线未更新"]}}
        _, _, message = engine.engine_health_alert_content("error", {
            "incident_kind": "data_quality", "error_fingerprint": "DAILY_CONTRACT_INCOMPLETE"}, h)
        self.assertIn("1/336只", message)
        self.assertIn("600301(华锡有色)", message)

    def test_new_failure_after_confirmed_recovery_is_not_silenced(self):
        state = {}
        for i in range(3):
            action, state = engine.engine_health_alert_transition(state, now=self.now + timedelta(seconds=i),
                error="MARKET_BREADTH_INCOMPLETE", incident_kind="data_quality")
        self.assertEqual(action, "error")

        for i in (10, 70, 131):
            action, state = engine.engine_health_alert_transition(state, now=self.now + timedelta(seconds=i))
        self.assertEqual(action, "recovery")
        for i in range(3):
            action, state = engine.engine_health_alert_transition(state, now=self.now + timedelta(seconds=140+i),
                error="MARKET_BREADTH_INCOMPLETE", incident_kind="data_quality")
        self.assertEqual(action, "error")

    def test_health_uses_observation_clock_not_slow_cycle_start(self):
        with patch.object(engine, "now_dt", return_value=self.now):
            health = engine.minute_bar_health([{"available": True, "last_minute_time": "09:59:58"}])
        self.assertEqual(health["latest_time"], "2026-09-15 09:59:58")
        self.assertEqual(health["delay_sec"], 2)

    def test_future_bar_is_not_relabelled_yesterday(self):
        health = engine.minute_bar_health([{"available": True, "last_minute_time": "10:02:00"}], now=self.now)
        self.assertFalse(health["available"])
        self.assertIsNone(health["latest_time"])
