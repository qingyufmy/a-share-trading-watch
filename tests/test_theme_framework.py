import unittest
from unittest.mock import patch

from core import global_risk
from core import theme_validation
import intraday_report as intra
import realtime_signal_engine as engine
import render_report_dashboard as dashboard


class ThemeFrameworkEvidenceTests(unittest.TestCase):
    def test_semiconductor_cannot_match_medical_board_from_raw_concepts(self):
        quote = {
            "code": "600206",
            "name": "有研新材",
            "industry": "半导体",
            "concepts": "医疗 创新药",  # provider field is deliberately untrusted
            "close": 20.0,
            "open": 19.8,
            "high": 20.2,
            "low": 19.6,
            "pct": 1.0,
            "amount_wan": 500000,
        }
        boards = [{"f14": "医疗服务", "f3": 2.5}, {"f14": "创新药", "f3": 3.1}]
        evidence = theme_validation.candidate_theme_evidence(quote, boards)
        self.assertFalse(evidence["valid"])

        _, matched, _ = intra.radar_candidate_score(quote, ["医疗", "创新药"], boards=boards)
        self.assertEqual(matched, [])
        self.assertFalse(quote["theme_evidence"]["valid"])

    def test_same_family_industry_and_board_is_retained(self):
        quote = {"name": "泓博医药", "industry": "医疗服务"}
        boards = [{"f14": "创新药", "f3": 2.8}]
        evidence = theme_validation.candidate_theme_evidence(quote, boards)
        self.assertTrue(evidence["valid"])
        self.assertIn("医疗", evidence["labels"])

    def test_media_industry_matches_live_media_board_family(self):
        quote = {"name": "龙版传媒", "industry": "出版", "concepts": "AI应用"}
        boards = [{"f14": "文字媒体", "f3": 5.6}]
        evidence = theme_validation.candidate_theme_evidence(quote, boards)
        self.assertTrue(evidence["valid"])
        self.assertIn("传媒文娱", evidence["labels"])

    def test_unmapped_industry_can_use_company_context_for_liquid_cooling_board(self):
        quote = {
            "name": "飞龙股份",
            "industry": "交运设备-汽车-汽车零部件",
            "concepts": ["液冷", "AI算力", "热管理"],
        }
        boards = [{"f14": "液冷服务器", "f3": 3.2}]
        evidence = theme_validation.candidate_theme_evidence(quote, boards)
        self.assertTrue(evidence["valid"])
        self.assertIn("科技", evidence["labels"])

    def test_persisted_conflicting_framework_is_not_replayed(self):
        focus = "全市场框架筛选：半导体｜题材匹配：医疗｜强度评分：8.8"
        validation = theme_validation.framework_evidence_from_text(focus)
        self.assertFalse(validation["valid"])
        meta = engine.candidate_meta_from_report_row(
            {"机会代码": "600206", "机会名称": "有研新材", "框架依据": focus},
            "report:test",
            1,
        )
        self.assertEqual(meta["matched"], [])
        self.assertFalse(meta["framework_validation"]["valid"])

    def test_tracked_candidate_rechecks_framework_industry_against_live_board(self):
        meta = engine.candidate_meta_from_report_row(
            {
                "机会代码": "603127",
                "机会名称": "测试医药",
                "框架依据": "全市场框架筛选：医疗服务｜主线匹配：创新药｜强度评分：8.8",
            },
            "report:test",
            1,
        )
        items = engine.build_tracked_candidate_items(
            [meta],
            {"603127": {"name": "测试医药", "close": 12.0, "high": 12.1, "low": 11.8}},
            boards=[{"f14": "创新药", "f3": 2.8}],
        )
        self.assertEqual(items[0]["quote"]["industry"], "医疗服务")
        self.assertTrue(items[0]["quote"]["theme_evidence"]["valid"])

    def test_sector_mapping_prefers_specific_industry_board_over_stronger_family_board(self):
        momentum = intra.sector_momentum_for_candidate(
            {"industry": "半导体", "name": "测试芯片"},
            boards=[
                {"f14": "通信线缆及配套", "f3": 5.4},
                {"f14": "半导体材料", "f3": 3.7},
            ],
        )
        self.assertEqual(momentum["board_name"], "半导体材料")

    def test_sector_mapping_does_not_use_only_a_broad_family(self):
        momentum = intra.sector_momentum_for_candidate(
            {"industry": "半导体", "name": "测试芯片"},
            boards=[{"f14": "横向通用软件", "f3": 8.1}],
        )
        self.assertEqual(momentum["board_name"], "")
        self.assertFalse(momentum["emotion_ok"])

    def test_sector_mapping_prefers_exact_board_over_stronger_containment(self):
        momentum = intra.sector_momentum_for_candidate(
            {"industry": "出版", "name": "测试传媒"},
            boards=[
                {"f14": "大众出版", "f3": 6.2},
                {"f14": "出版", "f3": 4.1},
            ],
        )
        self.assertEqual(momentum["board_name"], "出版")

    def test_sector_mapping_can_use_an_explicit_provider_concept(self):
        momentum = intra.sector_momentum_for_candidate(
            {"industry": "汽车零部件", "concepts": "液冷服务器、飞行汽车"},
            boards=[{"f14": "液冷服务器", "f3": 3.8}],
        )
        self.assertEqual(momentum["board_name"], "液冷服务器")

    def test_fast_board_fetch_keeps_complete_provider_response(self):
        boards = [{"f14": f"板块{i}", "f3": 3.0} for i in range(45)]
        with patch.object(intra.base, "fetch_boards", return_value=(boards, {})):
            self.assertEqual(len(intra.fetch_fast_boards()), 45)

    def test_generic_equipment_to_oil_theme_is_evidence_insufficient_not_conflict(self):
        validation = theme_validation.framework_evidence_from_text(
            "全市场框架筛选：专用设备｜题材匹配：油气｜强度评分：7.0"
        )
        self.assertTrue(validation["valid"])
        self.assertFalse(validation["executable"])

    def test_validated_theme_prevents_raw_concept_from_changing_risk_bucket(self):
        row = {
            "industry": "医疗服务",
            "theme_evidence": {"valid": True, "labels": ["医疗"]},
            "quote": {"name": "测试医药", "industry": "医疗服务", "concepts": "半导体 芯片"},
        }
        self.assertEqual(global_risk.candidate_risk_bucket(row), "other")

    def test_dashboard_conflict_is_observation_only(self):
        row = dashboard.enrich_market_opportunity({
            "机会代码": "600206",
            "机会名称": "有研新材",
            "框架依据": "全市场框架筛选：半导体｜题材匹配：医疗｜强度评分：8.8",
            "雷达状态": "🟢 替代机会",
            "触发价": "20.00",
            "失效价": "19.60",
            "现价": "20.10",
        })
        dashboard.apply_market_opportunity_gate([row], {"risk_level": "green", "policy": {"allow_market_opportunity_buy": True}})
        self.assertIn("框架依据冲突", row["模拟门控"])
        self.assertEqual(row["雷达状态"], "🔴 框架依据冲突")
        self.assertEqual(row["框架依据"], "框架依据冲突（不参与候选）")
        alert = dashboard.entry_alert_from_market_opportunity(row, {"risk_level": "green", "policy": {"allow_market_opportunity_buy": True}})
        self.assertEqual(alert["label"], "框架证据待重核")
        self.assertNotIn("技术门控未通过", alert["action"])

    def test_framework_selector_alone_is_not_a_trend_label(self):
        style, _ = engine.framework_style_for_row({
            "state": "雷达观察",
            "focus": "全市场框架筛选：半导体｜主线匹配：科技",
            "quote": {"pct": 0, "amount_wan": 0},
        })
        self.assertEqual(style, "未分类")

    def test_generic_technical_gate_reason_is_not_repeated(self):
        action = dashboard.compact_market_gate_action(
            "技术门控未通过：技术门控未通过；等待回踩确认。",
            "技术门控未通过",
        )
        self.assertEqual(action, "技术门控未通过：等待量价/形态确认；等待回踩确认。")


if __name__ == "__main__":
    unittest.main()
