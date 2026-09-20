import unittest
from unittest.mock import patch

import after_close_report as base

from after_close_report import (
    merge_watchlist_groups,
    observation_group_summary,
    observation_group_summary_lines,
    parse_watchlist_payload,
    premarket_plan_observation_details,
    previous_trading_date,
    snapshot_watchlist_rows,
    stock_symbol,
    watchlist_sync_changes,
    watchlist_sync_summary,
    watchlist_sync_entry_gate,
)


class PriorityWatchlistTests(unittest.TestCase):
    def test_previous_trading_date_skips_weekend(self):
        self.assertEqual(previous_trading_date("2026-09-07"), "2026-09-04")

    def test_all_self_selected_groups_are_included_in_premarket_plan(self):
        details = {
            "status": "ready", "source": "tonghuashun_custom_groups",
            "rows": [("600186", "17"), ("300413", "33")],
            "groups": [
                {"name": "策略盘", "observation_codes": ["600186"]},
                {"name": "CPO", "observation_codes": ["300413"]},
            ],
        }
        with patch.object(base, "PREMARKET_PLAN_OBSERVATION_GROUPS", ("ALL",)):
            selected = premarket_plan_observation_details(details)
        self.assertEqual(selected["selection"], "all_groups")
        self.assertEqual(selected["rows"], [("600186", "17"), ("300413", "33")])
        self.assertEqual([item["name"] for item in selected["groups"]], ["策略盘", "CPO"])

    def test_priority_groups_merge_without_losing_memberships(self):
        rows, memberships = merge_watchlist_groups([
            ("我的股票", [("000001", "33"), ("600000", "17")]),
            ("观察池", [("600000", "17"), ("300001", "33")]),
        ])

        self.assertEqual(rows, [("000001", "33"), ("600000", "17"), ("300001", "33")])
        self.assertEqual(memberships["600000"], ["我的股票", "观察池"])

    def test_sync_changes_keep_group_level_add_and_remove(self):
        previous = {
            "rows": [{"code": "000001"}, {"code": "600000"}],
            "groups": [
                {"name": "我的股票", "codes": ["000001"]},
                {"name": "观察池", "codes": ["600000"]},
            ],
        }
        groups = [
            {"name": "我的股票", "codes": ["000001", "300001"]},
            {"name": "观察池", "codes": []},
        ]
        changes = watchlist_sync_changes(
            [("000001", "33"), ("300001", "33")],
            groups,
            previous,
        )

        self.assertEqual(changes["added"], ["300001"])
        self.assertEqual(changes["removed"], ["600000"])
        self.assertEqual(changes["group_changes"]["我的股票"]["added"], ["300001"])
        self.assertEqual(changes["group_changes"]["观察池"]["removed"], ["600000"])

    def test_summary_marks_partial_source_as_degraded(self):
        summary = watchlist_sync_summary({
            "rows": [("000001", "33")],
            "status": "degraded",
            "groups": [{"name": "我的股票", "count": 1}],
            "errors": ["观察池读取失败：文件不存在"],
        })

        self.assertIn("同步degraded", summary)
        self.assertIn("观察池读取失败", summary)

    def test_summary_never_describes_fallback_as_verified_sync(self):
        summary = watchlist_sync_summary({
            "rows": [("000001", "33")],
            "status": "fallback_unverified",
            "groups": [],
            "errors": ["我的股票读取失败：权限不足"],
        })

        self.assertIn("未完成同步", summary)
        self.assertIn("无法确认", summary)

    def test_unverified_fallback_cannot_authorize_new_entries(self):
        allowed, reason = watchlist_sync_entry_gate({
            "rows": [("600313", "17")],
            "status": "fallback_unverified",
            "errors": ["权限不足"],
        })
        self.assertFalse(allowed)
        self.assertIn("禁止新增仓", reason)

    def test_recent_verified_snapshot_can_bridge_launchd_container_permission(self):
        allowed, reason = watchlist_sync_entry_gate({
            "rows": [("600313", "17")],
            "status": "snapshot_verified",
            "updated_at": "2026-09-01 08:20:00",
        }, now=__import__("datetime").datetime(2026, 9, 1, 9, 0))
        self.assertTrue(allowed)
        self.assertIn("已验证快照", reason)

    def test_observation_snapshot_rows_are_normalized_for_quote_coverage(self):
        rows = snapshot_watchlist_rows([
            {"code": "600313", "market": "17"},
            {"code": "300143", "market": "33"},
        ])

        self.assertEqual(rows, [("600313", "17"), ("300143", "33")])

    def test_september_observation_group_is_selected_for_premarket_plan(self):
        details = {
            "status": "ready",
            "source": "tonghuashun_custom_groups",
            "rows": [("600186", "17"), ("300408", "33")],
            "groups": [{
                "name": "9月观察池",
                "observation_codes": ["600186", "300408"],
            }],
        }

        selected = premarket_plan_observation_details(details)

        self.assertEqual(selected["rows"], [("600186", "17"), ("300408", "33")])
        self.assertEqual(selected["groups"][0]["name"], "9月观察池")

    def test_observation_summary_is_descriptive_and_keeps_core_symbols_out(self):
        details = {
            "groups": [{
                "name": "策略盘",
                "observation_codes": ["600313", "920808"],
                "core_overlap": ["000831"],
            }],
        }
        quotes = {
            "600313": {"name": "农发种业", "close": 7.9, "pct": 10.03},
            "920808": {"name": "九菱科技", "close": 18.0, "pct": -2.1},
        }

        summary = observation_group_summary(details, quotes)
        rendered = "\n".join(observation_group_summary_lines(summary))

        self.assertIn("策略盘", rendered)
        self.assertIn("农发种业 10.03%", rendered)
        self.assertIn("九菱科技 -2.10%", rendered)
        self.assertIn("已去重", rendered)

    def test_parser_and_quote_symbol_support_beijing_exchange_codes(self):
        rows = parse_watchlist_payload(b"920808|600313|,151|17|")

        self.assertEqual(rows, [("920808", "151"), ("600313", "17")])
        self.assertEqual(stock_symbol("920808"), "bj920808")


if __name__ == "__main__":
    unittest.main()
