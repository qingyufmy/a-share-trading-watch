import sqlite3
import unittest
from datetime import datetime
from tempfile import TemporaryDirectory

from core import opportunity_rating
import paper_trading
import realtime_signal_engine as engine


class StrategyRemediationTests(unittest.TestCase):
    def test_group_leader_is_promoted_for_deep_check_not_direct_buy(self):
        details = {
            "groups": [{
                "name": "策略盘",
                "observation_codes": ["600313", "600000"],
            }],
        }
        quotes = {
            "600313": {"name": "农发种业", "close": 7.90, "pct": 10.03, "amount_wan": 160000},
            "600000": {"name": "测试", "close": 10.20, "pct": 1.20, "amount_wan": 90000},
        }
        metas = engine.build_observation_group_candidate_metas(details, quotes)
        self.assertEqual([item["code"] for item in metas], ["600313", "600000"])
        self.assertEqual(metas[0]["status"], "GROUP_PROMOTED_DEEP_CHECK")
        self.assertFalse(metas[0].get("premarket_plan_allows_entry", False))

    def test_universe_gate_converts_only_entry_to_wait_and_preserves_exit(self):
        entry = {
            "scenario": "STRATEGY_520_ENTRY",
            "priority": "P1", "external_status": "立即处理", "action": "BUY_PROBE",
            "timing_v2": {"entry_allowed": True, "blockers": []}, "reasons": [],
        }
        exit_signal = {"scenario": "V2_REDUCE", "timing_v2": {"entry_allowed": False}, "reasons": []}
        result = engine.block_new_entries_for_universe([entry, exit_signal], False, "同步失败")
        self.assertEqual(result[0]["scenario"], "V2_WAIT")
        self.assertFalse(result[0]["timing_v2"]["entry_allowed"])
        self.assertEqual(result[1]["scenario"], "V2_REDUCE")

    def test_v2_wait_is_recorded_in_market_decision_ledger(self):
        with TemporaryDirectory() as tmp:
            old_db, old_date = engine.STATE_DB, engine.intra.REPORT_DATE
            try:
                engine.STATE_DB = __import__("pathlib").Path(tmp) / "signal.sqlite"
                engine.intra.REPORT_DATE = "2026-09-01"
                con = engine.init_db()
                signal = {
                    "symbol": "600313", "name": "农发种业", "current_price": 7.9,
                    "scenario": "V2_WAIT", "reasons": ["15分钟等待确认"],
                    "timing_v2": {
                        "regime": "TREND", "location": "STRUCTURAL_RECLAIM",
                        "blockers": ["15分钟等待确认"],
                        "data_quality": {"minute_available": True, "closed_5m_bars": 6, "closed_15m_bars": 24},
                    },
                }
                rating = opportunity_rating.rate_signal(signal)
                engine.record_v2_decision(con, signal, rating, now=datetime(2026, 9, 1, 10, 0))
                row = con.execute("SELECT stage, reason FROM market_decision_events").fetchone()
                con.close()
            finally:
                engine.STATE_DB, engine.intra.REPORT_DATE = old_db, old_date
        self.assertEqual(row[0], "V2_WAIT")
        self.assertIn("15分钟", row[1])

    def test_stale_losing_position_requires_review_without_creating_sell(self):
        review = paper_trading.position_review_status(23, -4.17, 0.2)
        self.assertEqual(review["level"], "REVIEW_REQUIRED")
        self.assertIn("复核", review["reason"])


if __name__ == "__main__":
    unittest.main()
