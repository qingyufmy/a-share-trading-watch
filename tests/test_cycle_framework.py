import unittest

from core import cycle_framework


def rising_daily(days=80):
    rows = []
    for index in range(days):
        close = 10 + index * 0.12
        rows.append({
            "date": f"2026-{'01' if index < 31 else '02' if index < 59 else '03'}-{index % 28 + 1:02d}",
            "open": close - 0.08,
            "close": close,
            "high": close + 0.18,
            "low": close - 0.20,
            "amount_wan": 1000 + index * 4,
        })
    return rows


class CycleFrameworkTests(unittest.TestCase):
    def test_post_close_plan_is_ranking_only_and_keeps_high_j_as_protection(self):
        rows = rising_daily()
        context = cycle_framework.build_plan_context(rows, {"close": rows[-1]["close"]})
        self.assertTrue(context["plan_only"])
        self.assertIn("monthly", context)
        self.assertNotIn("weekly", context)
        self.assertEqual(context["volume"]["role"], "ranking_only")
        self.assertIn("月线定大势", context["execution_rule"])
        self.assertIn(context["time"]["state"], {"normal", "protection"})
        if context["time"]["state"] == "protection":
            self.assertIn("高位时间保护区", cycle_framework.execution_blockers(context)[0])

    def test_missing_ledger_cannot_open_market_radar_position(self):
        self.assertIn("缺少盘后", cycle_framework.execution_blockers({})[0])

    def test_distribution_proxy_blocks_new_entry(self):
        rows = rising_daily()
        rows[-1]["close"] = rows[-2]["close"] * 0.95
        rows[-1]["amount_wan"] = rows[-2]["amount_wan"] * 2.0
        context = cycle_framework.build_plan_context(rows, {"close": rows[-1]["close"]})
        self.assertEqual(context["chips"]["state"], "distribution")
        self.assertIn("筹码代理", "；".join(cycle_framework.execution_blockers(context)))
