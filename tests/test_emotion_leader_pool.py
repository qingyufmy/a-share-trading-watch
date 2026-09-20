import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from core import emotion_leader_pool as pool


class EmotionLeaderPoolTests(unittest.TestCase):
    def response(self):
        columns = [
            {"key": "GUBA_TOP_REAL_TIME{2026-09-03}", "indexName": "GUBA_TOP_REAL_TIME", "dateMsg": "2026.09.03"},
        ]
        rows = [
            {
                "SERIAL": "10", "SECURITY_CODE": "002536", "SECURITY_SHORT_NAME": "飞龙股份",
                "NEWEST_PRICE": "63.03", "CHG": "8.54", "PEAK_PRICE<140>": "63.88", "BOTTOM_PRICE<140>": "57.42",
                "TURNOVER_RATE": "16.79", "QRR": "1.60", "TRADING_VOLUMES": "56.06亿",
                "CIRCULATION_MARKET_VALUE<140>": "343.72亿", "TOAL_MARKET_VALUE<140>": "387.10亿",
                "GUBA_TOP_REAL_TIME{2026-09-03}": "10",
            },
            {
                "SERIAL": "12", "SECURITY_CODE": "000892", "SECURITY_SHORT_NAME": "欢瑞世纪",
                "NEWEST_PRICE": "5.07", "CHG": "-2.31", "PEAK_PRICE<140>": "5.72", "BOTTOM_PRICE<140>": "5.00",
                "TURNOVER_RATE": "40.47", "QRR": "7.89", "TRADING_VOLUMES": "15.65亿",
                "CIRCULATION_MARKET_VALUE<140>": "36.11亿", "TOAL_MARKET_VALUE<140>": "50.00亿",
                "GUBA_TOP_REAL_TIME{2026-09-03}": "12",
            },
            {
                "SERIAL": "3", "SECURITY_CODE": "003040", "SECURITY_SHORT_NAME": "楚天龙",
                "NEWEST_PRICE": "20.00", "CHG": "-9.09", "PEAK_PRICE<140>": "22.00", "BOTTOM_PRICE<140>": "19.50",
                "TURNOVER_RATE": "25", "QRR": "2", "TRADING_VOLUMES": "20亿",
                "CIRCULATION_MARKET_VALUE<140>": "90亿", "TOAL_MARKET_VALUE<140>": "100亿",
                "GUBA_TOP_REAL_TIME{2026-09-03}": "3",
            },
        ]
        return {"code": "100", "data": {"quoteTime": "2026-09-03 15:00:00", "result": {"columns": columns, "dataList": rows}}}

    def test_normalize_and_select_keeps_flydragon_not_falling_popularity(self):
        snapshot = pool.normalize_eastmoney_response(self.response())
        candidates = pool.select_leader_candidates(snapshot, {
            "source_date": "2026-09-03",
            "rows": [{"rank": 1, "code": "002536", "theme": "液冷", "list_type": "盘中人气榜"}],
        })
        self.assertEqual(snapshot["source_date"], "2026-09-03")
        self.assertEqual([item["code"] for item in candidates], ["002536"])
        self.assertTrue(candidates[0]["cross_verified"])
        self.assertIn("开盘啦盘中人气榜第1", candidates[0]["reasons"])

    def test_plan_merge_adds_candidates_without_duplicate_rows(self):
        snapshot = pool.normalize_eastmoney_response(self.response())
        snapshot["candidates"] = pool.select_leader_candidates(snapshot)
        plan, metadata = pool.merge_into_plan({"rows": [("002536", "33")], "groups": []}, snapshot)
        self.assertEqual(plan["rows"], [("002536", "33")])
        self.assertEqual(plan["groups"][0]["name"], pool.GROUP_NAME)
        self.assertIn("002536", metadata)

    def test_failed_limit_top_rank_cross_verified_name_enters_reversal_profile(self):
        snapshot = pool.normalize_eastmoney_response(self.response())
        candidates = pool.select_leader_candidates(snapshot, {
            "source_date": "2026-09-03",
            "rows": [{"rank": 6, "code": "000892", "theme": "AI应用", "list_type": "复盘人气榜"}],
        })
        candidate = next(item for item in candidates if item["code"] == "000892")
        self.assertEqual(candidate["leader_profile"], "FAILED_LIMIT_REVERSAL")
        self.assertTrue(candidate["cross_verified"])
        self.assertIn("触板回落次日弱转强候选", candidate["reasons"])

    def test_stale_kaipanla_snapshot_cannot_raise_candidate_score(self):
        snapshot = pool.normalize_eastmoney_response(self.response())
        with TemporaryDirectory() as temp:
            runtime = Path(temp) / "data" / "runtime"
            runtime.mkdir(parents=True)
            (runtime / "kaipanla_popularity_latest.json").write_text(
                '{"source_date":"2026-09-02","rows":[{"rank":1,"code":"002536"}]}',
                encoding="utf-8",
            )
            built = pool.build_snapshot(Path(temp), raw=snapshot)
        flydragon = next(item for item in built["candidates"] if item["code"] == "002536")
        self.assertFalse(flydragon["cross_verified"])
        self.assertFalse(built["kaipanla"]["available"])

    def test_snapshot_round_trip(self):
        snapshot = pool.normalize_eastmoney_response(self.response())
        snapshot["candidates"] = pool.select_leader_candidates(snapshot)
        with TemporaryDirectory() as temp:
            path = pool.write_snapshot(Path(temp), snapshot)
            self.assertTrue(path.exists())
            self.assertEqual(pool.load_latest_snapshot(Path(temp))["source_date"], "2026-09-03")


if __name__ == "__main__":
    unittest.main()
