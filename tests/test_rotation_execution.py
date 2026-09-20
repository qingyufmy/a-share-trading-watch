import unittest
from datetime import datetime
from tempfile import TemporaryDirectory
from unittest.mock import patch

from core import global_risk
import paper_trading
import realtime_signal_engine as engine


def breadth_rows(up, down, flat, amount):
    rows = []
    for index in range(up):
        rows.append({"f12": f"60{index:04d}", "f3": 1.0, "f6": amount})
    for index in range(down):
        rows.append({"f12": f"00{index:04d}", "f3": -1.0, "f6": amount})
    for index in range(flat):
        rows.append({"f12": f"30{index:04d}", "f3": 0.0, "f6": amount})
    return rows


class RotationExecutionTests(unittest.TestCase):
    def test_runtime_revalidates_partial_day_trend_contract_with_t_minus_one_only(self):
        levels = {
            "600002": {
                "code": "600002", "name": "测试趋势", "observation_plan_group": "观察池",
                "industry": "船舶制造",
                "strategy_key": "TREND_520", "strategy_daily_qualified": False,
                "premarket_plan_allows_entry": False,
            }
        }
        daily = [
            {
                "date": f"2026-07-{index + 1:02d}", "close": 9.5, "low": 9.4,
                "high": 9.6, "pct": 0.1, "amount_wan": 100.0,
            }
            for index in range(30)
        ]
        daily.extend([
            {
                "date": f"2026-08-{index + 1:02d}", "close": 10.5, "low": 10.0,
                "high": 10.6, "pct": 1.0, "amount_wan": 120.0,
            }
            for index in range(5)
        ])
        daily[-1]['date'] = '2026-09-03'
        daily.append({
            "date": "2026-09-04", "close": 10.8, "low": 10.7, "high": 10.9,
            "pct": 2.8, "amount_wan": 1.0,
        })
        cache = {}
        with patch('core.sector_identity.load_catalog', return_value=[{'f12': 'BK1', 'f14': '船舶制造'}]), \
             patch.object(engine.intra.base, "fetch_sohu_daily", return_value=daily), \
             patch.object(engine.observation_strategy_router, "_kdj_metrics", return_value={
                 "k": 55.0, "d": 50.0, "j": 65.0, "k_cross_up": True,
                 "bullish": True, "not_overheated": True,
             }):
            engine.revalidate_planned_trend_contracts(
                levels, cache=cache, report_date="2026-09-04"
            )
        self.assertTrue(levels["600002"]["strategy_daily_qualified"])
        self.assertTrue(levels["600002"]["premarket_plan_allows_entry"])
        self.assertEqual(levels["600002"]["planned_v2_probe_position_pct"], 0.01)
        self.assertEqual(cache["daily_contract_revalidation_health"]["corrected"], 1)

    def test_startup_delay_still_builds_board_rotation_samples_for_watchlist(self):
        args = type("Args", (), {"radar_start_delay": 180, "radar_refresh": 120, "bar_refresh": 60})()
        quote = {
            "code": "600000", "name": "测试出版", "industry": "出版",
            "close": 10.5, "open": 10.0, "pct": 5.0, "amount_wan": 100000,
        }
        cache = {"radar_first_seen_ts": __import__("time").time()}
        with patch.object(engine.intra, "fetch_fast_boards", return_value=[{"f14": "出版", "f3": 4.0}]), \
             patch.object(engine, "build_tracked_opportunity_rows", return_value=[]):
            engine.refresh_market_opportunities(
                {}, cache, args, quotes={"600000": quote}, global_context={}
            )
        state = cache["board_rotation_state"]["出版"]
        self.assertEqual(state["samples"], 1)
        self.assertTrue(state["leader_healthy"])

    def test_market_profiles_enrich_watchlist_quotes_without_overwriting_live_name(self):
        quotes = {"603162": {"name": "海通发展", "close": 14.64}}
        cache = {
            "market_stock_profiles": {
                "603162": {
                    "code": "603162", "name": "错误名称", "industry": "航运港口",
                    "concepts": "海洋经济", "region": "福建板块",
                }
            }
        }
        enriched = engine.enrich_quotes_with_market_profiles(
            quotes, cache=cache, current=datetime(2026, 9, 4, 10, 15)
        )
        self.assertEqual(enriched["603162"]["name"], "海通发展")
        self.assertEqual(enriched["603162"]["industry"], "航运港口")
        self.assertEqual(enriched["603162"]["concepts"], "海洋经济")
        self.assertEqual(cache["market_profile_health"]["industry_enriched"], 1)

    def test_market_profiles_survive_engine_restart_via_disk_cache(self):
        quotes = {"603162": {"name": "海通发展", "close": 14.64}}
        persisted = {
            "603162": {
                "code": "603162", "name": "海通发展", "industry": "航运港口",
                "concepts": "海洋经济", "region": "福建板块",
            }
        }
        with patch.object(engine, "load_market_profile_cache", return_value=persisted):
            enriched = engine.enrich_quotes_with_market_profiles(
                quotes, cache={}, current=datetime(2026, 9, 4, 10, 15)
            )
        self.assertEqual(enriched["603162"]["industry"], "航运港口")

    def test_profile_fetch_falls_back_to_f10_when_quote_host_is_limited(self):
        def f10_response(url, timeout=6):
            if "CompanySurvey" in url:
                return {"jbzl": [{"EM2016": "文化传媒-影视动漫-影视"}]}
            return {"ssbk": [{"BOARD_NAME": "影视院线"}, {"BOARD_NAME": "短剧互动游戏"}]}

        with patch.object(engine, "fetch_json_fast", side_effect=OSError("limited")), \
             patch.object(engine.intra.base, "fetch_json", side_effect=f10_response):
            profile = engine._fetch_stock_profile_fast("300133")
        self.assertEqual(profile["industry"], "文化传媒-影视动漫-影视")
        self.assertIn("影视院线", profile["concepts"])

    def test_minute_volume_gate_uses_only_completed_windows(self):
        rows = [
            {"m": f"10:{minute:02d}:00", "p": 10.0 + minute / 100, "v": 100, "avg_p": 10.0}
            for minute in range(0, 17)
        ]
        rows.append({"m": "10:17:00", "p": 10.18, "v": 1, "avg_p": 10.0})
        feat = engine.minute_features(rows, 10.18, current=datetime(2026, 9, 4, 10, 17, 20))
        self.assertEqual(feat["volume_sample_time"], "10:16:00")
        self.assertAlmostEqual(feat["amount_ratio_1m"], 1.0)
        self.assertAlmostEqual(feat["amount_ratio_5m"], 1.0)

    def test_tencent_index_identity_cannot_be_overwritten_by_same_code_stock(self):
        def line(symbol, name, code, pct):
            parts = [""] * 40
            parts[1], parts[2] = name, code
            parts[3], parts[4], parts[5], parts[6] = "10", "9.9", "9.95", "100"
            parts[30], parts[31], parts[32] = "20260827150000", "0.1", str(pct)
            parts[33], parts[34], parts[37], parts[38] = "10.1", "9.8", "10000", "1.0"
            return f'v_{symbol}="{"~".join(parts)}";'

        parsed = engine.parse_tencent_quote_text("\n".join([
            line("sz000001", "平安银行", "000001", -1.19),
            line("sh000001", "上证指数", "000001", 1.14),
            line("sz399001", "深证成指", "399001", 1.51),
            line("sz399006", "创业板指", "399006", 1.72),
        ]))
        self.assertEqual(parsed["000001"]["name"], "平安银行")
        self.assertEqual(parsed["sh000001"]["pct"], 1.14)
        regime = engine.build_a_share_market_regime(parsed, cache={}, now=datetime(2026, 8, 27, 15, 0))
        self.assertEqual(regime["indexes"]["shanghai"]["pct"], 1.14)
        self.assertEqual(regime["state"], "growth_lead")

    def test_broad_strong_market_expands_but_caps_rotation_pilot_budget(self):
        budget = engine.rotation_pilot_budget({
            "risk_level": "green",
            "a_share_market_regime": {"state": "broad_strong"},
            "market_breadth": {"up_ratio": 0.68},
        })
        self.assertGreater(budget["cap_pct"], engine.ROTATION_PILOT_ENTRY_CAP_PCT)
        self.assertLessEqual(budget["cap_pct"], 0.025)
        self.assertLessEqual(budget["probe_pct"], 0.01)

    def test_risk_off_market_closes_rotation_pilot_budget(self):
        budget = engine.rotation_pilot_budget({
            "risk_level": "green", "a_share_market_regime": {"state": "risk_off"}
        })
        self.assertEqual(budget["cap_pct"], 0.0)

    def test_shrinking_weak_breadth_changes_local_regime(self):
        cache = {}
        engine.build_market_breadth(
            breadth_rows(up=15, down=66, flat=19, amount=1_000_000),
            cache=cache,
            now=datetime(2026, 8, 11, 10, 0),
        )
        breadth = engine.build_market_breadth(
            breadth_rows(up=15, down=66, flat=19, amount=1_100_000),
            cache=cache,
            now=datetime(2026, 8, 11, 10, 2),
        )
        breadth = engine.build_market_breadth(
            breadth_rows(up=15, down=66, flat=19, amount=1_150_000),
            cache=cache, now=datetime(2026, 8, 11, 10, 4),
        )
        self.assertEqual(breadth["state"], "shrinking_weak")
        regime = engine.build_a_share_market_regime(
            {
                "000001": {"pct": 0.05},
                "399001": {"pct": -0.08},
                "399006": {"pct": 0.10},
            },
            cache=cache,
            now=datetime(2026, 8, 11, 10, 2),
            breadth=breadth,
        )
        self.assertEqual(regime["state"], "rotation_defensive")

    def test_incomplete_breadth_snapshot_cannot_become_broad_strong(self):
        breadth = engine.build_market_breadth(
            breadth_rows(up=100, down=0, flat=0, amount=1_000_000),
            cache={},
            now=datetime(2026, 8, 12, 10, 0),
            expected_total=5_000,
        )
        self.assertEqual(breadth["state"], "data_stale")
        self.assertFalse(breadth["coverage_ready"])
        self.assertIn("100/5000", breadth["reason"])

    def test_single_board_spike_cannot_open_rotation_lane(self):
        row = {
            "industry": "医疗研发外包",
            "quote": {"name": "测试医药", "close": 12.0},
            "sector_momentum": {"board_name": "医疗研发外包", "board_pct": 1.2, "emotion_ok": True},
            "sector_rotation": {"available": True, "sustained": False, "leader_healthy": True},
            "rt_features": {"amount_ratio_1m": 1.3, "amount_ratio_5m": 1.2},
        }
        ok, reason = global_risk.sector_rotation_gate(row)
        self.assertFalse(ok)
        self.assertIn("连续刷新", reason)

    def test_second_persistent_board_sample_and_healthy_leader_are_required(self):
        board = [{"f14": "医疗研发外包", "f3": 1.20}]
        candidate = {
            "quote": {
                "code": "603127", "name": "测试医药", "industry": "医疗研发外包",
                "close": 12.0, "open": 11.5, "pct": 2.2, "amount_wan": 120000,
            },
            "score": 8.0,
            "matched": [],
        }
        cache = {}
        first = engine.update_board_rotation_state(board, [candidate], cache=cache, now=datetime(2026, 8, 11, 10, 0))
        self.assertFalse(first["医疗研发外包"]["sustained"])
        second = engine.update_board_rotation_state(board, [candidate], cache=cache, now=datetime(2026, 8, 11, 10, 2))
        state = second["医疗研发外包"]
        self.assertTrue(state["sustained"])
        self.assertTrue(state["leader_healthy"])

    def test_small_cap_leader_uses_normalized_liquidity_not_fixed_eight_yi(self):
        board = [{"f14": "出版", "f3": 3.34}]
        candidate = {
            "quote": {
                "code": "605577", "name": "龙版传媒", "industry": "出版",
                "close": 15.55, "open": 14.22, "pct": 9.97, "amount_wan": 47200,
                "turnover": 7.52, "float_market_cap_yi": 62.84,
            },
            "score": 10.0,
            "matched": [],
        }
        cache = {}
        engine.update_board_rotation_state(board, [candidate], cache=cache, now=datetime(2026, 9, 4, 11, 28))
        state = engine.update_board_rotation_state(board, [candidate], cache=cache, now=datetime(2026, 9, 4, 11, 30))["出版"]
        self.assertTrue(state["sustained"])
        self.assertTrue(state["leader_healthy"])
        self.assertLess(state["leader_liquidity"]["amount_yi"], 8.0)
        self.assertTrue(state["leader_liquidity"]["ok"])

    def test_yesterday_board_samples_cannot_pass_todays_resonance_gate(self):
        board = [{"f14": "医疗研发外包", "f3": 1.20}]
        candidate = {
            "quote": {
                "code": "603127", "name": "测试医药", "industry": "医疗研发外包",
                "close": 12.0, "open": 11.5, "pct": 2.2, "amount_wan": 120000,
            },
            "score": 8.0,
            "matched": [],
        }
        cache = {
            "board_rotation_history": {
                "医疗研发外包": [
                    {"timestamp": "2026-08-11 14:55:00", "pct": 1.5},
                    {"timestamp": "2026-08-11 14:57:00", "pct": 1.4},
                ]
            }
        }
        state = engine.update_board_rotation_state(board, [candidate], cache=cache, now=datetime(2026, 8, 12, 9, 31))
        self.assertEqual(state["医疗研发外包"]["samples"], 1)
        self.assertFalse(state["医疗研发外包"]["sustained"])

    def test_new_theme_discovery_cannot_manufacture_an_intraday_entry_plan(self):
        board = [{"f14": "医疗研发外包", "f3": 1.20}]
        candidate = {
            "quote": {
                "code": "603127", "name": "测试医药", "industry": "医疗研发外包",
                "close": 12.0, "open": 11.5, "pct": 2.2, "amount_wan": 120000,
            },
            "score": 8.0,
            "matched": [],
        }
        state = {"医疗研发外包": {"sustained": True, "leader_healthy": True, "board_pct": 1.2}}
        context = {"large_cycle_ready": True, "chips": {"state": "supportive"}, "time": {"state": "normal"}}
        with patch.object(engine.intra.base, "fetch_sohu_daily", return_value=[]), \
             patch.object(engine.cycle_framework, "build_plan_context", return_value=context):
            pilots = engine.build_rotation_pilot_candidates([candidate], board, state, cache={})
        self.assertEqual(pilots, [])

    def test_legacy_whole_percent_rotation_cap_is_normalized(self):
        self.assertEqual(engine.normalized_position_pct("1.5", 0.015), 0.015)
        self.assertEqual(engine.normalized_position_pct("0.02", 0.015), 0.02)

    def test_rotation_pilot_cap_reaches_paper_order_sizing(self):
        with TemporaryDirectory() as tmp:
            con = paper_trading.init_db(__import__("pathlib").Path(tmp))
            plan = paper_trading.buy_position_plan(
                con,
                {
                    "scenario": "MARKET_OPPORTUNITY_ACTIONABLE",
                    "symbol": "600000",
                    "current_price": 100.0,
                    "global_risk_level": "green",
                    "entry_position_cap_pct": 0.015,
                },
                account={"cash": 1_000_000.0, "total_assets_estimate": 1_000_000.0},
            )
        self.assertTrue(plan["passed"], plan["reason"])
        self.assertLessEqual(plan["projected_position_pct"], 1.5)
