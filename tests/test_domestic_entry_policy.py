import copy
from datetime import datetime, timedelta
import unittest

from core import global_risk as risk
import realtime_signal_engine as engine


class DomesticEntryPolicyTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 9, 14, 11, 30)
        self.base = {"date": "2026-09-14", "risk_level": "red", "risk_scope": "broad",
                     "policy": risk.policy_for_level("red"),
                     "intraday_path": {"event_state": "shock_unrepaired"}}
        self.local = {"state": "broad_strong", "stale": False,
                      "updated_at": str(self.now),
                      "indexes": {k: {"pct": v, "stale": False} for k, v in
                                  zip(("shanghai", "shenzhen", "growth"), (.17, -.26, -.47))},
                      "breadth": {"state": "broad_strong", "coverage_ready": True,
                                  "updated_at": str(self.now), "up_ratio": .6422}}

    def apply(self):
        return risk.apply_a_share_entry_policy(self.base, self.local, self.now)

    def test_external_broad_red_does_not_freeze_domestic_recovery(self):
        result = self.apply()
        self.assertTrue(result["policy"]["allow_core_attack_buy"])
        self.assertTrue(result["policy"]["allow_market_opportunity_buy"])
        self.assertIsNone(result["policy"]["disable_attack_buy_before"])
        self.assertEqual(result["risk_level"], "red")
        self.assertEqual(result["entry_risk_source"], "external_only")
        for bucket in ("technology", "other"):
            self.assertTrue(risk.core_attack_gate_status(result, {"risk_bucket": bucket}, self.now)[0])
            self.assertTrue(risk.market_opportunity_gate_status(result, {"risk_bucket": bucket}, self.now)[0])

    def test_tech_external_warning_does_not_override_strong_domestic_sector(self):
        self.base["risk_scope"] = "technology"
        result = self.apply()
        self.assertTrue(risk.core_attack_gate_status(result, {"risk_bucket": "technology"}, self.now)[0])

    def test_external_severity_never_changes_local_permission(self):
        for level in ("green", "yellow", "red"):
            for scope in ("normal", "technology", "broad"):
                with self.subTest(level=level, scope=scope):
                    self.base.update(risk_level=level, risk_scope=scope,
                                     policy=risk.policy_for_level(level, scope))
                    self.assertTrue(self.apply()["policy"]["allow_core_attack_buy"])

    def weak(self):
        self.local["state"] = "risk_off"
        self.local["breadth"]["state"] = "broad_weak"
        for v in self.local["indexes"].values():
            v["pct"] = -2

    def test_domestic_systemic_risk_blocks_both_entry_routes(self):
        self.weak()
        for external in ("green", "red"):
            self.base["risk_level"] = external
            result = self.apply()
            self.assertEqual(result["entry_risk_source"], "a_share_systemic")
            self.assertFalse(risk.core_attack_gate_status(result, {}, self.now)[0])
            self.assertFalse(risk.market_opportunity_gate_status(result, {}, self.now)[0])

    def test_growth_weakness_alone_is_not_a_broad_freeze(self):
        self.local["state"] = "risk_off"
        result = self.apply()
        self.assertTrue(result["policy"]["allow_core_attack_buy"])
        self.assertFalse(risk.core_attack_gate_status(result, {"risk_bucket": "technology"}, self.now)[0])

    def test_breadth_weakness_alone_is_not_systemic(self):
        self.local["state"] = "rotation_defensive"
        self.local["breadth"]["state"] = "broad_weak"
        self.assertFalse(self.apply()["a_share_entry_evidence"]["systemic_confirmed"])

    def test_recovery_retires_previous_ban_without_waiting_for_korea(self):
        original = copy.deepcopy(self.local)
        self.weak()
        self.base = self.apply()
        self.local = original
        self.assertTrue(self.apply()["policy"]["allow_core_attack_buy"])

    def test_missing_stale_incomplete_future_or_nan_domestic_data_blocks_as_data(self):
        original = copy.deepcopy(self.local)
        mutations = [
            lambda x: x.update(stale=True),
            lambda x: x.update(indexes={}),
            lambda x: x["indexes"]["shanghai"].update(pct=float("nan")),
            lambda x: x["indexes"]["shanghai"].update(pct=float("inf")),
            lambda x: x["indexes"]["growth"].update(stale=True),
            lambda x: x.update(updated_at=str(self.now-timedelta(days=1))),
            lambda x: x["breadth"].update(updated_at=str(self.now-timedelta(seconds=301))),
            lambda x: x["breadth"].update(updated_at=str(self.now+timedelta(seconds=1))),
            lambda x: x["breadth"].update(coverage_ready=False),
            lambda x: x.update(breadth={}),
        ]
        for i, mutate in enumerate(mutations):
            with self.subTest(case=i):
                self.local = copy.deepcopy(original)
                mutate(self.local)
                result = self.apply()
                self.assertEqual(result["entry_risk_source"], "a_share_data_unready")
                self.assertEqual(risk.core_attack_gate_status(result, {}, self.now)[1], "CORE_A_SHARE_DATA_STALE")
                self.assertEqual(risk.market_opportunity_gate_status(result, {}, self.now)[1], "A_SHARE_REGIME_BLOCKED")

    def test_defensive_rules_and_inputs_unchanged(self):
        original = copy.deepcopy(self.base)
        local = copy.deepcopy(self.local)
        result = self.apply()
        self.assertEqual(original, self.base)
        self.assertEqual(local, self.local)
        for key in ("overnight_position_cap_pct", "profit_guard_day_loss_pct", "portfolio_risk_day_loss_pct"):
            self.assertEqual(result["policy"][key], original["policy"][key])

    def test_inferred_premarket_warning_cannot_close_entry(self):
        result = risk.infer_premarket_context("2026-09-14", [], now=self.now)
        self.assertEqual(result["entry_policy_version"], risk.LOCAL_ENTRY_POLICY_VERSION)
        self.assertTrue(result["policy"]["allow_core_attack_buy"])

    def test_display_does_not_call_overseas_red_domestic_systemic(self):
        self.assertNotIn("全市场系统性", risk.summary_line(self.apply()))

    def test_budget_does_not_have_a_second_overseas_broad_ban(self):
        self.assertGreater(engine.rotation_pilot_budget(self.apply())["cap_pct"], 0)
        self.weak()
        self.assertEqual(engine.rotation_pilot_budget(self.apply())["cap_pct"], 0)


if __name__ == "__main__":
    unittest.main()
