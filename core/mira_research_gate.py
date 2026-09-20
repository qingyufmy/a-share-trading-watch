"""Mira-style evidence and refresh gate for daily A-share reports.

This module keeps the trading-plan scripts fast while adding a lightweight
research-readiness layer: source scope, claim posture, stale conditions and
action boundaries. It is intentionally heuristic because the current reports
are generated from Markdown plus local runtime snapshots rather than a formal
case-level evidence log.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any


READINESS_LABELS = {
    "draft": "草稿",
    "working_view": "工作观点",
    "research_ready": "研究可用",
    "actionable_with_caveats": "可用于订盘但需条件确认",
    "watch_only": "仅观察",
    "not_actionable": "不可用于行动",
    "needs_refresh": "需要刷新",
}


def _text(record: dict[str, Any]) -> str:
    parts: list[str] = []
    for key in ("title", "kind", "date", "generated", "raw"):
        value = record.get(key)
        if value:
            parts.append(str(value))
    for item in record.get("meta") or []:
        parts.append(f"{item.get('label', '')}:{item.get('value', '')}")
    for table in record.get("tables") or []:
        parts.append(f"{table.get('section', '')}:{table.get('title', '')}")
    return "\n".join(parts)


def _has_any(text: str, words: tuple[str, ...]) -> bool:
    return any(word in text for word in words)


def _source_scope(record: dict[str, Any], text: str) -> list[dict[str, str]]:
    source_defs = [
        ("watchlist", "同花顺自选/持仓范围", ("自选", "我的股票", "持仓")),
        ("ths_special", "同花顺早盘/复盘/异动/日历", ("早盘必读", "复盘", "异动", "公告日历", "同花顺")),
        ("quote", "行情与量价", ("行情", "收盘", "现价", "涨跌", "成交额", "VWAP", "分钟线")),
        ("f10_notice", "右侧资料/F10/公告研报", ("F10", "右侧资料", "noticeReport", "公告", "研报", "概念")),
        ("community", "社区情绪温度计", ("社区", "股民", "帖子", "情绪温度")),
        ("global_macro", "全球市场/宏观资讯", ("全球市场", "国际财经", "隔夜", "纳斯达克", "标普", "美元", "人民币")),
        ("auction", "集合竞价候选与开盘验证", ("集合竞价", "竞价候选", "09:25")),
        ("paper", "模拟盘订单与持仓", ("模拟盘", "模拟账户", "成交复盘", "可卖", "T+1")),
        ("realtime_signal", "实时信号与盘中事件", ("实时信号", "P0", "P1", "signal", "latest_signals")),
        ("evidence_log", "Mira结构化研究台账", ("Mira研究台账", "evidence_logs", "mira_daily_evidence_log")),
    ]
    present_counts = {
        "paper": len(record.get("paper_trades") or []) + len(record.get("paper_positions") or []),
        "watchlist": len(record.get("rows") or []),
        "auction": len(record.get("market_opportunities") or []) if record.get("kind") == "auction" else 0,
        "evidence_log": 1 if record.get("mira_evidence_log") else 0,
        # Structured global-risk context is stronger evidence than a keyword
        # match in a report paragraph, particularly for after-close reports.
        "global_macro": 1 if record.get("global_risk") else 0,
    }
    result = []
    for source_id, label, markers in source_defs:
        present = _has_any(text, markers) or present_counts.get(source_id, 0) > 0
        result.append({
            "id": source_id,
            "label": label,
            "status": "present" if present else "missing",
        })
    return result


def _evidence_cards(record: dict[str, Any], text: str, source_scope: list[dict[str, str]]) -> list[dict[str, str]]:
    present = {item["id"] for item in source_scope if item["status"] == "present"}
    cards = [
        {
            "claim_area": "market_reaction",
            "claim_type": "market_pricing",
            "label": "价格/成交额/VWAP/关键位",
            "treatment": "use_normally" if "quote" in present else "source_gap",
            "readiness_impact": "supports_working_view" if "quote" in present else "blocks_actionability",
            "note": "只能说明市场定价和承接强弱，不能当作基本面验证。",
        },
        {
            "claim_area": "news_context",
            "claim_type": "fact",
            "label": "同花顺早盘/复盘/公告日历",
            "treatment": "attribute" if "ths_special" in present else "source_gap",
            "readiness_impact": "supports_working_view" if "ths_special" in present else "blocks_actionability",
            "note": "命中题材可提高盯盘优先级，但必须等待价格和量能确认。",
        },
        {
            "claim_area": "sentiment",
            "claim_type": "sentiment",
            "label": "社区讨论热度",
            "treatment": "monitor" if "community" in present else "open_item",
            "readiness_impact": "monitoring_only",
            "note": "只作为温度计和反向风险提示，不作为买卖事实依据。",
        },
        {
            "claim_area": "simulation",
            "claim_type": "derived_calculation",
            "label": "模拟盘成交/盈亏/信号质量",
            "treatment": "sensitize" if "paper" in present or "realtime_signal" in present else "source_gap",
            "readiness_impact": "supports_working_view" if "paper" in present or "realtime_signal" in present else "not_material",
            "note": "用于复盘策略质量；金额、可卖数量和 T+1 规则必须可复算。",
        },
    ]
    if record.get("kind") in {"premarket", "afterclose"}:
        cards.append({
            "claim_area": "macro_context",
            "claim_type": "reported_metric",
            "label": "全球市场/宏观风险偏好",
            "treatment": "attribute" if "global_macro" in present else "source_gap",
            "readiness_impact": "supports_working_view" if "global_macro" in present else "blocks_actionability",
            "note": "影响开盘风险偏好，不直接触发个股交易。",
        })
    return cards


def _blocking_gaps(record: dict[str, Any], source_scope: list[dict[str, str]]) -> list[str]:
    kind = record.get("kind") or "report"
    present = {item["id"] for item in source_scope if item["status"] == "present"}
    gaps: list[str] = []
    if not record.get("generated"):
        gaps.append("缺少生成时间，无法判断报告时效。")
    if not (record.get("rows") or record.get("market_opportunities") or record.get("paper_positions")):
        gaps.append("缺少自选/机会池/模拟持仓的结构化行，结论只能看原文。")
    if "quote" not in present:
        gaps.append("缺少行情或量价字段，不能升级为可执行订盘。")
    if kind in {"premarket", "afterclose"} and "ths_special" not in present:
        gaps.append("缺少同花顺早盘/复盘/异动/日历特色数据。")
    if kind == "premarket" and "global_macro" not in present:
        gaps.append("缺少全球市场/隔夜风险偏好摘要。")
    if kind in {"intraday", "realtime"} and "realtime_signal" not in present:
        gaps.append("缺少实时信号状态或盘中事件链路。")
    if kind == "afterclose" and "paper" not in present:
        gaps.append("缺少模拟盘订单/持仓复盘，无法评价信号落地质量。")
    return gaps[:6]


def _stale_after(kind: str, generated: str | None) -> str:
    if kind == "premarket":
        return "当日 09:25 集合竞价结束后"
    if kind == "auction":
        return "当日 09:30 开盘后"
    if kind in {"intraday", "realtime"}:
        return "行情延迟超过30秒或下一轮15分钟报告前"
    if kind == "afterclose":
        return "下一交易日 09:25 集合竞价结束后"
    if generated:
        try:
            base = datetime.strptime(generated[:19], "%Y-%m-%d %H:%M:%S")
            return (base + timedelta(hours=4)).strftime("%Y-%m-%d %H:%M")
        except ValueError:
            pass
    return "下一次关键行情/资讯刷新后"


def _refresh_triggers(kind: str) -> list[str]:
    common = [
        "同花顺早盘/复盘/异动/公告日历更新或日期不匹配时刷新。",
        "自选股、模拟盘持仓、候选池发生变化时刷新。",
        "行情源延迟、分钟线缺失、价格穿越关键位时刷新。",
    ]
    by_kind = {
        "premarket": ["09:25 集合竞价结束后必须重算开盘情绪和竞价候选。"],
        "auction": ["09:30 开盘后必须用成交价、VWAP 和首批分钟量确认。"],
        "intraday": ["P0/P1 状态变化、模拟成交、板块主线切换时刷新。"],
        "realtime": ["实时引擎健康状态异常或信号成交/取消后刷新。"],
        "afterclose": ["16:30 后同花顺今日复盘数据更新完成后刷新；下一交易日前需重新走盘前链路。"],
    }
    return (by_kind.get(kind, []) + common)[:5]


def _readiness(kind: str, source_scope: list[dict[str, str]], gaps: list[str], record: dict[str, Any]) -> tuple[str, str]:
    present_count = sum(1 for item in source_scope if item["status"] == "present")
    has_rows = bool(record.get("rows") or record.get("market_opportunities") or record.get("paper_positions"))
    has_severe_gap = any("缺少行情" in gap or "缺少生成时间" in gap for gap in gaps)
    if not has_rows or has_severe_gap:
        return "watch_only", "结构化对象或行情时效不足，只能作为观察与复核材料。"
    if kind in {"intraday", "realtime"} and present_count >= 4:
        return "actionable_with_caveats", "盘中量价和信号链路可用于订盘，但买卖仍需实时价格、量能和T+1约束确认。"
    if kind in {"premarket", "afterclose", "auction"} and present_count >= 5 and len(gaps) <= 2:
        return "actionable_with_caveats", "证据范围覆盖行情、同花顺特色数据和结构化对象，可生成订盘框架；开盘/盘中必须二次确认。"
    if present_count >= 3:
        return "working_view", "证据链可支撑工作观点，但存在刷新或来源缺口。"
    return "needs_refresh", "来源覆盖不足，需要刷新关键数据后再使用。"


def build_report_gate(record: dict[str, Any]) -> dict[str, Any]:
    text = _text(record)
    kind = record.get("kind") or "report"
    source_scope = _source_scope(record, text)
    gaps = _blocking_gaps(record, source_scope)
    readiness_level, readiness_basis = _readiness(kind, source_scope, gaps, record)
    evidence_log_status = "structured_daily_log" if record.get("mira_evidence_log") or "Mira研究台账" in text else "heuristic_from_report"
    return {
        "research_object": record.get("title") or "A股每日订盘报告",
        "market_scope": "沪深A股普通股票；可转债、ETF、北交所、港股通不混用股票规则。",
        "time_boundary": record.get("generated") or record.get("date") or "未识别",
        "source_scope": source_scope,
        "evidence_log_status": evidence_log_status,
        "quant_gate_status": "simulation_and_signal_review" if record.get("paper_trades") or record.get("paper_positions") else "not_available",
        "readiness_level": readiness_level,
        "readiness_label": READINESS_LABELS.get(readiness_level, readiness_level),
        "readiness_basis": readiness_basis,
        "blocking_gaps": gaps,
        "stale_after": _stale_after(kind, record.get("generated")),
        "must_refresh_if": _refresh_triggers(kind),
        "evidence_cards": _evidence_cards(record, text, source_scope),
        "action_boundary": "研究/订盘辅助，不是自动交易指令；价格、时间、量能、市场环境共同确认；社区情绪只作温度计。",
    }


def markdown_section_from_text(kind: str, title: str, date: str, generated: str, raw_text: str, row_count: int = 0) -> list[str]:
    record = {
        "kind": kind,
        "title": title,
        "date": date,
        "generated": generated,
        "raw": raw_text,
        "rows": [{}] * max(row_count, 0),
    }
    gate = build_report_gate(record)
    present = [item["label"] for item in gate["source_scope"] if item["status"] == "present"]
    gaps = gate["blocking_gaps"] or ["暂无阻塞缺口；按刷新条件继续复核。"]
    lines = [
        "## 证据与刷新门控",
        f"- 研究可用性：{gate['readiness_label']}。{gate['readiness_basis']}",
        f"- 研究对象：{gate['research_object']}；时间边界：{gate['time_boundary']}。",
        "- 证据范围：" + ("、".join(present) if present else "未识别到有效结构化证据。"),
        "- 阻塞缺口：" + "；".join(gaps),
        f"- 过期时间：{gate['stale_after']}。",
        "- 必须刷新：" + "；".join(gate["must_refresh_if"][:4]),
        f"- 行动边界：{gate['action_boundary']}",
        "- Mira融合口径：事实、市场定价、情绪、推断分离；市场价格只证明承接，不证明基本面；社区讨论不得单独触发买卖。",
    ]
    return lines
