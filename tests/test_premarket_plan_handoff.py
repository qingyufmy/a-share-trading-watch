import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import intraday_report as intra
import after_close_report as base
import paper_trading
import realtime_signal_engine as engine
from core import emotion_leader_pool, observation_strategy_router
from premarket_report import merge_closed_quote_for_premarket, premarket_position_plan


class PremarketPlanHandoffTests(unittest.TestCase):
    def test_timestamped_quote_extends_lagging_daily_history_on_its_real_trade_date(self):
        daily = [{"date": "2026-09-01", "close": 17.68, "open": 17.59, "low": 17.56, "high": 18.18}]
        quote = {
            "datetime": "20260902150000", "open": 17.68, "close": 17.50,
            "change": -0.18, "pct": -1.02, "low": 17.00, "high": 17.62,
            "volume_lot": 1.0, "amount_wan": 1.0, "turnover": 1.0,
        }
        enriched = base.daily_with_quote(daily, quote)
        self.assertEqual([row["date"] for row in enriched], ["2026-09-01", "2026-09-02"])
        self.assertEqual(enriched[-1]["close"], 17.50)
        self.assertEqual(len(base.daily_with_quote(enriched, quote)), 2)

    def test_premarket_never_promotes_same_day_partial_quote_to_daily_bar(self):
        daily = [{"date": "2026-09-03", "close": 17.70, "open": 17.50, "low": 17.40, "high": 17.90}]
        quote = {
            "datetime": "20260904092500", "open": 18.00, "close": 18.20,
            "pct": 2.82, "low": 17.95, "high": 18.20, "amount_wan": 10.0,
        }
        merged = merge_closed_quote_for_premarket(daily, quote, report_date="2026-09-04")
        self.assertEqual(merged, daily)

    def test_premarket_removes_same_day_partial_bar_returned_by_daily_provider(self):
        daily = [
            {"date": "2026-09-03", "close": 17.70, "open": 17.50, "low": 17.40, "high": 17.90},
            {"date": "2026-09-04", "close": 18.20, "open": 18.00, "low": 17.95, "high": 18.20},
        ]
        quote = {"datetime": "20260904092900", "close": 18.20}
        merged = merge_closed_quote_for_premarket(daily, quote, report_date="2026-09-04")
        self.assertEqual([row["date"] for row in merged], ["2026-09-03"])

    def test_premarket_can_merge_prior_closed_day_when_provider_lags(self):
        daily = [{"date": "2026-09-02", "close": 17.50, "open": 17.60, "low": 17.30, "high": 17.80}]
        quote = {
            "datetime": "20260903150000", "open": 17.60, "close": 17.70,
            "pct": 1.14, "low": 17.40, "high": 17.90, "amount_wan": 10.0,
        }
        merged = merge_closed_quote_for_premarket(daily, quote, report_date="2026-09-04")
        self.assertEqual(merged[-1]["date"], "2026-09-03")

    def test_intraday_watchlist_includes_previous_day_emotion_candidates(self):
        snapshot = {
            "source_date": "2026-09-03",
            "source": "eastmoney_popularity_top100",
            "candidates": [{"code": "600172", "market": "17", "qualified": True}],
        }
        plan = {"rows": [("000977", "33")], "groups": []}
        with patch.object(base, "read_watchlist", return_value=[("000831", "33")]), patch.object(
            base, "premarket_plan_observation_details", return_value=plan
        ), patch.object(base, "previous_trading_date", return_value="2026-09-03"), patch.object(
            emotion_leader_pool, "load_latest_snapshot", return_value=snapshot
        ):
            watchlist = intra.current_watchlist_map()
        self.assertEqual(set(watchlist), {"000831", "000977", "600172"})

    def test_retired_e6_cannot_reenter_even_with_sellable_base_position(self):
        row = {
            "code": "000831", "name": "中国稀土",
            "quote": {"name": "中国稀土", "close": 60.0, "prev_close": 58.0},
            "timing_v2": {
                "version": "intraday_timing_v2_0", "entry_allowed": True,
                "entry_pattern": "V2_E6_REPAIR_REGIME_UPGRADE", "position_action": "HOLD",
                "position_multiplier": 0.2, "levels": {"structural_invalidation": 58.5, "nearest_resistance": 64.0},
                "room_risk": {"entry_reference": 60.0, "room_atr": 2.0, "reward_risk": 2.67,
                              "execution_band_low": 59.9, "execution_band_high": 60.1},
                "blockers": [],
            },
            "premarket_plan_loaded": True,
            "premarket_plan_action": "已有仓位持有保护，可卖底仓做T",
            "premarket_plan_allows_entry": False,
            "planned_target_position_pct": 0.05,
            "planned_max_position_pct": 0.08,
            "planned_v2_probe_position_pct": 0.01,
        }
        signal = engine.v2_signal_for_row(row, position={"sellable": 200, "quantity": 200})
        self.assertEqual(signal["scenario"], "V2_WAIT")
        self.assertFalse(signal["repair_reentry_authorized"])
        self.assertFalse(signal["premarket_plan_allows_entry"])
        flat = engine.v2_signal_for_row(row, position={"sellable": 0, "quantity": 0})
        self.assertEqual(flat["scenario"], "V2_WAIT")

    def test_framework_plan_scales_technology_when_global_bias_is_weak(self):
        stock = {
            "quote": {"code": "688001", "name": "测试芯片", "industry": "半导体"},
            "tech": {"priority": "P2", "state": "强趋势"},
        }
        plan = premarket_position_plan(stock, "偏空科技成长")
        self.assertTrue(plan["v2_entry_enabled"])
        self.assertEqual(plan["plan_action"], "科技防守试仓")
        self.assertEqual(plan["target_position_pct"], 0.03)
        self.assertEqual(plan["v2_probe_position_pct"], 0.01)

    def test_september_observation_plan_uses_capped_v2_probe(self):
        stock = {
            "quote": {"code": "600186", "name": "测试观察", "industry": "食品"},
            "tech": {"priority": "P2", "state": "强趋势"},
            "plan_universe": "9月观察池",
            "observation_plan_group": "9月观察池",
            "strategy_contract": {
                "key": observation_strategy_router.TREND_520,
                "name": "520战法",
                "style": "趋势票",
            },
        }

        plan = premarket_position_plan(stock, "中性")

        self.assertEqual(plan["plan_action"], "520趋势条件试仓")
        self.assertEqual(plan["target_position_pct"], 0.02)
        self.assertEqual(plan["max_position_pct"], 0.03)
        self.assertEqual(plan["v2_probe_position_pct"], 0.01)
        self.assertTrue(plan["v2_entry_enabled"])

    def test_unclassified_observation_plan_remains_covered_but_cannot_open(self):
        stock = {
            "quote": {"code": "600186", "name": "测试观察", "industry": "食品"},
            "tech": {"priority": "P2", "state": "强趋势"},
            "plan_universe": "9月观察池",
            "observation_plan_group": "9月观察池",
            "strategy_contract": {"key": observation_strategy_router.OBSERVE},
        }
        plan = premarket_position_plan(stock, "中性")
        self.assertEqual(plan["plan_action"], "观察池待分类")
        self.assertFalse(plan["v2_entry_enabled"])

    def test_named_observation_methods_own_their_first_entry_and_position_caps(self):
        cases = [
            (observation_strategy_router.LEADER, "龙头战法", 0.01, 0.02),
            (observation_strategy_router.TREND_520, "520战法", 0.01, 0.03),
            (observation_strategy_router.TREND_MA5, "趋势5日线法则", 0.01, 0.03),
        ]
        for key, name, first_entry, max_position in cases:
            with self.subTest(strategy=key):
                plan = premarket_position_plan({
                    "quote": {"code": "600186", "name": "测试观察", "industry": "食品"},
                    "tech": {"priority": "P2", "state": "强趋势"},
                    "plan_universe": "全自选观察池/策略盘",
                    "observation_plan_group": "策略盘",
                    "strategy_contract": {"key": key, "name": name, "style": "情绪票" if key == observation_strategy_router.LEADER else "趋势票"},
                }, "中性")
                self.assertTrue(plan["v2_entry_enabled"])
                self.assertEqual(plan["v2_probe_position_pct"], first_entry)
                self.assertEqual(plan["max_position_pct"], max_position)

    def test_premarket_report_plan_is_parsed_into_runtime_levels(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            base_dir = Path(temp_dir)
            path = base_dir / "web_dashboard" / "data" / "reports" / "premarket_20260813.json"
            path.parent.mkdir(parents=True)
            path.write_text(json.dumps({"rows": [{
                "代码": "600000", "名称": "测试", "状态": "🟢 强趋势", "优先级": "P2",
                "防守": "10.00", "修复": "10.20", "压力": "10.80",
                "当日计划": "520战法计划", "目标仓位": "2%", "单票上限": "3%", "策略首笔": "1%",
                "策略键": "TREND_520", "日线策略资格": "通过",
            }]}, ensure_ascii=False), encoding="utf-8")
            with patch.object(intra, "BASE_DIR", base_dir), patch.object(intra, "REPORT_DATE", "2026-08-13"), patch.object(intra, "current_watchlist_map", return_value={}):
                levels = intra.read_premarket_levels_from_json()
        level = levels["600000"]
        self.assertEqual(level["planned_target_position_pct"], 0.02)
        self.assertEqual(level["planned_max_position_pct"], 0.03)
        self.assertEqual(level["planned_v2_probe_position_pct"], 0.01)
        self.assertTrue(level["premarket_plan_allows_entry"])
        self.assertTrue(level["premarket_plan_loaded"])

    def test_unclassified_observation_row_has_no_new_entry_authority(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            base_dir = Path(temp_dir)
            path = base_dir / "web_dashboard" / "data" / "reports" / "premarket_20260813.json"
            path.parent.mkdir(parents=True)
            path.write_text(json.dumps({"rows": [{
                "代码": "600186", "名称": "测试观察", "计划范围": "9月观察池",
                "防守": "10.00", "修复": "10.20", "压力": "10.80",
                "当日计划": "观察池待分类", "目标仓位": "0%", "单票上限": "0%", "策略首笔": "0%",
                "交易策略": "待分类观察", "策略键": "OBSERVE_UNCLASSIFIED",
            }]}, ensure_ascii=False), encoding="utf-8")
            with patch.object(intra, "BASE_DIR", base_dir), patch.object(intra, "REPORT_DATE", "2026-08-13"), patch.object(
                intra, "current_watchlist_map", return_value={"600186": "17"}
            ):
                levels = intra.read_premarket_levels_from_json()
        self.assertFalse(levels["600186"]["premarket_plan_allows_entry"])

    def test_observation_plan_row_survives_runtime_watchlist_filter(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            base_dir = Path(temp_dir)
            path = base_dir / "web_dashboard" / "data" / "reports" / "premarket_20260813.json"
            path.parent.mkdir(parents=True)
            path.write_text(json.dumps({"rows": [{
                "代码": "600186", "名称": "测试观察", "计划范围": "9月观察池", "状态": "🟢 强趋势", "优先级": "P2",
                "防守": "10.00", "修复": "10.20", "压力": "10.80",
                "当日计划": "观察池条件试仓", "目标仓位": "2%", "单票上限": "3%", "策略首笔": "1%",
                "交易类型": "趋势票", "对应方法": "520战法", "策略键": "TREND_520",
                "允许时机证据": "V2_E1_TREND_PULLBACK_RECLAIM、V2_E3_MA20_STRUCTURAL_RECLAIM",
                "策略入场纪律": "只做MA5/MA20收复", "策略退出纪律": "MA20失守退出", "策略依据": "日线回踩收复",
            }]}, ensure_ascii=False), encoding="utf-8")
            with patch.object(intra, "BASE_DIR", base_dir), patch.object(intra, "REPORT_DATE", "2026-08-13"), patch.object(
                intra, "current_watchlist_map", return_value={"600186": "17"}
            ):
                levels = intra.read_premarket_levels_from_json()
        level = levels["600186"]
        self.assertEqual(level["plan_universe"], "9月观察池")
        self.assertEqual(level["planned_v2_probe_position_pct"], 0.01)
        self.assertEqual(level["strategy_key"], "TREND_520")
        self.assertIn("V2_E3_MA20_STRUCTURAL_RECLAIM", level["strategy_allowed_patterns"])

    def test_current_type_and_strategy_headers_survive_runtime_handoff(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            base_dir = Path(temp_dir)
            path = base_dir / "web_dashboard" / "data" / "reports" / "premarket_20260813.json"
            path.parent.mkdir(parents=True)
            path.write_text(json.dumps({"rows": [{
                "代码": "600186", "名称": "测试观察", "计划范围": "9月观察池", "状态": "🟢 强趋势", "优先级": "P2",
                "防守": "10.00", "修复": "10.20", "压力": "10.80",
                "当日计划": "520趋势条件试仓", "目标仓位": "2%", "单票上限": "3%", "策略首笔": "1%",
                "股票类型": "趋势票", "交易策略": "520战法", "策略键": "TREND_520",
                "正式买入信号": "520战法正式买入：仅E1/E3/E4任一确认后首笔试仓",
            }]}, ensure_ascii=False), encoding="utf-8")
            with patch.object(intra, "BASE_DIR", base_dir), patch.object(intra, "REPORT_DATE", "2026-08-13"), patch.object(
                intra, "current_watchlist_map", return_value={"600186": "17"}
            ):
                levels = intra.read_premarket_levels_from_json()
        level = levels["600186"]
        self.assertEqual(level["strategy_style"], "趋势票")
        self.assertEqual(level["strategy_name"], "520战法")
        self.assertIn("E1/E3/E4", level["strategy_formal_entry_signal"])

    def test_missing_observation_plan_row_is_covered_but_cannot_fall_back_to_core_entry(self):
        with patch.object(intra, "current_watchlist_map", return_value={"600000": "17", "300001": "33"}), patch.object(
            intra.base,
            "read_observation_watchlist_details",
            return_value={"memberships": {"300001": ["商业航天"]}},
        ):
            levels = intra.normalize_levels_to_watchlist({"600000": {"code": "600000", "name": "核心"}})
        fallback = levels["300001"]
        self.assertEqual(fallback["observation_plan_group"], "商业航天")
        self.assertEqual(fallback["strategy_key"], observation_strategy_router.OBSERVE)
        self.assertFalse(fallback["premarket_plan_allows_entry"])
        self.assertEqual(fallback["planned_v2_probe_position_pct"], 0.0)

    def test_observation_fallback_with_no_premarket_levels_keeps_engine_monitoring(self):
        level = intra.observation_fallback_level(
            "000630",
            "33",
            {"memberships": {"000630": ["9月观察池"]}},
        )
        rows = intra.build_rows({"000630": level}, {
            "000630": {"code": "000630", "name": "测试观察", "close": 10.0, "prev_close": 9.9, "open": 9.95, "high": 10.1, "low": 9.8, "pct": 1.01},
        }, {"000630": {"available": False}})
        self.assertEqual(len(rows), 1)
        self.assertFalse(rows[0]["premarket_plan_allows_entry"])
        self.assertGreater(rows[0]["pressure"], 0)
        self.assertGreater(rows[0]["defense"], 0)

    def test_legacy_raw_timing_pattern_is_rejected_by_position_planner(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            con = paper_trading.init_db(Path(temp_dir))
            plan = paper_trading.buy_position_plan(con, {
                "scenario": "V2_E1_TREND_PULLBACK_RECLAIM",
                "strategy_family": "V2_ONLY",
                "symbol": "600000",
                "current_price": 100.0,
                "position_multiplier": 0.33,
                "premarket_plan_action": "顺势分批",
                "premarket_plan_allows_entry": True,
                "premarket_plan_complete": True,
                "planned_target_position_pct": 0.06,
                "planned_max_position_pct": 0.08,
                "planned_v2_probe_position_pct": 0.02,
            }, account={"cash": 1_000_000.0, "total_assets_estimate": 1_000_000.0})
        self.assertFalse(plan["passed"])
        self.assertIn("不能直接下单", plan["reason"])

    def test_v2_candidate_without_daily_position_plan_is_wait_only(self):
        row = {
            "code": "002407",
            "quote": {"code": "002407", "name": "测试候选", "close": 36.63, "prev_close": 36.20, "high": 36.80, "low": 36.10},
            "timing_v2": {
                "entry_allowed": True,
                "entry_pattern": "V2_E2_STRUCTURAL_SUPPORT_REVERSAL",
                "version": "intraday_timing_v2_0",
                "regime": "REPAIR",
                "location": "TIER1_SUPPORT",
                "setup_15m": "E2_STRUCTURAL_SUPPORT_REVERSAL",
                "execution_5m": "VWAP_RECLAIM",
                "levels": {"structural_invalidation": 36.47, "nearest_resistance": 37.27},
                "room_risk": {"entry_reference": 36.63, "reward_risk": 4.12, "room_atr": 2.13, "execution_band_low": 36.60, "execution_band_high": 36.66},
            },
        }
        signal = engine.v2_signal_for_row(row)
        self.assertEqual(signal["scenario"], "V2_WAIT")
        self.assertIn("盘前仓位计划缺失", "；".join(signal["reasons"]))

    def test_observation_strategy_rejects_a_v2_pattern_from_another_method(self):
        row = {
            "code": "600186",
            "quote": {"code": "600186", "name": "测试观察", "close": 10.0},
            "timing_v2": {
                "entry_allowed": True,
                "entry_pattern": "V2_E5_LEADER_SECOND_LEG",
                "version": "intraday_timing_v2_0",
                "regime": "TREND",
                "location": "LEADER_SECOND_LEG",
                "setup_15m": "E5_LEADER_SECOND_LEG",
                "execution_5m": "VWAP_RECLAIM",
                "levels": {"structural_invalidation": 9.7, "nearest_resistance": 10.8},
                "room_risk": {"entry_reference": 10.0, "reward_risk": 2.5, "room_atr": 2.0, "execution_band_low": 9.98, "execution_band_high": 10.02},
            },
            "observation_plan_group": "9月观察池",
            "strategy_contract": {
                "key": observation_strategy_router.TREND_520,
                "name": "520战法",
                "style": "趋势票",
                "is_observation_strategy": True,
                "allowed_patterns": ["V2_E1_TREND_PULLBACK_RECLAIM", "V2_E3_MA20_STRUCTURAL_RECLAIM"],
            },
            "premarket_plan_action": "520趋势条件试仓",
            "premarket_plan_allows_entry": True,
            "premarket_plan_loaded": True,
            "premarket_plan_source": "premarket",
            "planned_target_position_pct": 0.02,
            "planned_max_position_pct": 0.03,
            "planned_v2_probe_position_pct": 0.01,
            "framework_validation": {"executable": True},
        }
        signal = engine.v2_signal_for_row(row)
        self.assertEqual(signal["scenario"], "V2_WAIT")
        self.assertIn("策略路由", "；".join(signal["reasons"]))

    def test_waiting_execution_uses_candidate_pattern_for_strategy_contract(self):
        row = {
            "code": "600186",
            "quote": {"code": "600186", "name": "测试观察", "close": 10.0},
            "timing_v2": {
                "entry_allowed": False,
                "entry_pattern": None,
                "candidate_entry_pattern": "V2_E3_MA20_STRUCTURAL_RECLAIM",
                "version": "intraday_timing_v2_0",
                "regime": "TREND",
                "location": "TIER1_SUPPORT",
                "setup_15m": "E3_MA20_STRUCTURAL_RECLAIM",
                "execution_5m": "VWAP_HOLD",
                "levels": {"structural_invalidation": 9.7, "nearest_resistance": 10.8},
                "room_risk": {"reward_risk": 2.5, "room_atr": 2.0},
                "blockers": ["量能未形成可执行确认"],
            },
            "strategy_contract": {
                "key": observation_strategy_router.TREND_520,
                "name": "520战法",
                "style": "趋势票",
                "is_observation_strategy": True,
                "daily_qualified": True,
                "allowed_patterns": [
                    "V2_E1_TREND_PULLBACK_RECLAIM",
                    "V2_E3_MA20_STRUCTURAL_RECLAIM",
                    "V2_E4_BREAKOUT_RETEST",
                ],
            },
            "premarket_plan_action": "520趋势条件试仓",
            "premarket_plan_allows_entry": True,
            "premarket_plan_loaded": True,
            "planned_target_position_pct": 0.02,
            "planned_max_position_pct": 0.03,
            "planned_v2_probe_position_pct": 0.01,
            "sector_momentum": {"emotion_ok": True},
            "sector_rotation": {"sustained": True, "leader_healthy": True},
            "framework_validation": {"executable": True},
        }
        with patch.object(
            observation_strategy_router,
            "sector_resonance_gate",
            return_value=(True, "板块共振通过"),
        ):
            signal = engine.v2_signal_for_row(row)
        self.assertEqual(signal["scenario"], "V2_WAIT")
        self.assertTrue(signal["strategy_gate_ok"])
        self.assertNotIn("策略路由/策略合同", "；".join(signal["reasons"]))

    def test_rotation_pilot_without_named_daily_contract_is_wait_only(self):
        row = {
            "code": "002821",
            "quote": {"code": "002821", "name": "凯莱英", "close": 165.53},
            "timing_v2": {
                "entry_allowed": True,
                "entry_pattern": "V2_E5_LEADER_SECOND_LEG",
                "version": "intraday_timing_v2_0",
                "regime": "TREND",
                "location": "LEADER_SECOND_LEG",
                "position_multiplier": 0.33,
                "levels": {"structural_invalidation": 162.58, "nearest_resistance": 172.23},
                "room_risk": {
                    "entry_reference": 165.53, "reward_risk": 2.27, "room_atr": 4.66,
                    "execution_band_low": 165.39, "execution_band_high": 165.67,
                },
            },
            "rotation_pilot": True,
            "entry_position_cap_pct": 0.015,
            "premarket_plan_action": "盘中轮动龙头试错",
            "premarket_plan_allows_entry": True,
            "premarket_plan_loaded": True,
            "premarket_plan_source": "intraday_rotation_pilot",
            "planned_target_position_pct": 0.015,
            "planned_max_position_pct": 0.015,
            "planned_v2_probe_position_pct": 0.01,
            "framework_validation": {"executable": True},
        }
        signal = engine.v2_signal_for_row(row)
        self.assertEqual(signal["scenario"], "V2_WAIT")
        self.assertEqual(signal["entry_position_cap_pct"], 0.015)
        self.assertTrue(signal["premarket_plan_complete"])
        self.assertIsNone(paper_trading.signal_side(signal))

    def test_observation_strategy_emits_its_own_order_scenario(self):
        row = {
            "code": "600186",
            "quote": {"code": "600186", "name": "测试观察", "close": 10.0},
            "timing_v2": {
                "entry_allowed": True, "entry_pattern": "V2_E3_MA20_STRUCTURAL_RECLAIM",
                "version": "intraday_timing_v2_0", "regime": "TREND", "location": "STRUCTURAL_RECLAIM",
                "position_multiplier": 0.33,
                "levels": {"structural_invalidation": 9.7, "nearest_resistance": 10.8},
                "room_risk": {"entry_reference": 10.0, "reward_risk": 2.0, "room_atr": 2.0,
                              "execution_band_low": 9.95, "execution_band_high": 10.05},
            },
            "observation_plan_group": "策略盘",
            "strategy_contract": {
                "key": observation_strategy_router.TREND_520, "name": "520战法", "style": "趋势票",
                "is_observation_strategy": True,
                "daily_qualified": True,
                "daily_gate_reason": "日线520资格通过",
                "allowed_patterns": ["V2_E1_TREND_PULLBACK_RECLAIM", "V2_E3_MA20_STRUCTURAL_RECLAIM", "V2_E4_BREAKOUT_RETEST"],
            },
            "sector_momentum": {"board_name": "测试板块", "board_pct": 1.2, "emotion_ok": True},
            "sector_rotation": {"sustained": True, "leader_healthy": True},
            "premarket_plan_action": "520趋势条件试仓", "premarket_plan_allows_entry": True,
            "premarket_plan_loaded": True, "premarket_plan_source": "premarket",
            "planned_target_position_pct": 0.02, "planned_max_position_pct": 0.03,
            "planned_v2_probe_position_pct": 0.01, "framework_validation": {"executable": True},
        }
        signal = engine.v2_signal_for_row(row)
        self.assertEqual(signal["scenario"], "STRATEGY_520_ENTRY")
        self.assertEqual(signal["strategy_entry_pattern"], "V2_E3_MA20_STRUCTURAL_RECLAIM")
        self.assertEqual(signal["strategy_name"], "520战法")
        self.assertEqual(paper_trading.signal_side(signal), "BUY")
        with tempfile.TemporaryDirectory() as temp_dir:
            con = paper_trading.init_db(Path(temp_dir))
            plan = paper_trading.buy_position_plan(
                con,
                signal,
                account={"cash": 1_000_000.0, "total_assets_estimate": 1_000_000.0},
            )
        self.assertTrue(plan["passed"], plan["reason"])
        self.assertEqual(plan["step"], "STRATEGY_PROBE_INITIAL")
        self.assertEqual(plan["target_position_pct"], 1.0)
        self.assertLessEqual(plan["projected_position_pct"], 3.0)

        row["sector_rotation"]["sustained"] = False
        blocked = engine.v2_signal_for_row(row)
        self.assertEqual(blocked["scenario"], "V2_WAIT")
        self.assertFalse(blocked["sector_resonance_ok"])
        self.assertIn("连续刷新", "；".join(blocked["reasons"]))

        row["sector_rotation"]["sustained"] = True
        row["strategy_contract"]["daily_qualified"] = False
        row["strategy_contract"]["daily_gate_reason"] = "日线MA20回踩资格失效"
        blocked = engine.v2_signal_for_row(row)
        self.assertEqual(blocked["scenario"], "V2_WAIT")
        self.assertFalse(blocked["strategy_daily_qualified"])
        self.assertIn("日线MA20回踩资格失效", "；".join(blocked["reasons"]))

    def test_retired_e7_is_wait_only(self):
        row = {
            "code": "600227",
            "quote": {"code": "600227", "name": "赤天化", "close": 4.34},
            "timing_v2": {
                "entry_allowed": True,
                "entry_pattern": "V2_E7_FLAT_BASE_BREAKOUT",
                "version": "intraday_timing_v2_0",
                "regime": "TREND",
                "location": "FLAT_BASE_BREAKOUT",
                "setup_15m": "E7_FLAT_BASE_BREAKOUT",
                "execution_5m": "VWAP_RECLAIM",
                "position_multiplier": 0.2,
                "levels": {
                    "entry_invalidation": 4.24,
                    "structural_invalidation": 3.06,
                    "nearest_resistance": 4.58,
                },
                "room_risk": {
                    "entry_reference": 4.34,
                    "reward_risk": 2.4,
                    "room_atr": 6.0,
                    "execution_band_low": 4.336,
                    "execution_band_high": 4.344,
                },
            },
            "premarket_plan_action": "修复试仓",
            "premarket_plan_allows_entry": True,
            "premarket_plan_loaded": True,
            "premarket_plan_source": "premarket_20260828",
            "planned_target_position_pct": 0.03,
            "planned_max_position_pct": 0.05,
            "planned_v2_probe_position_pct": 0.01,
            "framework_validation": {"executable": True},
        }
        signal = engine.v2_signal_for_row(row)
        self.assertEqual(signal["scenario"], "V2_WAIT")
        self.assertIsNone(paper_trading.signal_side(signal))


if __name__ == "__main__":
    unittest.main()
