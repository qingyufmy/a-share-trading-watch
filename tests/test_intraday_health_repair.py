import unittest
from copy import deepcopy
from datetime import datetime, timedelta
from unittest.mock import patch

import realtime_signal_engine as engine
from core import observation_strategy_router as router


class IntradayHealthRepairTests(unittest.TestCase):
    now = datetime(2026, 9, 8, 9, 32)

    def alerted_quality_state(self):
        state = {}
        for i in range(3):
            action, state = engine.engine_health_alert_transition(
                state, now=self.now + timedelta(seconds=i * 5), error="MARKET_BREADTH_INCOMPLETE",
                incident_kind="data_quality")
        self.assertEqual(action, "error")
        return state

    def test_changed_quality_subset_does_not_reset_cooldown(self):
        state = self.alerted_quality_state()
        original_alert_time = state["last_error_alert_at"]
        for i in range(6):
            action, state = engine.engine_health_alert_transition(
                state, now=self.now + timedelta(seconds=20 + i * 5),
                error="MARKET_BREADTH_INCOMPLETE" + (",DAILY_CONTRACT_INCOMPLETE" if i % 2 else ""),
                incident_kind="data_quality")
            self.assertIsNone(action)
            self.assertEqual(state["last_error_alert_at"], original_alert_time)

    def test_flapping_quality_has_no_repeated_error_or_recovery(self):
        state = self.alerted_quality_state()
        action, state = engine.engine_health_alert_transition(state, now=self.now + timedelta(seconds=15))
        self.assertIsNone(action)
        for i in range(3):
            action, state = engine.engine_health_alert_transition(
                state, now=self.now + timedelta(seconds=20 + i * 5),
                error="MARKET_BREADTH_INCOMPLETE", incident_kind="data_quality")
            self.assertIsNone(action)
        action, state = engine.engine_health_alert_transition(state, now=self.now + timedelta(seconds=40))
        self.assertIsNone(action)

    def test_quality_alert_after_cooldown_and_new_exception_escalation(self):
        state = self.alerted_quality_state()
        action, state = engine.engine_health_alert_transition(
            state, now=self.now + timedelta(seconds=1811), error="MARKET_BREADTH_INCOMPLETE",
            incident_kind="data_quality")
        self.assertIsNone(action)
        action, state = engine.engine_health_alert_transition(
            state, now=self.now + timedelta(seconds=7211), error="MARKET_BREADTH_INCOMPLETE",
            incident_kind="data_quality")
        self.assertEqual(action, "error")
        for i in range(3):
            action, state = engine.engine_health_alert_transition(
                state, now=self.now + timedelta(seconds=7220 + i * 5), error="database locked")
        self.assertEqual(action, "error")
        self.assertEqual(state["incident_kind"], "engine_error")

    def test_same_exception_second_episode_can_send_recovery(self):
        state = {}
        for episode in range(2):
            for i in range(3):
                action, state = engine.engine_health_alert_transition(
                    state, now=self.now + timedelta(seconds=episode * 100 + i * 5), error="exception")
            self.assertEqual(action, "error")
            action, state = engine.engine_health_alert_transition(
                state, now=self.now + timedelta(seconds=episode * 100 + 20))
            self.assertEqual(action, "recovery")

    def test_plan_warning_does_not_claim_engine_failure_or_unlock_stock(self):
        health = {"market_breadth": {"coverage_ready": True}, "ledger_quality": {"ready": True},
                  "daily_contract_revalidation": {"mainline_pending": ["600183"], "pending_retry": 0}}
        self.assertIsNone(engine.apply_runtime_quality(health))
        self.assertEqual(health["engine_status"], "ok")
        self.assertEqual(health["quality_warnings"], ["PREMARKET_MAINLINE_REVIEW_REQUIRED"])
        contract = {"key": router.TREND_520, "is_observation_strategy": True,
                    "daily_metrics": {"resonance_policy": "locked_mainline_v1", "resonance_boards": []}}
        self.assertFalse(router.sector_resonance_gate(contract, {
            "sector_momentum": {"board_name": "PCB", "board_pct": 5, "emotion_ok": True},
            "sector_rotation": {"sustained": True, "leader_healthy": True}})[0])

    def test_breadth_ledger_and_daily_failures_remain_quality_errors(self):
        health = {"market_breadth": {"coverage_ready": False}, "ledger_quality": {"ready": False},
                  "daily_contract_revalidation": {"pending_retry": 1}}
        error = engine.apply_runtime_quality(health)
        self.assertEqual(len(error.split(",")), 3)
        self.assertEqual(health["engine_status"], "degraded")

    def test_quality_card_is_not_interruption_or_buy_signal(self):
        title, color, summary = engine.engine_health_alert_content("error", self.alerted_quality_state(), {})
        self.assertIn("数据降级", title)
        self.assertIn("盯盘仍在运行", summary)
        self.assertNotIn("中断", title)
        self.assertEqual(color, "orange")
        title, color, summary = engine.engine_health_alert_content("recovery", {}, {
            "daily_contract_revalidation": {"mainline_pending": ["600183"]}})
        self.assertNotEqual(color, "green")
        self.assertIn("1只", summary)

    def test_failed_page_does_not_pollute_breadth_samples(self):
        cache = {"market_breadth_samples": [{"amount_yuan": 100, "timestamp": "2026-09-08 09:30:00"}]}
        before = deepcopy(cache)
        snapshot = {"rows": [{"f12": str(i), "f3": 1, "f6": 100} for i in range(100)],
                    "reported_total": 101, "failed_pages": [2], "page_errors": {2: "RemoteDisconnected"}}
        with patch.object(engine, "fetch_resilient_market_breadth", return_value=snapshot), patch.object(
            engine, "write_market_profile_cache"):
            result = engine.refresh_market_breadth(cache, self.now)
        self.assertFalse(result["coverage_ready"])
        self.assertIsNone(result["up_ratio"])
        self.assertEqual(cache["market_breadth_samples"], before["market_breadth_samples"])
        self.assertEqual(result["samples"], 1)
        self.assertEqual(result["page_errors"], snapshot["page_errors"])

    def test_fetch_budget_prevents_more_network_calls(self):
        with patch.object(engine.time, "monotonic", side_effect=[0, 21]), patch.object(engine, "fetch_json_fast") as fetch:
            result = engine.fetch_market_breadth_snapshot_fast()
        fetch.assert_not_called()
        self.assertEqual(result["failed_pages"], [1])
        self.assertEqual(result["page_errors"][1], "fetch_budget_exhausted")

    def test_truncated_page_is_not_complete(self):
        with patch.object(engine, "fetch_json_fast", return_value={
            "data": {"total": 100, "diff": [{"f12": "000001"}]}}):
            result = engine.fetch_market_breadth_snapshot_fast()
        self.assertEqual(result["failed_pages"], [1])
        self.assertFalse(result["rows"])

    def test_provider_exception_is_visible_without_sensitive_url(self):
        with patch.object(engine, "fetch_json_fast", side_effect=ConnectionError("private URL")):
            result = engine.fetch_market_breadth_snapshot_fast()
        self.assertEqual(result["page_errors"][1], "ConnectionError")
        self.assertNotIn("private URL", str(result))
