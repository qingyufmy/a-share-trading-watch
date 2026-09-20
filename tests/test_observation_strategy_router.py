import unittest
from unittest.mock import patch

from core import observation_strategy_router as router


def daily_rows(closes, lows=None, highs=None, pcts=None, amounts=None):
    lows = lows or [value * 0.995 for value in closes]
    highs = highs or [value * 1.005 for value in closes]
    pcts = pcts or [1.0] * len(closes)
    amounts = amounts or [100.0] * len(closes)
    return [
        {"close": close, "low": low, "high": high, "pct": pct, "amount_wan": amount}
        for close, low, high, pct, amount in zip(closes, lows, highs, pcts, amounts)
    ]


def observation_stock(code, closes, quote, **extra):
    return {
        "observation_plan_group": "9月观察池",
        "daily": daily_rows(closes, **extra),
        "quote": {"code": code, "industry": "测试行业", "concepts": "测试题材", **quote},
        "tech": extra.get("tech", {}),
    }


class ObservationStrategyRouterTests(unittest.TestCase):
    def test_daily_amount_ratio_ignores_partial_realtime_quote(self):
        closes = [9.5] * 55 + [10.5] * 10
        stock = observation_stock(
            "600002",
            closes,
            {"close": 10.8, "low": 10.0, "pct": 2.8, "amount_wan": 1.0},
            lows=[9.4] * 55 + [10.0] * 10,
            amounts=[100.0] * 64 + [120.0],
        )
        contract = router.classify_stock(stock)
        self.assertEqual(contract["key"], router.TREND_520)
        self.assertTrue(contract["daily_qualified"])
        self.assertAlmostEqual(contract["daily_metrics"]["amount_ratio_5d"], 1.2)
        self.assertEqual(contract["daily_metrics"]["amount_ratio_source"], "last_completed_daily_bar")

    def test_leader_context_keeps_institutional_and_ordinary_seats_separate(self):
        institutional = router._leader_context_evidence("龙虎榜：2家机构净买入1.72亿元")
        ordinary = router._leader_context_evidence("龙虎榜：普通席位净买2.43亿元")
        generic = router._leader_context_evidence("该股今日登上龙虎榜")
        self.assertTrue(institutional["institutional_net_buy"])
        self.assertFalse(institutional["ordinary_seat_net_buy"])
        self.assertFalse(ordinary["institutional_net_buy"])
        self.assertTrue(ordinary["ordinary_seat_net_buy"])
        self.assertFalse(any(generic.values()))

    def test_limit_up_is_only_a_leader_candidate_and_allows_e5(self):
        stock = observation_stock(
            "600001",
            [10.0] * 70,
            {"close": 10.0, "low": 9.9, "pct": 10.0, "amount_wan": 160.0, "turnover": 12.0},
            pcts=[0.2] * 69 + [10.0],
        )
        stock['daily'][-1]['turnover'] = 12.0
        contract = router.classify_stock(stock)
        self.assertEqual(contract["key"], router.LEADER)
        self.assertEqual(
            contract["allowed_patterns"],
            ["V2_E5B_LEADER_OPENING_HOLD", "V2_E5_LEADER_SECOND_LEG"],
        )
        self.assertNotIn("E5A", contract["formal_entry_signal"])
        self.assertNotIn("E5A", contract["reason"])
        self.assertFalse(router.execution_gate(contract, "V2_E1_TREND_PULLBACK_RECLAIM")[0])
        self.assertTrue(router.execution_gate(contract, "V2_E5_LEADER_SECOND_LEG")[0])
        self.assertTrue(router.execution_gate(contract, "V2_E5B_LEADER_OPENING_HOLD")[0])
        self.assertEqual(router.entry_scenario(contract, "V2_E5_LEADER_SECOND_LEG"), "STRATEGY_LEADER_ENTRY")

    def test_limit_up_mid_cap_with_broad_trading_evidence_is_a_leader_candidate(self):
        stock = observation_stock(
            "002536",
            [58.07] * 70,
            {
                "close": 58.07,
                "low": 55.0,
                "pct": 10.0,
                "amount_wan": 54000.0,
                "volume_lot": 900000.0,
                "turnover": 15.6,
                "concepts": ["液冷", "AI算力"],
            },
            pcts=[0.2] * 69 + [10.0],
            amounts=[50000.0] * 69 + [54000.0],
        )
        stock['daily'][-1].update(turnover=15.6, volume_lot=900000.0)
        contract = router.classify_stock(stock)
        self.assertEqual(contract["key"], router.LEADER)
        self.assertGreaterEqual(contract["daily_metrics"]["leader_evidence_score"], 4)
        self.assertLess(contract["daily_metrics"]["amount_ratio_5d"], 1.5)
        self.assertLess(contract["daily_metrics"]["float_market_cap_yi"], 500.0)

    def test_external_popularity_candidate_routes_to_leader_without_close_limit(self):
        stock = observation_stock(
            "002536",
            [58.07] * 70,
            {
                "close": 63.03,
                "low": 57.42,
                "pct": 8.54,
                "amount_wan": 560600.0,
                "turnover": 16.79,
                "float_market_cap_yi": 343.72,
                "concepts": ["液冷"],
            },
            pcts=[0.2] * 70,
            amounts=[300000.0] * 69 + [560600.0],
        )
        stock["emotion_leader_candidate"] = {
            "qualified": True,
            "rank": 10,
            "score": 12,
            "cross_verified": True,
            "source_date": "2026-09-03",
            "reasons": ["东财人气第10", "开盘啦复盘人气榜第11", "液冷"],
        }
        contract = router.classify_stock(stock)
        self.assertEqual(contract["key"], router.LEADER)
        self.assertTrue(contract["daily_qualified"])
        self.assertEqual(contract["daily_metrics"]["emotion_pool_rank"], 10)
        self.assertIn("开盘啦：已交叉验证", contract["reason"])

    def test_failed_limit_leader_contract_adds_only_the_restricted_e5a_route(self):
        stock = observation_stock(
            "000892",
            [5.08] * 70,
            {"close": 5.07, "low": 5.0, "pct": -2.31, "amount_wan": 156500.0, "turnover": 40.47},
            pcts=[0.2] * 70,
            amounts=[100000.0] * 69 + [156500.0],
        )
        stock["emotion_leader_candidate"] = {
            "qualified": True, "rank": 12, "score": 14, "cross_verified": True,
            "source_date": "2026-09-03", "leader_profile": "FAILED_LIMIT_REVERSAL",
            "touched_limit": True, "closed_limit": False,
        }
        contract = router.classify_stock(stock)
        self.assertEqual(contract["key"], router.LEADER)
        self.assertIn("V2_E5A_LEADER_OPENING_REVERSAL", contract["allowed_patterns"])
        self.assertNotIn("V2_E5B_LEADER_OPENING_HOLD", contract["allowed_patterns"])
        self.assertIn("E5A", contract["formal_entry_signal"])
        self.assertIn("E5A", contract["reason"])
        self.assertEqual(contract["daily_metrics"]["leader_profile"], "FAILED_LIMIT_REVERSAL")

    def test_core_watchlist_name_must_also_receive_a_named_method(self):
        stock = {
            "daily": daily_rows([10.0] * 40),
            "quote": {"code": "600000", "close": 10.0, "industry": "银行"},
            "tech": {},
        }
        contract = router.classify_stock(stock)
        self.assertNotEqual(contract["key"], router.CORE_V2)
        self.assertTrue(contract["is_observation_strategy"])

    def test_legacy_or_missing_contract_fails_closed(self):
        missing = router.contract_from_level({"code": "600000"})
        legacy = router.contract_from_level({"code": "600000", "strategy_key": router.CORE_V2})
        for contract in (missing, legacy):
            self.assertEqual(contract["key"], router.OBSERVE)
            self.assertFalse(contract["daily_qualified"])
            self.assertFalse(router.daily_qualification_gate(contract)[0])
            self.assertFalse(router.execution_gate(contract, "V2_E1_TREND_PULLBACK_RECLAIM")[0])

    def test_limit_up_with_only_thin_turnover_and_theme_label_stays_unclassified(self):
        stock = observation_stock(
            "600005",
            [10.0] * 70,
            {
                "close": 10.0,
                "low": 9.8,
                "pct": 10.0,
                "amount_wan": 60000.0,
                "volume_lot": 10000.0,
                "turnover": 2.0,
            },
            pcts=[0.2] * 69 + [10.0],
            amounts=[100000.0] * 69 + [60000.0],
        )
        contract = router.classify_stock(stock)
        self.assertEqual(contract["key"], router.OBSERVE)
        self.assertFalse(contract["daily_qualified"])

    def test_ma20_reclaim_routes_to_520(self):
        closes = [9.5] * 55 + [10.5] * 10
        stock = observation_stock(
            "600002",
            closes,
            {"close": 10.5, "low": 10.0, "pct": 1.9, "amount_wan": 120.0},
            lows=[9.4] * 55 + [10.0] * 10,
        )
        contract = router.classify_stock(stock)
        self.assertEqual(contract["key"], router.TREND_520)
        self.assertTrue(router.execution_gate(contract, "V2_E3_MA20_STRUCTURAL_RECLAIM")[0])
        self.assertEqual(router.entry_scenario(contract, "V2_E3_MA20_STRUCTURAL_RECLAIM"), "STRATEGY_520_ENTRY")

    def test_520_keeps_method_classification_but_blocks_entry_without_kdj(self):
        closes = [9.5] * 55 + [10.5] * 10
        stock = observation_stock(
            "600002",
            closes,
            {"close": 10.5, "low": 10.0, "pct": 1.9, "amount_wan": 120.0},
            lows=[9.4] * 55 + [10.0] * 10,
        )
        with patch.object(router, "_kdj_metrics", return_value={"k": 45.0, "d": 55.0, "j": 25.0, "k_cross_up": False, "bullish": False, "not_overheated": True}):
            contract = router.classify_stock(stock)
        self.assertEqual(contract["key"], router.TREND_520)
        self.assertFalse(contract["daily_qualified"])
        self.assertIn("KDJ", contract["daily_gate_reason"])

    def test_kdj_metrics_identifies_bullish_non_overheated_confirmation(self):
        highs = [10.1 + index * 0.1 for index in range(20)]
        lows = [9.8 + index * 0.1 for index in range(20)]
        closes = [9.95 + index * 0.1 for index in range(20)]
        metrics = router._kdj_metrics(highs, lows, closes)
        self.assertTrue(metrics["bullish"])
        self.assertTrue(metrics["not_overheated"])

    def test_ma5_first_pullback_routes_to_trend_ma5(self):
        closes = [10.0 + index * 0.1 for index in range(70)]
        lows = [value - 0.1 for value in closes]
        ma5 = sum(closes[-5:]) / 5
        lows[-1] = ma5 * 0.995
        stock = observation_stock(
            "600003",
            closes,
            {"close": closes[-1], "low": closes[-1] - 0.1, "pct": 1.0, "amount_wan": 100.0},
            lows=lows,
        )
        contract = router.classify_stock(stock)
        self.assertEqual(contract["key"], router.TREND_MA5)
        self.assertFalse(router.execution_gate(contract, "V2_E3_MA20_STRUCTURAL_RECLAIM")[0])
        self.assertTrue(router.execution_gate(contract, "V2_E1_TREND_PULLBACK_RECLAIM")[0])

    def test_mature_uptrend_routes_to_ma5_without_requiring_t_minus_one_pullback(self):
        closes = [20.0 + index * 0.15 for index in range(70)]
        lows = [value * 0.995 for value in closes]
        stock = observation_stock(
            "000977",
            closes,
            {"close": closes[-1], "low": lows[-1], "pct": 0.4, "amount_wan": 110.0},
            lows=lows,
            amounts=[100.0] * 69 + [110.0],
        )
        contract = router.classify_stock(stock)
        self.assertEqual(contract["key"], router.TREND_MA5)
        self.assertTrue(contract["daily_qualified"])
        self.assertFalse(contract["daily_metrics"]["first_pullback"])
        self.assertEqual(contract["allowed_patterns"], ["V2_E1_TREND_PULLBACK_RECLAIM", "V2_E4_BREAKOUT_RETEST"])

    def test_broken_ma20_structure_stays_unclassified(self):
        closes = [20.0 + index * 0.1 for index in range(60)] + [24.0 - index * 0.7 for index in range(10)]
        stock = observation_stock(
            "600004",
            closes,
            {"close": closes[-1], "low": closes[-1] * 0.99, "pct": -3.0, "amount_wan": 120.0},
        )
        contract = router.classify_stock(stock)
        self.assertEqual(contract["key"], router.OBSERVE)
        self.assertFalse(contract["daily_qualified"])

    def test_unclassified_observation_cannot_open_on_generic_v2_pattern(self):
        stock = observation_stock(
            "600004",
            [10.0] * 25,
            {"close": 10.0, "low": 9.7, "pct": 0.2, "amount_wan": 20.0},
        )
        contract = router.classify_stock(stock)
        self.assertEqual(contract["key"], router.OBSERVE)
        allowed, reason = router.execution_gate(contract, "V2_E1_TREND_PULLBACK_RECLAIM")
        self.assertFalse(allowed)
        self.assertIn("未取得", reason)

    def test_each_strategy_has_one_explicit_formal_buy_signal(self):
        self.assertIn("E5", router.formal_entry_signal(router.LEADER))
        self.assertIn("日线锚点", router.formal_entry_signal(router.TREND_520))
        self.assertIn("日线MA5", router.formal_entry_signal(router.TREND_MA5))
        self.assertIn("不产生新增仓", router.formal_entry_signal(router.OBSERVE))

    def test_observation_entry_requires_live_sector_resonance_after_daily_qualification(self):
        contract = {
            "key": router.TREND_520,
            "name": "520战法",
            "is_observation_strategy": True,
            "daily_qualified": True,
            "daily_gate_reason": "日线520资格通过",
        }
        weak, weak_reason = router.sector_resonance_gate(contract, {"sector_momentum": {"board_name": "测试板块", "board_pct": 1.2, "emotion_ok": True}, "sector_rotation": {"sustained": False, "leader_healthy": True}})
        self.assertFalse(weak)
        self.assertIn("连续刷新", weak_reason)
        qualified, qualified_reason = router.daily_qualification_gate(contract)
        self.assertTrue(qualified)
        self.assertIn("日线520", qualified_reason)
        strong, strong_reason = router.sector_resonance_gate(contract, {"sector_momentum": {"board_name": "测试板块", "board_pct": 1.2, "emotion_ok": True}, "sector_rotation": {"sustained": True, "leader_healthy": True}})
        self.assertTrue(strong)
        self.assertIn("板块共振通过", strong_reason)

    def test_trend_strategy_accepts_sustained_strong_board_without_local_leader_proxy(self):
        contract = {
            "key": router.TREND_520,
            "name": "520战法",
            "is_observation_strategy": True,
            "daily_qualified": True,
        }
        allowed, reason = router.sector_resonance_gate(contract, {
            "sector_momentum": {"board_name": "短剧互动游戏", "board_pct": 3.7, "emotion_ok": True},
            "sector_rotation": {"sustained": True, "leader_healthy": False},
        })
        self.assertTrue(allowed)
        self.assertIn("连续强板块代理", reason)

    def test_leader_strategy_still_requires_leader_health(self):
        contract = {
            "key": router.LEADER,
            "name": "龙头战法",
            "is_observation_strategy": True,
            "daily_qualified": True,
        }
        allowed, reason = router.sector_resonance_gate(contract, {
            "sector_momentum": {"board_name": "大众出版", "board_pct": 3.8, "emotion_ok": True},
            "sector_rotation": {"sustained": True, "leader_healthy": False},
        })
        self.assertFalse(allowed)
        self.assertIn("领涨股", reason)


if __name__ == "__main__":
    unittest.main()
