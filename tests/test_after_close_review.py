import unittest

from after_close_report import (
    analyze_paper_order,
    build_market_decision_funnel,
    signal_action_type,
)


class AfterCloseReviewTests(unittest.TestCase):
    def test_partial_fill_is_not_reported_as_unfilled(self):
        order = {
            "status": "PARTIAL_FILLED",
            "side": "BUY",
            "symbol": "300059",
            "name": "东方财富",
            "qty": 900,
            "fill_price": 20.14,
            "created_at": "2026-07-23 13:00:12",
            "reason": "按执行区、滑点和成交量参与率模拟成交",
            "discipline_check": {"passed": True, "summary": "纪律通过"},
            "strategy_rationale": {"action_basis": ["连续分钟确认", "盈亏比达标"]},
        }
        result = analyze_paper_order(order, points=[])
        self.assertEqual(result["status"], "PARTIAL_FILLED")
        self.assertNotEqual(result["trade_quality"], "未成交")
        self.assertEqual(result["trade_quality"], "已成交待复盘")

    def test_named_strategy_entry_is_counted_as_probe_entry(self):
        self.assertEqual(signal_action_type("STRATEGY_520_ENTRY"), "三策略首笔试仓")
        self.assertNotEqual(signal_action_type("V2_E1_TREND_PULLBACK_RECLAIM"), "三策略首笔试仓")

    def test_market_decision_funnel_keeps_candidate_path(self):
        decisions = [
            {"symbol": "600000", "stage": "CANDIDATE"},
            {"symbol": "600000", "stage": "TECHNICAL_BLOCKED"},
            {"symbol": "300001", "stage": "CANDIDATE"},
            {"symbol": "300001", "stage": "SIGNAL_READY"},
        ]
        orders = [
            {
                "symbol": "300001",
                "scenario": "MARKET_OPPORTUNITY_ACTIONABLE",
                "status": "REJECTED",
            }
        ]
        funnel = build_market_decision_funnel(decisions, orders)
        self.assertEqual(funnel["counts"]["candidate"], 2)
        self.assertEqual(funnel["counts"]["technical_blocked"], 1)
        self.assertEqual(funnel["counts"]["ready"], 1)
        self.assertEqual(funnel["counts"]["order_rejected"], 1)

    def test_market_decision_funnel_keeps_shadow_quality_when_globally_blocked(self):
        decisions = [
            {"symbol": "600000", "stage": "CANDIDATE"},
            {"symbol": "600000", "stage": "A_SHARE_REGIME_BLOCKED"},
            {"symbol": "600000", "stage": "SHADOW_TECHNICAL_READY"},
        ]
        funnel = build_market_decision_funnel(decisions, [])
        self.assertEqual(funnel["counts"]["global_blocked"], 1)
        self.assertEqual(funnel["counts"]["a_share_regime_blocked"], 1)
        self.assertEqual(funnel["counts"]["shadow_ready"], 1)
        self.assertIn("影子评估技术合格 1 只", funnel["summary"])


if __name__ == "__main__":
    unittest.main()
