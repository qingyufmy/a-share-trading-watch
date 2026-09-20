import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import intraday_report as intra


class RuntimeLevelFallbackTests(unittest.TestCase):
    def test_snapshot_requires_rating_status_and_frozen_contract_for_entry(self):
        base_signal = {
            "strategy_family": "THREE_METHOD",
            "scenario": "STRATEGY_520_ENTRY",
            "name": "测试股",
            "symbol": "600000",
            "current_price": 10.00,
            "external_status": "立即处理",
            "opportunity_grade": "A",
            "opportunity_hard_veto": [],
            "timing_v2": {"entry_allowed": True},
            "contract_errors": [],
            "signal_contract": {
                "side": "BUY",
                "sim_allowed": True,
                "exec_low": 9.98,
                "exec_high": 10.02,
                "expires_at": "2099-01-01 10:00:00",
            },
        }
        blocked = {**base_signal, "symbol": "600001", "opportunity_grade": "C"}
        observed = {**base_signal, "symbol": "600002", "external_status": "观察"}
        no_contract = {**base_signal, "symbol": "600003", "signal_contract": {"sim_allowed": False}}
        payload = {"signals": [base_signal, blocked, observed, no_contract], "health": {}}

        with patch.object(intra, "load_latest_realtime_signals", return_value=payload):
            snapshot = intra.v2_realtime_snapshot()

        self.assertEqual([item["symbol"] for item in snapshot["executable"]], ["600000"])
        self.assertEqual({item["symbol"] for item in snapshot["waits"]}, {"600001", "600002", "600003"})

    def test_legacy_raw_timing_signal_is_absent_from_active_snapshot(self):
        legacy = {
            "strategy_family": "V2_ONLY",
            "scenario": "V2_E1_TREND_PULLBACK_RECLAIM",
            "symbol": "600000",
            "external_status": "立即处理",
            "opportunity_grade": "A",
            "timing_v2": {"entry_allowed": True},
            "signal_contract": {"side": "BUY", "sim_allowed": True},
        }
        with patch.object(intra, "load_latest_realtime_signals", return_value={"signals": [legacy], "health": {}}):
            snapshot = intra.v2_realtime_snapshot()
        self.assertEqual(snapshot["signals"], [])
        self.assertEqual(snapshot["executable"], [])
        self.assertEqual(snapshot["waits"], [])

    def test_tracking_funnel_and_grade_summary_are_explainable(self):
        signals = [
            {"tracking": {"state": "DISCOVERED"}, "opportunity_grade": "C"},
            {"tracking": {"state": "NEAR_TRIGGER"}, "opportunity_grade": "A"},
            {"tracking": {"state": "DATA_BLOCKED"}, "opportunity_grade": "D"},
        ]

        funnel = {item["state"]: item["count"] for item in intra.tracking_funnel_summary(signals)}
        grades = intra.opportunity_grade_summary(signals)

        self.assertEqual(funnel["DISCOVERED"], 1)
        self.assertEqual(funnel["NEAR_TRIGGER"], 1)
        self.assertEqual(funnel["DATA_BLOCKED"], 1)
        self.assertEqual(grades, {"A": 1, "B": 0, "C": 1, "D": 1})

    def test_wait_summary_deduplicates_categories_per_symbol(self):
        waits = [{
            "symbol": "000001",
            "timing_v2": {
                "blocker_diagnostics": [
                    {"reason": "120分钟BEAR，不允许新开多", "severity": "HARD"},
                    {"reason": "结构状态 BEAR 不允许新开多", "severity": "HARD"},
                    {"reason": "5分钟未形成抬高低点", "severity": "TEMPORARY"},
                ]
            },
        }]

        summary = intra.summarize_wait_blockers(waits)

        self.assertEqual(summary["hard"], 1)
        self.assertEqual(summary["temporary_only"], 0)
        categories = {item["key"]: item["count"] for item in summary["categories"]}
        self.assertEqual(categories["structure"], 1)
        self.assertEqual(categories["execution"], 1)

    def test_premarket_ineligible_wait_is_not_near_ready(self):
        signal = {
            "strategy_family": "V2_ONLY",
            "scenario": "V2_WAIT",
            "symbol": "600001",
            "reasons": ["盘前计划=未加载当日盘前仓位计划，当日不允许新增试仓"],
            "timing_v2": {
                "regime": "TREND",
                "location": "TIER1_SUPPORT",
                "setup_15m": "TREND_PULLBACK_SETUP",
                "execution_5m": "VWAP_HOLD",
                "blocker_diagnostics": [
                    {"reason": "5分钟未形成抬高低点", "severity": "TEMPORARY"}
                ],
            },
        }

        summary = intra.summarize_wait_blockers([signal])

        self.assertEqual(summary["structural_hard"], 1)
        self.assertEqual(intra.near_ready_waits([signal]), [])

    def test_extension_risk_is_not_near_ready(self):
        signal = {
            "strategy_family": "V2_ONLY",
            "scenario": "V2_WAIT",
            "symbol": "600002",
            "timing_v2": {
                "regime": "TREND",
                "location": "STRUCTURAL_RECLAIM",
                "setup_15m": "TREND_PULLBACK_SETUP",
                "execution_5m": "VWAP_HOLD",
                "blocker_diagnostics": [
                    {"reason": "延伸状态 CLIMAX，禁止追价", "severity": "TEMPORARY"}
                ],
            },
        }

        self.assertEqual(intra.near_ready_waits([signal]), [])

    def test_feishu_card_separates_waits_positions_and_discovery(self):
        temporary = {
            "strategy_family": "V2_ONLY",
            "scenario": "V2_WAIT",
            "name": "趋势候选",
            "symbol": "000001",
            "opportunity_grade": "B",
            "opportunity_score": 74,
            "tracking": {"state": "NEAR_TRIGGER"},
            "timing_v2": {
                "regime": "TREND",
                "location": "TIER1_SUPPORT",
                "setup_15m": "TREND_PULLBACK_SETUP",
                "execution_5m": "VWAP_HOLD",
                "blockers": ["5分钟未形成抬高低点"],
                "blocker_diagnostics": [
                    {"reason": "5分钟未形成抬高低点", "severity": "TEMPORARY"}
                ],
            },
        }
        hard = {
            "strategy_family": "V2_ONLY",
            "scenario": "V2_WAIT",
            "name": "弱势候选",
            "symbol": "000002",
            "opportunity_grade": "D",
            "opportunity_score": 31,
            "tracking": {"state": "DISCOVERED"},
            "timing_v2": {
                "regime": "BEAR",
                "location": "MID_AIR",
                "setup_15m": "WAIT",
                "execution_5m": "FAILURE",
                "blocker_diagnostics": [
                    {"reason": "120分钟BEAR，不允许新开多", "severity": "HARD"}
                ],
            },
        }
        snapshot = {
            "data": {
                "updated_at": "2026-08-26 14:30:05",
                "health": {
                    "engine_status": "ok",
                    "can_attack": True,
                    "paper_positions": 1,
                    "tracked_opportunities_live": 4,
                    "tracked_opportunity_codes": ["600001", "600002"],
                    "radar_actionable_signals": 0,
                    "quote_delay_sec": 2,
                    "minute_bar_delay_sec": 0,
                    "minute_bar_symbols": 48,
                    "a_share_market_regime": {
                        "label": "A股指数中性",
                        "indexes": {"shanghai": {"label": "上证", "pct": 1.0}},
                    },
                    "market_breadth": {
                        "coverage_ratio": 0.95,
                        "coverage_ready": True,
                        "up": 3000,
                        "down": 1500,
                        "state": "mixed",
                    },
                },
            },
            "signals": [temporary, hard],
            "executable": [],
            "waits": [temporary, hard],
        }
        paper_snapshot = {
            "account": {"position_pct": 5.0, "day_pnl": 100, "unrealized_pnl": -50},
            "positions": [{
                "symbol": "000001",
                "name": "趋势候选",
                "quantity": 100,
                "last_price": 10,
                "day_pnl_pct": 1.0,
                "unrealized_pnl_pct": -0.5,
            }],
        }

        card = intra.build_intraday_feishu_card(
            [], {}, [{"f14": "铜", "f3": 3.0}], 1.2,
            radar_rows=[{"code": "600001", "quote": {"name": "发现标的"}}],
            paper_snapshot=paper_snapshot,
            now=__import__("datetime").datetime(2026, 8, 26, 14, 30),
            snapshot=snapshot,
        )
        rendered = json.dumps(card, ensure_ascii=False)

        self.assertIn("结构硬否决 1", rendered)
        self.assertIn("条件型等待 1", rendered)
        self.assertIn("当前无绿色正式买入信号", rendered)
        self.assertNotIn("🟢 绿色正式买入信号", rendered)
        self.assertIn("临界等待（禁止下单）", rendered)
        self.assertIn("仅观察/禁止下单", rendered)
        self.assertIn("趋势候选(000001)", rendered)
        self.assertIn("120m", rendered)
        self.assertIn("下一关", rendered)
        self.assertIn("模拟持仓与退出监控", rendered)
        self.assertIn("发现池 ≠ 盘前资格 ≠ V2信号 ≠ 模拟成交", rendered)
        self.assertIn("状态变化审计（不构成下单）", rendered)
        self.assertIn("A股策略整点盯盘", rendered)
        self.assertIn("整点总览", rendered)
        self.assertIn("跟踪漏斗", rendered)
        self.assertIn("机会评级", rendered)
        self.assertIn("临近触发", rendered)
        self.assertIn("B级 74分", rendered)
        self.assertIn("评级用于质量排序", rendered)
        self.assertIn("下一固定节点：15:00 收盘盯盘", rendered)

    def test_formal_order_reference_requires_fresh_contract_and_execution_band(self):
        signal = {
            "strategy_family": "THREE_METHOD",
            "scenario": "STRATEGY_520_ENTRY",
            "name": "正式候选",
            "symbol": "000001",
            "current_price": 10.02,
            "external_status": "立即处理",
            "opportunity_grade": "A",
            "opportunity_score": 84,
            "opportunity_hard_veto": [],
            "contract_errors": [],
            "timing_v2": {
                "entry_allowed": True,
                "regime": "TREND",
                "setup_15m": "E1_TREND_PULLBACK_RECLAIM",
                "execution_5m": "VWAP_RECLAIM",
                "levels": {"entry_invalidation": 9.80, "nearest_resistance": 10.80},
            },
            "signal_contract": {
                "side": "BUY",
                "sim_allowed": True,
                "exec_low": 9.98,
                "exec_high": 10.05,
                "invalid_price": 9.80,
                "target_price": 10.80,
                "net_reward_risk": 2.10,
                "position_cap_pct": 0.01,
                "expires_at": "2026-08-26 10:01:30",
            },
        }
        now = __import__("datetime").datetime(2026, 8, 26, 10, 1)
        self.assertTrue(intra.is_formal_entry_reference(signal, now=now))
        signal["current_price"] = 10.06
        self.assertFalse(intra.is_formal_entry_reference(signal, now=now))
        signal["current_price"] = 10.02
        expired = __import__("datetime").datetime(2026, 8, 26, 10, 2)
        self.assertFalse(intra.is_formal_entry_reference(signal, now=expired))

        rendered = intra.formal_entry_reference_text([signal])
        self.assertIn("绿色正式买入信号｜立即处理", rendered)
        self.assertIn("9.98～10.05", rendered)
        self.assertIn("试仓上限 1.0%", rendered)
        self.assertIn("入场失效：**9.80**", rendered)
        self.assertIn("第一目标 10.80", rendered)
        self.assertIn("成本后RR 2.10", rendered)
        self.assertIn("有效至：**2026-08-26 10:01:30**", rendered)

    def test_stale_snapshot_never_exposes_formal_order_reference(self):
        signal = {
            "strategy_family": "THREE_METHOD",
            "scenario": "STRATEGY_520_ENTRY",
            "symbol": "600000",
            "current_price": 10.00,
            "external_status": "立即处理",
            "opportunity_grade": "A",
            "opportunity_hard_veto": [],
            "contract_errors": [],
            "timing_v2": {"entry_allowed": True},
            "signal_contract": {
                "side": "BUY", "sim_allowed": True, "exec_low": 9.98, "exec_high": 10.02,
                "expires_at": "2099-01-01 10:00:00",
            },
        }
        payload = {"signals": [signal], "health": {}, "stale": True}
        with patch.object(intra, "load_latest_realtime_signals", return_value=payload):
            snapshot = intra.v2_realtime_snapshot()
        self.assertEqual(snapshot["executable"], [])
        self.assertEqual([item["symbol"] for item in snapshot["waits"]], ["600000"])

    def test_audit_change_reports_cleared_and_added_gate_categories(self):
        previous = {
            "trading_date": "2026-08-26",
            "signals": {
                "000001": {
                    "name": "测试股",
                    "scenario": "V2_WAIT",
                    "tracking_state": "SETUP_FORMING",
                    "opportunity_grade": "C",
                    "categories": ["setup", "execution"],
                }
            },
        }
        current = {
            "trading_date": "2026-08-26",
            "signals": {
                "000001": {
                    "name": "测试股",
                    "scenario": "V2_NO_ADD",
                    "tracking_state": "NEAR_TRIGGER",
                    "opportunity_grade": "B",
                    "categories": ["execution", "extension"],
                }
            },
        }

        rendered = intra.audit_change_text(previous, current)

        self.assertIn("V2_WAIT→V2_NO_ADD", rendered)
        self.assertIn("跟踪结构成形→临近触发", rendered)
        self.assertIn("评级C→B", rendered)
        self.assertIn("解除15m Setup", rendered)
        self.assertIn("新增位置/追高/空间", rendered)

    def test_same_hourly_node_does_not_push_feishu_twice(self):
        now = __import__("datetime").datetime(2026, 8, 26, 14, 0)
        snapshot = {"data": {"health": {}}, "signals": [], "executable": [], "waits": []}
        delivered = {
            "trading_date": "2026-08-26",
            "delivery_key": "2026-08-26 14:00",
            "signals": {},
        }

        with patch.object(intra, "v2_realtime_snapshot", return_value=snapshot), patch.object(
            intra, "load_feishu_audit_state", return_value=delivered
        ), patch.object(intra.base, "current_datetime", return_value=now), patch.object(
            intra.urllib.request, "urlopen"
        ) as urlopen:
            result = json.loads(intra.send_feishu([], {}, [], 0.1))

        self.assertEqual(result["msg"], "skipped duplicate hourly delivery")
        urlopen.assert_not_called()

    def test_realtime_section_excludes_legacy_signals_and_explains_v2_wait(self):
        snapshot = {
            "updated_at": "2026-08-14 11:23:06",
            "stale": False,
            "health": {"engine_status": "ok", "quote_delay_sec": 3},
            "signals": [
                {
                    "strategy_family": "legacy",
                    "scenario": "P0_HARD_RISK_REDUCE",
                    "name": "旧信号",
                    "symbol": "000001",
                },
                {
                    "strategy_family": "THREE_METHOD",
                    "scenario": "V2_WAIT",
                    "name": "V2测试",
                    "symbol": "000831",
                    "timing_v2": {"blockers": ["MID_AIR", "RoomATR 0.93"]},
                },
            ],
        }
        lines = []
        with patch.object(intra, "load_latest_realtime_signals", return_value=snapshot):
            intra.append_realtime_signal_section(lines)

        rendered = "\n".join(lines)
        self.assertIn("三策略实时执行状态", rendered)
        self.assertIn("未形成三策略可执行买卖事件", rendered)
        self.assertIn("MID_AIR", rendered)
        self.assertNotIn("P0_HARD_RISK_REDUCE", rendered)

    def test_intraday_report_does_not_render_legacy_execution_table(self):
        snapshot = {
            "updated_at": "2026-08-14 11:23:06",
            "stale": False,
            "health": {"engine_status": "ok", "quote_delay_sec": 3},
            "signals": [{
                "strategy_family": "THREE_METHOD",
                "scenario": "V2_WAIT",
                "name": "V2测试",
                "symbol": "000831",
                "current_price": 58.0,
                "action": "不预挂固定买入价",
                "timing_v2": {
                    "regime": "TREND",
                    "location": "MID_AIR",
                    "setup_15m": "WAIT",
                    "execution_5m": "WAIT",
                    "levels": {"structural_invalidation": 56.4, "nearest_resistance": 60.0},
                    "blockers": ["MID_AIR"],
                },
            }],
        }
        rows = [{
            "code": "000831",
            "quote": {"name": "中国稀土", "pct": 1.2},
            "signal": {"priority": "P0", "command": "旧指令"},
        }]
        with patch.object(intra, "load_latest_realtime_signals", return_value=snapshot), patch.object(
            intra.base, "current_datetime", return_value=__import__("datetime").datetime(2026, 8, 14, 11, 30)
        ), patch.object(intra.base, "append_paper_position_section"):
            report = intra.make_report(rows, {}, [], 1.0, [], {}, {})

        self.assertIn("三策略核心执行表", report)
        self.assertIn("V2_WAIT", report)
        self.assertNotIn("旧指令", report)
        self.assertNotIn("AI进攻罗盘", report)

    def test_afterclose_plan_supplies_levels_when_premarket_artifact_is_missing(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            base_dir = Path(temp_dir)
            report_path = base_dir / "web_dashboard" / "data" / "reports" / "afterclose_20260813.json"
            report_path.parent.mkdir(parents=True)
            report_path.write_text(json.dumps({
                "rows": [{
                    "代码": "000831",
                    "名称": "中国稀土",
                    "状态": "🟡 关键位观察",
                    "优先级": "P1",
                    "防守/止损（硬失效）": "56.41",
                    "加仓触发": "56.60",
                    "趋势压力": "58.58",
                    "加仓确认": "放量站稳后才观察。",
                }],
            }, ensure_ascii=False), encoding="utf-8")

            with patch.object(intra, "BASE_DIR", base_dir), patch.object(
                intra, "REPORT_DATE", "2026-08-13"
            ), patch.object(intra, "PREMARKET_REPORT", base_dir / "missing.md"), patch.object(
                intra, "current_watchlist_map", return_value={}
            ):
                levels = intra.read_premarket_levels()

        self.assertEqual(set(levels), {"000831"})
        self.assertEqual(levels["000831"]["defense"], 56.41)
        self.assertEqual(levels["000831"]["repair"], 56.60)
        self.assertEqual(levels["000831"]["pressure"], 58.58)
        self.assertEqual(levels["000831"]["level_source"], "afterclose_next_day_plan")
