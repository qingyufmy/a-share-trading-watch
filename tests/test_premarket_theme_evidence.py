import unittest

from premarket_report import (
    build_premarket_theme_evidence,
    premarket_candidate_score,
    premarket_theme_keywords,
    stock_context_theme_labels,
)


def focus(*, fupan="", zaopan="", zhangting=""):
    return {
        "fupan": {"main_themes": fupan, "active_thread": "", "summary": "", "themes": []},
        "zaopan": {"sections": {"重大新闻汇总": [zaopan] if zaopan else []}},
        "zhangting": {"items": [{"title": zhangting, "stocks": []}] if zhangting else []},
        "bidu_special": {},
    }


class PremarketThemeEvidenceTests(unittest.TestCase):
    def test_company_research_text_supplies_liquid_cooling_identity(self):
        labels = stock_context_theme_labels(
            {
                "news": [{"title": "液冷龙头历史新高", "abstract": "AI算力服务器热管理"}],
                "notice": [],
            },
            {"profile": {"business": "汽车零部件"}},
        )
        self.assertIn("液冷", labels)
        self.assertIn("AI算力", labels)
        self.assertIn("热管理", labels)

    def test_cross_source_theme_with_prior_board_becomes_main_watch(self):
        ths_focus = focus(fupan="算力和CPO成为市场主线", zaopan="数据中心需求延续", zhangting="涨停雷达：光通信")
        news = {"财经要闻": [{"title": "服务器产业链订单增长", "summary": ""}]}
        evidence = build_premarket_theme_evidence(ths_focus, news, [], [{"f14": "CPO"}])
        ai = next(item for item in evidence if item["theme"] == "新质生产力/AI")
        self.assertEqual(ai["tier"], "MAIN")
        self.assertIn("boards", ai["sources"])
        self.assertGreaterEqual(ai["source_count"], 3)

    def test_tactical_themes_are_not_collapsed_into_generic_material_or_ai_buckets(self):
        ths_focus = focus(
            fupan="液冷和培育钻石领涨，白银价格走强",
            zaopan="热管理、超硬材料和贵金属继续活跃",
            zhangting="涨停雷达：先进散热与金刚石",
        )
        evidence = build_premarket_theme_evidence(
            ths_focus,
            {"财经要闻": [{"title": "银价与铜价上涨", "summary": ""}]},
            [],
            [{"f14": "液冷"}, {"f14": "培育钻石"}, {"f14": "贵金属"}],
        )
        by_theme = {item["theme"]: item for item in evidence}
        self.assertEqual(by_theme["液冷/先进散热"]["tier"], "MAIN")
        self.assertEqual(by_theme["超硬材料/培育钻石"]["tier"], "MAIN")
        self.assertEqual(by_theme["贵金属/有色涨价"]["tier"], "MAIN")
        self.assertNotIn("培育钻石", by_theme.get("材料/新能源", {}).get("keywords", []))

    def test_one_headline_remains_clue_and_cannot_feed_policy_candidate_keywords(self):
        ths_focus = focus()
        news = {"财经要闻": [{"title": "先进封装设备获订单", "summary": ""}]}
        evidence = build_premarket_theme_evidence(ths_focus, news, [], [])
        chip = next(item for item in evidence if item["theme"] == "国产替代/半导体")
        self.assertEqual(chip["tier"], "CLUE")
        keys = premarket_theme_keywords(ths_focus, news, [], [])
        self.assertNotIn("国产替代/半导体", keys)
        self.assertNotIn("先进封装", keys)

    def test_evidence_bonus_orders_watch_queue_without_making_a_trade_signal(self):
        quote = {
            "code": "600000", "name": "测试电子", "industry": "电子元件", "concepts": "",
            "close": 10.4, "high": 10.6, "low": 10.0, "open": 10.1, "pct": 3.0, "amount": 4_000_000_000,
        }
        keys = ["新质生产力/AI", "AI", "算力", "CPO"]
        main = [{"theme": "新质生产力/AI", "tier": "MAIN"}]
        base_score, *_ = premarket_candidate_score(quote, keys, "电子", [])
        evidence_score, matched, *_ = premarket_candidate_score(quote, keys, "电子", main)
        self.assertTrue(matched)
        self.assertAlmostEqual(evidence_score - base_score, 0.7)


if __name__ == "__main__":
    unittest.main()
