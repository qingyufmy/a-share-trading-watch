import unittest
import sqlite3
from datetime import datetime
from tempfile import TemporaryDirectory
from unittest.mock import patch

from core import global_risk
from core.strategy_discipline import build_discipline
import render_report_dashboard as dashboard
import realtime_signal_engine as engine
import paper_trading


def market_signal(pressure):
    return {
        "scenario": "MARKET_OPPORTUNITY_ACTIONABLE",
        "external_status": "立即处理",
        "symbol": "002851",
        "current_price": 172.55,
        "trigger_price": 172.06,
        "invalid_price": 171.64,
        "pressure_price": pressure,
        "execution_band_low": 172.06,
        "execution_band_high": 172.55,
        "vwap": 172.20,
        "epsilon": 0.08,
        "last3_prices": [172.42, 172.50, 172.55],
        "max_chase_distance": 1.00,
        "amount_ratio_1m": 1.32,
        "amount_ratio_5m": 1.15,
        "signal_quality_gate": "vwap_pullback_reclaim",
        "confirm_rule": "VWAP回踩收复且量能确认",
        "cancel_rule": "跌破失效价或重新远离VWAP",
        "framework_trade_style": "趋势/波段",
        "framework_execution": {
            "large_cycle_ready": True,
            "chips": {"state": "supportive"},
            "time": {"state": "normal"},
        },
    }


def v2_signal(pressure=174.95):
    signal = market_signal(pressure)
    signal.update({
        "scenario": "STRATEGY_520_ENTRY",
        "strategy_family": "THREE_METHOD",
        "strategy_version": "three_method_strategy_v1",
        "strategy_contract": {
            "key": "TREND_520",
            "name": "520战法",
            "is_observation_strategy": True,
            "daily_qualified": True,
            "allowed_patterns": ["V2_E1_TREND_PULLBACK_RECLAIM", "V2_E3_MA20_STRUCTURAL_RECLAIM", "V2_E4_BREAKOUT_RETEST"],
        },
        "sector_resonance_ok": True,
        "sector_resonance_reason": "板块共振通过",
        "strategy_daily_qualified": True,
        "strategy_daily_evidence": "520日线资格通过",
        "strategy_gate_ok": True,
        "strategy_gate_reason": "520时机证据匹配",
        "signal_quality_gate": "v2_closed_bar_setup",
        "timing_v2": {
            "entry_allowed": True,
            "entry_pattern": "V2_E1_TREND_PULLBACK_RECLAIM",
            "setup_15m": "E1_TREND_PULLBACK_RECLAIM",
            "execution_5m": "VWAP_RECLAIM",
            "room_risk": {"reward_risk": (pressure - 172.55) / (172.55 - 171.64)},
        },
    })
    return signal


class MarketOpportunityRiskRewardTests(unittest.TestCase):
    def test_technical_block_records_its_local_reason_without_crashing(self):
        row = {"code": "600000", "quote": {"code": "600000", "name": "测试候选", "close": 10.0}}
        context = {"risk_level": "green", "policy": {"allow_market_opportunity_buy": True}}
        with patch.object(engine.global_risk, "market_opportunity_gate_status", return_value=(True, "NORMAL", "允许")), \
            patch.object(engine, "assess_market_opportunity_technical", return_value=(False, "未满足回踩确认", "standard")):
            signals = engine.evaluate_market_opportunity_signals(
                [row], 1, global_context=context, now=datetime(2026, 8, 11, 10, 0)
            )
        self.assertEqual(signals, [])

    def test_health_status_preserves_last_success_context(self):
        with TemporaryDirectory() as tmp:
            health_path = __import__("pathlib").Path(tmp) / "signal_health.json"
            health_path.write_text(
                '{"engine_status":"ok","last_success_at":"2026-08-11 10:00:22","global_risk":{"risk_level":"green"}}',
                encoding="utf-8",
            )
            with patch.object(engine, "SIGNAL_HEALTH", health_path), \
                patch.object(engine, "now_dt", return_value=datetime(2026, 8, 11, 10, 1, 0)):
                health = engine.write_health("engine_error", "candidate evaluator failed")
        self.assertEqual(health["last_success_at"], "2026-08-11 10:00:22")
        self.assertEqual(health["global_risk"]["risk_level"], "green")
        self.assertEqual(health["last_error"], "candidate evaluator failed")

    def test_dashboard_marks_mismatched_afterclose_report_stale(self):
        with TemporaryDirectory() as tmp:
            path = __import__("pathlib").Path(tmp) / "同花顺我的股票盘后订盘_2026-08-11.md"
            path.write_text(
                "# 同花顺我的股票下个交易日订盘建议｜2026-08-11（基于 2026-08-10 收盘）\n\n- 生成时间：2026-08-10 16:30:00\n",
                encoding="utf-8",
            )
            report = dashboard.parse_report(path)
        self.assertTrue(report["report_freshness"]["stale_afterclose"])

    def test_v2_entry_with_insufficient_reward_risk_is_blocked(self):
        # 172.55 -> 173.75 against 171.64 yields 1.32, below the V2 threshold.
        signal = v2_signal(173.75)
        with patch.dict("os.environ", {}, clear=False):
            result = build_discipline(signal, "BUY", now_session_allows=True)
        self.assertFalse(result["passed"])
        self.assertIn("已知压力下盈亏比必须达标", result["failed_checks"])
        rr_check = next(item for item in result["checks"] if item["name"] == "已知压力下盈亏比必须达标")
        self.assertIn("门槛 1.50", rr_check["detail"])

    def test_v2_closed_bar_entry_can_pass(self):
        # 172.55 -> 174.95 against 171.64 yields 2.63 after all V2 gates close.
        signal = v2_signal(174.95)
        with patch.dict("os.environ", {}, clear=False):
            result = build_discipline(signal, "BUY", now_session_allows=True)
        self.assertTrue(result["passed"], result["summary"])

    def test_v2_uses_its_own_dynamic_resistance_not_legacy_pressure(self):
        signal = v2_signal(36.84)
        signal.update({
            "current_price": 36.63,
            "trigger_price": 36.63,
            "execution_band_low": 36.60,
            "execution_band_high": 36.66,
            "invalid_price": 36.47,
            "pressure_price": 36.84,
            "amount_ratio_1m": 1.20,
            "amount_ratio_5m": 1.15,
        })
        signal["timing_v2"].update({
            "room_risk": {"entry_reference": 36.63, "reward_risk": 4.12, "execution_band_low": 36.60, "execution_band_high": 36.66},
            "levels": {"structural_invalidation": 36.47, "nearest_resistance": 37.27},
            "execution_gates": {"volume_confirmed": True, "vwap_distance_ok": True},
            "metrics": {"rvol_1m": 1.20, "rvol_5m": 1.15},
        })
        result = build_discipline(signal, "BUY", now_session_allows=True)
        self.assertTrue(result["passed"], result["summary"])
        rr_check = next(item for item in result["checks"] if item["name"] == "已知压力下盈亏比必须达标")
        self.assertIn("阻力 37.27", rr_check["detail"])

    def test_broad_strength_is_not_reported_as_neutral(self):
        quotes = {
            "000001": {"pct": 0.20},
            "399001": {"pct": 0.30},
            "399006": {"pct": 0.25},
        }
        regime = engine.build_a_share_market_regime(
            quotes,
            now=datetime(2026, 8, 20, 14, 30),
            breadth={"state": "broad_strong", "up_ratio": 0.85},
        )
        self.assertEqual(regime["state"], "broad_strong")

    def test_v2_entry_without_5m_vwap_reclaim_is_blocked(self):
        signal = v2_signal(174.95)
        signal["timing_v2"]["execution_5m"] = "FAILURE"
        result = build_discipline(signal, "BUY", now_session_allows=True)
        self.assertFalse(result["passed"])
        self.assertIn("必须符合小周期确认", result["failed_checks"])

    def test_retired_market_opportunity_scenario_is_not_executable(self):
        signal = market_signal(174.95)
        result = build_discipline(signal, "BUY", now_session_allows=True)
        self.assertFalse(result["passed"])
        self.assertIn("场景必须允许进攻买入", result["failed_checks"])

    def test_stale_minute_data_blocks_any_buy(self):
        signal = market_signal(174.95)
        signal.update({"minute_data_fresh": False, "minute_data_fresh_reason": "分钟线已滞后 20 分钟"})
        result = build_discipline(signal, "BUY", now_session_allows=True)
        self.assertFalse(result["passed"])
        self.assertIn("分钟线必须新鲜", result["failed_checks"])

    def test_v2_entry_requires_matching_closed_bar_pattern(self):
        signal = v2_signal(174.95)
        signal["timing_v2"]["entry_pattern"] = "V2_E5_LEADER_SECOND_LEG"
        result = build_discipline(signal, "BUY", now_session_allows=True)
        self.assertFalse(result["passed"])
        self.assertIn("必须符合小周期确认", result["failed_checks"])


class MarketOpportunityGlobalGateTests(unittest.TestCase):
    @staticmethod
    def independent_rotation_row(industry="医药生物", board_pct=1.25, amount_1m=1.20, amount_5m=1.10):
        return {
            "code": "603127",
            "industry": industry,
            "focus": industry,
            "quote": {"code": "603127", "name": "测试候选", "close": 50.0},
            "sector_momentum": {
                "board_name": industry,
                "board_pct": board_pct,
                "emotion_ok": board_pct >= 0.8,
                "reason": f"{industry} {board_pct:.2f}%",
            },
            "sector_rotation": {
                "available": True,
                "sustained": True,
                "leader_healthy": True,
                "reason": "测试板块持续与领涨承接",
            },
            "rt_features": {"amount_ratio_1m": amount_1m, "amount_ratio_5m": amount_5m},
        }

    def test_red_gate_is_observation_only(self):
        context = {"risk_level": "red", "policy": {"allow_market_opportunity_buy": False}}
        self.assertFalse(global_risk.market_opportunity_buy_allowed(context, now=datetime(2026, 7, 20, 10, 0)))
        self.assertIn("红色门控阻断", global_risk.market_opportunity_gate_reason(context))

    def test_yellow_gate_can_remain_open(self):
        context = {"risk_level": "yellow", "policy": {"allow_market_opportunity_buy": True}}
        self.assertTrue(global_risk.market_opportunity_buy_allowed(context, now=datetime(2026, 7, 20, 10, 0)))

    def test_technology_shock_blocks_technology_but_allows_confirmed_nontech_rotation(self):
        context = {
            "risk_level": "red",
            "risk_scope": "technology",
            "policy": global_risk.policy_for_level("red", risk_scope="technology"),
        }
        tech = self.independent_rotation_row(industry="半导体", board_pct=2.1)
        allowed, stage, _ = global_risk.market_opportunity_gate_status(context, row=tech)
        self.assertFalse(allowed)
        self.assertEqual(stage, "TECH_RISK_BLOCKED")

        rotation = self.independent_rotation_row()
        allowed, stage, reason = global_risk.market_opportunity_gate_status(context, row=rotation)
        self.assertTrue(allowed, reason)
        self.assertEqual(stage, "SECTOR_ROTATION_ALLOWED")
        self.assertIn("医药生物", reason)

    def test_nontech_rotation_needs_board_emotion_and_minute_volume(self):
        context = {
            "risk_level": "red",
            "risk_scope": "technology",
            "policy": global_risk.policy_for_level("red", risk_scope="technology"),
        }
        weak_board = self.independent_rotation_row(board_pct=0.55)
        allowed, stage, reason = global_risk.market_opportunity_gate_status(context, row=weak_board)
        self.assertFalse(allowed)
        self.assertEqual(stage, "SECTOR_ROTATION_BLOCKED")
        self.assertIn("板块情绪确认", reason)

        weak_volume = self.independent_rotation_row(amount_1m=1.25, amount_5m=0.82)
        allowed, stage, reason = global_risk.market_opportunity_gate_status(context, row=weak_volume)
        self.assertFalse(allowed)
        self.assertEqual(stage, "SECTOR_ROTATION_BLOCKED")
        self.assertIn("量能", reason)

    def test_broad_red_risk_still_blocks_nontech_rotation(self):
        context = {
            "risk_level": "red",
            "risk_scope": "broad",
            "policy": global_risk.policy_for_level("red"),
        }
        allowed, stage, _ = global_risk.market_opportunity_gate_status(context, row=self.independent_rotation_row())
        self.assertFalse(allowed)
        self.assertEqual(stage, "GLOBAL_RISK_BLOCKED")

    def test_premarket_classifies_tech_shock_and_broad_shock_separately(self):
        tech_only = global_risk.infer_premarket_context(
            "2026-08-11",
            [
                {"name": "韩国综合指数", "pct": -4.3},
                {"name": "日经225", "pct": -0.3},
                {"name": "纳斯达克", "pct": -0.7},
                {"name": "标普500", "pct": -0.2},
                {"name": "恒生指数", "pct": -0.1},
            ],
        )
        self.assertEqual(tech_only["risk_scope"], "technology")
        self.assertTrue(tech_only["policy"]["allow_non_tech_sector_buy"])

        broad = global_risk.infer_premarket_context(
            "2026-08-11",
            [
                {"name": "韩国综合指数", "pct": -4.3},
                {"name": "日经225", "pct": -2.1},
                {"name": "纳斯达克", "pct": -2.1},
                {"name": "标普500", "pct": -1.8},
                {"name": "恒生指数", "pct": -1.6},
            ],
        )
        self.assertEqual(broad["risk_scope"], "broad")
        self.assertTrue(broad["policy"]["allow_market_opportunity_buy"])
        self.assertEqual(broad["entry_risk_source"], "external_only")

    def test_deep_v_recovery_is_limited_for_late_overnight_entry(self):
        policy = global_risk.apply_intraday_recovery_policy(
            global_risk.policy_for_level("green"),
            {"event_state": "deep_v_recovery"},
        )
        self.assertTrue(policy["allow_market_opportunity_buy"])
        self.assertTrue(policy["recovery_overnight_guard"])
        self.assertEqual(policy["recovery_overnight_entry_position_cap_pct"], 2.0)
        self.assertEqual(policy["recovery_overnight_t1_cap_pct"], 2.5)

    def test_high_open_reentry_requires_a_real_cancel_then_reclaim(self):
        quote = {"open": 102.0, "prev_close": 100.0}
        features = {
            "vwap": 101.0,
            "or15_high": 101.5,
            "or15_mid": 101.2,
            "epsilon": 0.10,
            "last10_prices": [100.85, 101.1, 101.3, 101.45, 101.55, 101.6, 101.65],
            "amount_ratio_1m": 1.45,
            "amount_ratio_5m": 1.20,
        }
        ok, reason = engine.high_open_reclaim_ready(
            quote, features, 101.65, now=datetime(2026, 7, 27, 10, 5)
        )
        self.assertTrue(ok, reason)

        features["last10_prices"] = [101.05, 101.2, 101.35, 101.5, 101.55, 101.6, 101.65]
        ok, reason = engine.high_open_reclaim_ready(
            quote, features, 101.65, now=datetime(2026, 7, 27, 10, 5)
        )
        self.assertFalse(ok)
        self.assertIn("取消路径", reason)

    def test_recovery_overlay_caps_the_paper_entry_not_the_signal(self):
        with TemporaryDirectory() as tmp:
            con = paper_trading.init_db(__import__("pathlib").Path(tmp))
            plan = paper_trading.buy_position_plan(
                con,
                {
                    "scenario": "MARKET_OPPORTUNITY_ACTIONABLE",
                    "symbol": "600000",
                    "current_price": 100.0,
                    "global_risk_level": "green",
                    "recovery_overnight_guard": True,
                    "entry_position_cap_pct": 0.02,
                },
                account={"cash": 1_000_000.0, "total_assets_estimate": 1_000_000.0},
            )
        self.assertTrue(plan["passed"], plan["reason"])
        self.assertLessEqual(plan["projected_position_pct"], 2.0)
        self.assertIn("深V修复晚段", plan["reason"])

    def test_retired_market_candidate_entrypoint_writes_no_ledger_or_signal(self):
        con = sqlite3.connect(":memory:")
        con.execute(
            """
            CREATE TABLE market_decision_events (
              id TEXT PRIMARY KEY, trading_date TEXT, symbol TEXT, stage TEXT,
              reason TEXT, payload_json TEXT, created_at TEXT
            )
            """
        )
        row = {"code": "600000", "quote": {"code": "600000", "name": "测试", "close": 100.0}}
        engine.evaluate_market_opportunity_signals(
            [row],
            1,
            global_context={"risk_level": "red", "policy": {"allow_market_opportunity_buy": False}},
            now=datetime(2026, 7, 27, 10, 0),
            decision_con=con,
        )
        stages = [item[0] for item in con.execute("SELECT stage FROM market_decision_events ORDER BY stage")]
        self.assertEqual(stages, [])

    def test_stale_opening_global_quote_does_not_create_a_false_path_high(self):
        stale = [
            {"name": "韩国综合指数", "pct": 17.91, "cross_check": {"pct": -4.10, "source": "新浪"}},
            {"name": "日经225", "pct": 4.03, "cross_check": {"pct": -2.20, "source": "新浪"}},
        ]
        current = [
            {"name": "韩国综合指数", "pct": -4.48, "cross_check": {"pct": -4.50, "source": "新浪"}},
            {"name": "日经225", "pct": -2.15, "cross_check": {"pct": -2.16, "source": "新浪"}},
        ]
        with TemporaryDirectory() as tmp:
            base_dir = __import__("pathlib").Path(tmp)
            global_risk.record_intraday_path(base_dir, "2026-08-03", stale, now=datetime(2026, 8, 3, 8, 0))
            state = global_risk.record_intraday_path(base_dir, "2026-08-03", current, now=datetime(2026, 8, 3, 9, 25))
        self.assertEqual(state["markets"]["kospi"]["max_pct"], -4.1)
        self.assertEqual(state["markets"]["nikkei"]["max_pct"], -2.15)
        self.assertGreaterEqual(state["markets"]["kospi"]["drawdown_from_high_pct"], -0.4)

    def test_afternoon_external_fade_blocks_new_opportunity_buy(self):
        context = {
            "risk_level": "green",
            "policy": {"allow_market_opportunity_buy": True},
            "intraday_path": {
                "markets": {
                    "kospi": {"current_pct": 0.74, "drawdown_from_high_pct": -5.43},
                    "nikkei": {"current_pct": -0.18, "drawdown_from_high_pct": -2.20},
                }
            },
        }
        now = datetime(2026, 7, 22, 13, 5)
        tech_row = self.independent_rotation_row(industry="半导体", board_pct=1.8)
        self.assertFalse(global_risk.market_opportunity_buy_allowed(context, row=tech_row, now=now))
        self.assertIn("午后外部科技动能衰减", global_risk.market_opportunity_gate_reason(context, row=tech_row, now=now))

    def test_a_share_regime_turns_risk_off_on_growth_selloff(self):
        cache = {}
        neutral = engine.build_a_share_market_regime(
            {
                "000001": {"pct": 0.50},
                "399001": {"pct": 0.60},
                "399006": {"pct": -0.20},
            },
            cache=cache,
            now=datetime(2026, 7, 22, 13, 0),
        )
        risk_off = engine.build_a_share_market_regime(
            {
                "000001": {"pct": -0.35},
                "399001": {"pct": -1.76},
                "399006": {"pct": -3.38},
            },
            cache=cache,
            now=datetime(2026, 7, 22, 14, 15),
        )
        self.assertEqual(neutral["state"], "neutral")
        self.assertEqual(risk_off["state"], "risk_off")
        self.assertIn("禁止新增", risk_off["action"])

    def test_growth_lead_is_not_blocked_by_a_weak_shanghai_index(self):
        regime = engine.build_a_share_market_regime(
            {
                "000001": {"pct": -0.72},
                "399001": {"pct": 1.20},
                "399006": {"pct": 2.10},
            },
            cache={},
            now=datetime(2026, 8, 4, 14, 0),
        )
        self.assertEqual(regime["state"], "growth_lead")
        self.assertTrue(global_risk.market_opportunity_buy_allowed({
            "risk_level": "green",
            "policy": {"allow_market_opportunity_buy": True},
            "a_share_market_regime": regime,
        }))

    def test_local_regime_block_is_recorded_separately_from_global_risk(self):
        context = {
            "risk_level": "green",
            "policy": {"allow_market_opportunity_buy": True},
            "a_share_market_regime": {"state": "transition", "action": "本地转弱"},
        }
        allowed, stage, reason = global_risk.market_opportunity_gate_status(context)
        self.assertFalse(allowed)
        self.assertEqual(stage, "A_SHARE_REGIME_BLOCKED")
        self.assertEqual(reason, "本地转弱")

    def test_growth_lead_reopens_the_radar_before_existing_technical_checks(self):
        row = {"code": "600000", "quote": {"code": "600000", "name": "测试", "close": 100.0}}
        context = {
            "risk_level": "green",
            "policy": {"allow_market_opportunity_buy": True},
            "a_share_market_regime": {"state": "growth_lead"},
        }
        signals = engine.evaluate_signals([row], {}, {}, True, global_context=context)
        self.assertEqual(len(signals), 1)
        self.assertEqual(signals[0]["scenario"], "V2_WAIT")

    def test_legacy_raw_timing_pattern_cannot_open_a_flat_paper_account(self):
        with TemporaryDirectory() as tmp:
            con = paper_trading.init_db(__import__("pathlib").Path(tmp))
            plan = paper_trading.buy_position_plan(
                con,
                {
                    "scenario": "V2_E1_TREND_PULLBACK_RECLAIM",
                    "symbol": "600000",
                    "current_price": 100.0,
                    "strategy_family": "LEGACY_BLOCKED",
                    "position_multiplier": 0.33,
                    "premarket_plan_action": "顺势分批",
                    "premarket_plan_allows_entry": True,
                    "premarket_plan_complete": True,
                    "planned_target_position_pct": 0.06,
                    "planned_max_position_pct": 0.08,
                    "planned_v2_probe_position_pct": 0.02,
                },
                account={"cash": 1_000_000.0, "total_assets_estimate": 1_000_000.0},
            )
        self.assertIsNone(paper_trading.signal_side({"scenario": "V2_E1_TREND_PULLBACK_RECLAIM"}))

    def test_stale_a_share_regime_blocks_market_opportunity_buy(self):
        context = {
            "risk_level": "green",
            "policy": {"allow_market_opportunity_buy": True},
            "a_share_market_regime": {
                "state": "data_stale",
                "action": "指数数据未完成刷新，新增机会只观察",
            },
        }
        self.assertFalse(global_risk.market_opportunity_buy_allowed(context))
        self.assertIn("指数数据未完成刷新", global_risk.market_opportunity_gate_reason(context))

    def test_stale_tracked_candidate_is_not_actionable(self):
        row = {
            "quote": {"close": 100.32, "pct": 3.0, "amount_wan": 100000},
            "dynamic": {"vwap": 100.0},
            "rt_features": {"vwap": 100.0, "epsilon": 0.10, "amount_ratio_1m": 1.40, "amount_ratio_5m": 1.20},
            "axes": {"position": "green", "odds": "green"},
            "tracked_candidate": True,
            "candidate_source_age_sec": engine.RADAR_CANDIDATE_MAX_AGE_SECONDS + 1,
        }
        blockers = engine.radar_signal_blockers(row, now=datetime(2026, 7, 22, 10, 0))
        self.assertTrue(any("候选来源已过期" in item for item in blockers))

    def test_fill_price_path_reads_post_fill_snapshots(self):
        with TemporaryDirectory() as tmp:
            base_dir = __import__("pathlib").Path(tmp)
            con = paper_trading.init_db(base_dir)
            con.execute(
                "INSERT INTO paper_position_snapshots VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                ("2026-07-22 13:08:54", "2026-07-22", "000977", "测试", 300, 0, 93.79, 93.10, 27930, 28137, -207, -0.74, "test"),
            )
            con.commit()
            path = paper_trading.fill_price_path(base_dir, "2026-07-22", "000977", "2026-07-22 13:03:54")
        self.assertAlmostEqual(path["300"], 93.10)

    def test_fill_price_path_reads_marks_after_full_exit(self):
        with TemporaryDirectory() as tmp:
            base_dir = __import__("pathlib").Path(tmp)
            con = paper_trading.init_db(base_dir)
            con.execute(
                """
                INSERT INTO paper_orders (
                  order_id, trading_date, created_at, symbol, name, scenario,
                  side, qty, fill_price, status
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "sell-1", "2026-07-24", "2026-07-24 10:37:23", "002436",
                    "测试", "P0_HARD_RISK_REDUCE", "SELL", 800, 34.02, "FILLED",
                ),
            )
            con.commit()
            written = paper_trading.record_filled_order_marks(
                con,
                "2026-07-24",
                {"002436": {"last_price": 33.88}},
                now=datetime(2026, 7, 24, 10, 53, 0),
            )
            path = paper_trading.fill_price_path(
                base_dir, "2026-07-24", "002436", "2026-07-24 10:37:23"
            )
        self.assertEqual(written, 1)
        self.assertAlmostEqual(path["900"], 33.88)

    def test_green_confirmation_allows_only_local_confirmed_early_volume(self):
        row = {
            "code": "600000",
            "quote": {"code": "600000", "name": "测试科技", "close": 100.32, "pct": 3.0, "amount_wan": 100000},
            "dynamic": {"vwap": 100.0},
            "rt_features": {
                "vwap": 100.0,
                "epsilon": 0.10,
                "amount_ratio_1m": 0.90,
                "amount_ratio_5m": 1.00,
                "atr5m": 0.40,
                "last10_prices": [99.0, 99.2, 100.0, 99.8, 100.05, 100.30, 100.25, 100.35, 100.30, 100.32],
                "last5_prices": [100.25, 100.35, 100.30, 100.32, 100.32],
                "last3_prices": [100.20, 100.26, 100.32],
                "minutes": 30, "h60": 100.35, "l60": 99.0,
                "available": True, "last_minute_time": "09:59:30",
            },
            "axes": {"recognition": "green", "position": "green", "odds": "green"},
            "framework_context": {"large_cycle_ready": True, "chips": {"state": "supportive"}, "time": {"state": "normal"}},
        }
        context = {"risk_level": "green", "policy": {"allow_market_opportunity_buy": True}}
        with patch.object(engine.intra, "radar_opportunity_status", return_value="🟢 替代机会"), \
            patch.object(engine.intra, "amount_yi", return_value=10.0), \
            patch.object(engine, "rapid_rise", return_value=False):
            strict_ok, _ = engine.radar_signal_gate(row, now=datetime(2026, 7, 22, 10, 0))
            green_ok, reason = engine.green_confirmation_gate(
                row, context, now=datetime(2026, 7, 22, 10, 0)
            )
        self.assertFalse(strict_ok)
        self.assertTrue(green_ok, reason)

    def test_green_confirmation_does_not_open_yellow_or_red(self):
        context = {"risk_level": "yellow", "policy": {"allow_market_opportunity_buy": True}}
        ok, reason = engine.green_confirmation_gate({}, context, now=datetime(2026, 7, 22, 10, 0))
        self.assertFalse(ok)
        self.assertIn("绿色确认通道", reason)

    def test_strict_gate_reports_volume_blocker_once(self):
        row = {
            "quote": {"code": "600000", "name": "测试科技", "close": 100.32, "pct": 3.0, "amount_wan": 100000},
            "dynamic": {"vwap": 100.0},
            "rt_features": {"vwap": 100.0, "epsilon": 0.10, "amount_ratio_1m": 0.80, "amount_ratio_5m": 0.80, "minutes": 30, "h60": 100.35, "l60": 99.0, "available": True, "last_minute_time": "09:59:30"},
            "axes": {"recognition": "green", "position": "green", "odds": "green"},
            "framework_context": {"large_cycle_ready": True, "chips": {"state": "supportive"}, "time": {"state": "normal"}},
        }
        with patch.object(engine.intra, "radar_opportunity_status", return_value="🟢 替代机会"), \
            patch.object(engine.intra, "amount_yi", return_value=10.0), \
            patch.object(engine, "rapid_rise", return_value=False):
            ok, reason = engine.radar_signal_gate(row, now=datetime(2026, 7, 22, 10, 0))
        self.assertFalse(ok)
        self.assertEqual(reason.count("量能未达"), 1)

    def test_strict_gate_success_path_is_stable(self):
        row = {
            "quote": {"code": "600000", "name": "测试科技", "close": 100.32, "pct": 3.0, "amount_wan": 100000},
            "dynamic": {"vwap": 100.0},
            "rt_features": {
                "vwap": 100.0,
                "epsilon": 0.10,
                "amount_ratio_1m": 1.40,
                "amount_ratio_5m": 1.20,
                "atr5m": 0.40,
                "last10_prices": [99.0, 99.2, 100.0, 99.8, 100.05, 100.30, 100.25, 100.35, 100.30, 100.32],
                "last5_prices": [100.25, 100.35, 100.30, 100.32, 100.32],
                "last3_prices": [100.20, 100.26, 100.32],
                "minutes": 30, "h60": 100.35, "l60": 99.0,
                "available": True, "last_minute_time": "09:59:30",
            },
            "axes": {"recognition": "green", "position": "green", "odds": "green"},
            "framework_context": {"large_cycle_ready": True, "chips": {"state": "supportive"}, "time": {"state": "normal"}},
        }
        with patch.object(engine.intra, "radar_opportunity_status", return_value="🟢 替代机会"), \
            patch.object(engine.intra, "amount_yi", return_value=10.0), \
            patch.object(engine, "rapid_rise", return_value=False):
            ok, reason = engine.radar_signal_gate(row, now=datetime(2026, 7, 22, 10, 0))
        self.assertTrue(ok, reason)

    def test_market_opportunity_green_confirmation_cannot_bypass_strategy_contract(self):
        row = {
            "code": "600000",
            "quote": {"code": "600000", "name": "测试科技", "close": 100.32, "pct": 3.0, "amount_wan": 100000},
            "dynamic": {"vwap": 100.0},
            "rt_features": {
                "vwap": 100.0,
                "epsilon": 0.10,
                "amount_ratio_1m": 0.90,
                "amount_ratio_5m": 1.00,
                "atr5m": 0.40,
                "last10_prices": [99.0, 99.2, 100.0, 99.8, 100.05, 100.30, 100.25, 100.35, 100.30, 100.32],
                "last5_prices": [100.25, 100.35, 100.30, 100.32, 100.32],
                "last3_prices": [100.20, 100.26, 100.32],
                "minutes": 30, "h60": 100.35, "l60": 99.0,
                "available": True, "last_minute_time": "09:59:30",
            },
            "axes": {"recognition": "green", "position": "green", "odds": "green"},
            "framework_context": {"large_cycle_ready": True, "chips": {"state": "supportive"}, "time": {"state": "normal"}},
        }
        context = {"risk_level": "green", "policy": {"allow_market_opportunity_buy": True}}
        with patch.object(engine.intra, "radar_opportunity_status", return_value="🟢 替代机会"), \
            patch.object(engine.intra, "amount_yi", return_value=10.0), \
            patch.object(engine, "rapid_rise", return_value=False):
            signals = engine.evaluate_market_opportunity_signals(
                [row], 3, global_context=context, now=datetime(2026, 7, 22, 10, 0)
            )
        self.assertEqual(signals, [])

    def test_minute_bar_health_reports_freshness(self):
        health = engine.minute_bar_health(
            [{"available": True, "last_minute_time": "09:59:30"}],
            now=datetime(2026, 7, 22, 10, 0),
        )
        self.assertTrue(health["available"])
        self.assertEqual(health["symbols"], 1)
        self.assertAlmostEqual(health["delay_sec"], 30.0)

    def test_market_opportunity_needs_60_minute_reclaim(self):
        ok, reason = engine.sixty_minute_structure_gate(
            {"minutes": 45, "h60": 100.0, "l60": 90.0, "epsilon": 0.1},
            92.5,
        )
        self.assertFalse(ok)
        self.assertIn("38.2%", reason)

    def test_previous_shock_is_carried_into_preopen_repair(self):
        old_quotes = [
            {"name": "韩国综合指数", "pct": -4.46},
            {"name": "日经225", "pct": -4.03},
            {"name": "恒生指数", "pct": -2.10},
        ]
        repaired_quotes = [
            {"name": "韩国综合指数", "pct": 0.08},
            {"name": "日经225", "pct": 1.18},
            {"name": "恒生指数", "pct": 2.36},
        ]
        with TemporaryDirectory() as tmp:
            base_dir = __import__("pathlib").Path(tmp)
            global_risk.record_intraday_path(base_dir, "2026-07-20", old_quotes, now=datetime(2026, 7, 20, 15, 0))
            state = global_risk.record_intraday_path(base_dir, "2026-07-21", repaired_quotes, now=datetime(2026, 7, 21, 8, 30))
        self.assertTrue(state["preopen_repair"])
        self.assertTrue(state["recovery_confirmed"])
        self.assertEqual(state["event_state"], "preopen_recovery")
        self.assertEqual(state["previous_kospi_current_pct"], -4.46)

    def test_repair_continuation_keeps_local_confirmation(self):
        row = {
            "code": "600000",
            "quote": {"code": "600000", "name": "测试科技", "close": 102.0, "pct": 3.5, "amount_wan": 100000},
            "dynamic": {"vwap": 100.0},
            "rt_features": {
                "vwap": 100.0,
                "epsilon": 0.10,
                "amount_ratio_1m": 1.50,
                "amount_ratio_5m": 1.20,
                "atr5m": 0.50,
                "last3_prices": [101.2, 101.6, 102.0],
                "minutes": 30, "h60": 102.0, "l60": 99.0,
                "available": True, "last_minute_time": "09:59:30",
            },
            "axes": {"position": "green", "odds": "green"},
            "framework_context": {"large_cycle_ready": True, "chips": {"state": "supportive"}, "time": {"state": "normal"}},
        }
        context = {
            "risk_level": "yellow",
            "policy": {"allow_market_opportunity_buy": True},
            "intraday_path": {"preopen_repair": True},
        }
        with patch.object(engine.intra, "radar_opportunity_status", return_value="🟢 替代机会"), patch.object(engine.intra, "amount_yi", return_value=10.0), patch.object(engine, "rapid_rise", return_value=False):
            ok, reason = engine.repair_continuation_gate(row, context, now=datetime(2026, 7, 21, 10, 0))
        self.assertTrue(ok, reason)

    def test_dashboard_does_not_label_unconfirmed_candidate_as_entry(self):
        row = dashboard.enrich_market_opportunity({
            "机会代码": "600000",
            "机会名称": "测试科技",
            "雷达状态": "🟢 替代机会",
            "模拟门控": "现价离VWAP过远",
            "触发价": "103.00",
            "失效价": "99.00",
            "动作": "候选观察，暂不模拟成交",
        })
        alert = dashboard.entry_alert_from_market_opportunity(row, {"risk_level": "green", "policy": {"allow_market_opportunity_buy": True}})
        self.assertEqual(alert["priority"], "P2")
        self.assertEqual(alert["label"], "观察候选（未触发）")

    def test_dashboard_does_not_duplicate_technical_gate_reason(self):
        row = dashboard.enrich_market_opportunity({
            "机会代码": "600000",
            "机会名称": "测试科技",
            "雷达状态": "🟢 替代机会",
            "模拟门控": "量能不足",
            "触发价": "103.00",
            "失效价": "99.00",
            "动作": "技术门控未通过：量能不足；候选观察，暂不模拟成交",
        })
        dashboard.apply_market_opportunity_gate(
            [row],
            {"risk_level": "green", "policy": {"allow_market_opportunity_buy": True}},
        )
        self.assertEqual(row["动作"].count("技术门控未通过"), 1)
        self.assertEqual(row["动作"].count("量能不足"), 1)


if __name__ == "__main__":
    unittest.main()
