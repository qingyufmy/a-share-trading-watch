"""Structured daily evidence log for the A-share trading watch system.

The goal is not to reproduce the full Mira case database. This module writes a
small, stable local ledger that converts daily trading signals and simulated
orders into reviewable claims. The ledger is append-friendly and can later feed
hit-rate, false-positive and rule-iteration analysis.
"""

from __future__ import annotations

import csv
import json
import re
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any


CANONICAL_COLUMNS = [
    "source_id",
    "claim_area",
    "claim_type",
    "claim_text",
    "source_speaker",
    "verification_status",
    "authority_level",
    "source_date",
    "as_of_date",
    "url_or_path",
    "used_by_agent",
    "used_by_skill",
    "confidence",
    "upstream_sources",
    "notes",
    "evidence_category",
    "freshness_status",
    "conflict_status",
    "treatment",
    "readiness_impact",
    "source_language",
    "translation_basis",
]


def _compact_date(date_text: str) -> str:
    return re.sub(r"\D", "", date_text or "")[:8]


def _safe(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (dict, list, tuple)):
        return json.dumps(value, ensure_ascii=False, default=str, separators=(",", ":"))
    return str(value).replace("\n", " ").strip()


def _confidence_from_review(text: str) -> str:
    if any(key in text for key in ("数据不足", "弱验证", "需复核", "待复盘")):
        return "low"
    if any(key in text for key in ("有效", "合理", "失败", "偏早", "低效")):
        return "medium"
    return "medium"


def _treatment_from_review(text: str) -> str:
    if any(key in text for key in ("不合理", "失败", "失效", "需降噪", "过早", "偏早")):
        return "haircut"
    if any(key in text for key in ("数据不足", "弱验证", "待复盘")):
        return "open_item"
    if any(key in text for key in ("有效", "合理")):
        return "sensitize"
    return "monitor"


def _readiness_impact(text: str) -> str:
    if any(key in text for key in ("不合理", "失败", "失效", "需降噪", "过早")):
        return "blocks_actionability"
    if any(key in text for key in ("数据不足", "弱验证", "待复盘")):
        return "monitoring_only"
    return "supports_working_view"


def _row(
    *,
    source_id: str,
    claim_area: str,
    claim_text: str,
    source_date: str,
    as_of_date: str,
    url_or_path: str,
    notes: str,
    confidence: str,
    treatment: str,
    readiness_impact: str,
    claim_type: str = "derived_calculation",
    verification_status: str = "calculated",
    evidence_category: str = "derived_review",
) -> dict[str, str]:
    return {
        "source_id": source_id,
        "claim_area": claim_area,
        "claim_type": claim_type,
        "claim_text": claim_text,
        "source_speaker": "local_trading_watch",
        "verification_status": verification_status,
        "authority_level": "L6",
        "source_date": source_date,
        "as_of_date": as_of_date,
        "url_or_path": url_or_path,
        "used_by_agent": "a-share-trading-watch",
        "used_by_skill": "a-share-trading-plan",
        "confidence": confidence,
        "upstream_sources": "local_runtime,quote_cache,minute_bars,paper_trading,ths_context",
        "notes": notes,
        "evidence_category": evidence_category,
        "freshness_status": "current",
        "conflict_status": "not_checked",
        "treatment": treatment,
        "readiness_impact": readiness_impact,
        "source_language": "zh-CN",
        "translation_basis": "not_translated",
    }


def signal_rows(trading_date: str, next_trading_date: str, signal_review: dict[str, Any], signal_review_path: str) -> list[dict[str, str]]:
    rows = []
    for idx, item in enumerate(signal_review.get("events") or [], 1):
        review_text = "；".join(
            str(x)
            for x in (
                item.get("validity"),
                item.get("rationality"),
                item.get("review"),
                item.get("iteration_hint"),
            )
            if x
        )
        claim = (
            f"实时信号 {item.get('symbol')} {item.get('name')} {item.get('scenario')} "
            f"在 {item.get('created_at')} 触发，复盘结论为 {item.get('validity')}/{item.get('rationality')}。"
        )
        notes = {
            "priority": item.get("priority"),
            "action_type": item.get("action_type"),
            "price": item.get("price"),
            "trigger_price": item.get("trigger_price"),
            "invalid_price": item.get("invalid_price"),
            "amount_ratio_1m": item.get("amount_ratio_1m"),
            "iteration_hint": item.get("iteration_hint"),
        }
        rows.append(
            _row(
                source_id=f"signal_{trading_date}_{idx:03d}_{item.get('symbol', '')}",
                claim_area="realtime_signal_quality",
                claim_text=claim,
                source_date=trading_date,
                as_of_date=next_trading_date,
                url_or_path=signal_review_path,
                notes=_safe(notes),
                confidence=_confidence_from_review(review_text),
                treatment=_treatment_from_review(review_text),
                readiness_impact=_readiness_impact(review_text),
            )
        )
    return rows


def paper_order_rows(trading_date: str, next_trading_date: str, paper_review: dict[str, Any]) -> list[dict[str, str]]:
    rows = []
    orders = paper_review.get("all_orders") or paper_review.get("orders") or []
    for idx, item in enumerate(orders, 1):
        status = item.get("status") or ("FILLED" if item.get("fill_price") else "UNKNOWN")
        quality = item.get("trade_quality") or item.get("reason") or "未复盘"
        review = item.get("review") or item.get("reason") or ""
        discipline = item.get("discipline_check") or {}
        rationale = item.get("strategy_rationale") or {}
        review_text = f"{quality}；{review}"
        claim = (
            f"模拟订单 {item.get('symbol')} {item.get('name')} {item.get('side')} "
            f"{status}，质量结论为 {quality}。"
        )
        notes = {
            "created_at": item.get("created_at"),
            "scenario": item.get("scenario") or (item.get("signal") or {}).get("scenario"),
            "qty": item.get("qty"),
            "fill_price": item.get("fill_price"),
            "mfe15": item.get("mfe15"),
            "mae15": item.get("mae15"),
            "reason": item.get("reason"),
            "discipline_passed": discipline.get("passed"),
            "discipline_summary": discipline.get("summary"),
            "discipline_failed_checks": discipline.get("failed_checks"),
            "strategy_action_basis": rationale.get("action_basis"),
        }
        rows.append(
            _row(
                source_id=f"paper_{trading_date}_{idx:03d}_{item.get('symbol', '')}",
                claim_area="paper_trade_execution_quality",
                claim_text=claim,
                source_date=trading_date,
                as_of_date=next_trading_date,
                url_or_path="data/runtime/paper_trading.sqlite",
                notes=_safe(notes),
                confidence=_confidence_from_review(review_text),
                treatment=_treatment_from_review(review_text),
                readiness_impact=_readiness_impact(review_text),
            )
        )
    return rows


def auction_rows(trading_date: str, next_trading_date: str, auction_review: dict[str, Any]) -> list[dict[str, str]]:
    rows = []
    source_path = (auction_review.get("source") or {}).get("path") or f"data/auction_candidates/auction_candidates_{_compact_date(trading_date)}.json"
    for idx, item in enumerate(auction_review.get("candidates") or [], 1):
        result = item.get("result") or "未复盘"
        review = item.get("review") or ""
        review_text = f"{result}；{review}"
        claim = (
            f"集合竞价候选 {item.get('code')} {item.get('name')} 复盘结果为 {result}，"
            f"触发价 {item.get('trigger_price')}，失效价 {item.get('invalid_price')}。"
        )
        rows.append(
            _row(
                source_id=f"auction_{trading_date}_{idx:03d}_{item.get('code', '')}",
                claim_area="auction_candidate_quality",
                claim_text=claim,
                source_date=trading_date,
                as_of_date=next_trading_date,
                url_or_path=source_path,
                notes=_safe({
                    "status": item.get("status"),
                    "triggered": item.get("triggered"),
                    "entry_price": item.get("entry_price"),
                    "trigger_time": item.get("trigger_time"),
                    "review": review,
                }),
                confidence=_confidence_from_review(review_text),
                treatment=_treatment_from_review(review_text),
                readiness_impact=_readiness_impact(review_text),
            )
        )
    return rows


def paper_position_rows(trading_date: str, next_trading_date: str, paper_snapshot: dict[str, Any]) -> list[dict[str, str]]:
    rows = []
    for idx, item in enumerate(paper_snapshot.get("positions") or [], 1):
        claim = (
            f"模拟持仓 {item.get('symbol') or item.get('代码')} {item.get('name') or item.get('名称')} "
            f"收盘盯市收益率 {item.get('unrealized_pnl_pct') or item.get('收益率')}。"
        )
        rows.append(
            _row(
                source_id=f"position_{trading_date}_{idx:03d}_{item.get('symbol') or item.get('代码') or ''}",
                claim_area="paper_position_mark_to_market",
                claim_text=claim,
                source_date=trading_date,
                as_of_date=next_trading_date,
                url_or_path="data/runtime/paper_trading.sqlite",
                notes=_safe(item),
                confidence="medium",
                treatment="sensitize",
                readiness_impact="supports_working_view",
            )
        )
    return rows


def _event_payloads(
    trading_date: str,
    next_trading_date: str,
    signal_review: dict[str, Any],
    paper_review: dict[str, Any],
    auction_review: dict[str, Any],
    paper_snapshot: dict[str, Any],
) -> list[dict[str, Any]]:
    payloads = []
    for item in signal_review.get("events") or []:
        payloads.append({"trading_date": trading_date, "next_trading_date": next_trading_date, "event_family": "signal", **item})
    for item in paper_review.get("orders") or paper_review.get("all_orders") or []:
        payloads.append({"trading_date": trading_date, "next_trading_date": next_trading_date, "event_family": "paper_order", **item})
    for item in auction_review.get("candidates") or []:
        payloads.append({"trading_date": trading_date, "next_trading_date": next_trading_date, "event_family": "auction_candidate", **item})
    for item in paper_snapshot.get("positions") or []:
        payloads.append({"trading_date": trading_date, "next_trading_date": next_trading_date, "event_family": "paper_position", **item})
    return payloads


def _quality_summary(rows: list[dict[str, str]], signal_review: dict[str, Any], paper_review: dict[str, Any], auction_review: dict[str, Any]) -> dict[str, Any]:
    by_area = Counter(row["claim_area"] for row in rows)
    by_treatment = Counter(row["treatment"] for row in rows)
    by_impact = Counter(row["readiness_impact"] for row in rows)
    signal_metrics = signal_review.get("metrics") or {}
    orders = paper_review.get("orders") or []
    all_orders = paper_review.get("all_orders") or []
    discipline_orders = all_orders or orders
    trade_orders = [x for x in discipline_orders if str(x.get("side") or "").upper() in {"BUY", "SELL"}]
    filled_orders = [x for x in trade_orders if x.get("status") in {"FILLED", "PARTIAL_FILLED"}]
    rejected_orders = [x for x in trade_orders if x.get("status") == "REJECTED"]
    cancel_orders = [x for x in discipline_orders if str(x.get("side") or "").upper() == "CANCEL"]
    discipline_passed = [x for x in trade_orders if (x.get("discipline_check") or {}).get("passed") is True]
    discipline_failed = [x for x in trade_orders if (x.get("discipline_check") or {}).get("passed") is False]
    discipline_missing = [x for x in trade_orders if (x.get("discipline_check") or {}).get("passed") is None]
    auction_candidates = auction_review.get("candidates") or []
    open_items = [
        row["claim_text"]
        for row in rows
        if row["treatment"] in {"open_item", "haircut"} or row["readiness_impact"] == "blocks_actionability"
    ][:10]
    iteration_items = []
    for event in signal_review.get("events") or []:
        hint = event.get("iteration_hint")
        if hint and hint not in iteration_items:
            iteration_items.append(hint)
    for order in orders:
        review = order.get("review")
        if review and any(key in review for key in ("失败", "偏早", "低效", "数据不足")) and review not in iteration_items:
            iteration_items.append(review)
    for item in auction_candidates:
        review = item.get("review")
        if review and any(key in review for key in ("失效", "低效", "数据不足")) and review not in iteration_items:
            iteration_items.append(review)
    return {
        "total_claims": len(rows),
        "by_area": dict(by_area),
        "by_treatment": dict(by_treatment),
        "by_readiness_impact": dict(by_impact),
        "signal_metrics": signal_metrics,
        "paper_order_count": len(orders),
        "paper_order_all_count": len(discipline_orders),
        "paper_trade_attempt_count": len(trade_orders),
        "paper_fill_count": len(filled_orders),
        "paper_rejected_count": len(rejected_orders),
        "paper_cancel_count": len(cancel_orders),
        "discipline_passed": len(discipline_passed),
        "discipline_failed": len(discipline_failed),
        "discipline_missing": len(discipline_missing),
        "auction_candidate_count": len(auction_candidates),
        "open_items": open_items,
        "iteration_items": iteration_items[:10],
    }


def write_daily_evidence_log(
    *,
    base_dir: Path,
    trading_date: str,
    next_trading_date: str,
    generated_at: str,
    signal_review: dict[str, Any],
    paper_review: dict[str, Any],
    auction_review: dict[str, Any],
    paper_snapshot: dict[str, Any],
    signal_review_path: str,
) -> dict[str, Any]:
    compact = _compact_date(trading_date)
    out_dir = Path(base_dir) / "data" / "research" / "evidence_logs"
    out_dir.mkdir(parents=True, exist_ok=True)
    csv_path = out_dir / f"daily_{compact}.csv"
    jsonl_path = out_dir / f"daily_{compact}.events.jsonl"
    summary_path = out_dir / f"daily_{compact}.summary.json"
    history_path = out_dir / "evidence_history.jsonl"

    rows: list[dict[str, str]] = []
    rows.extend(signal_rows(trading_date, next_trading_date, signal_review, signal_review_path))
    rows.extend(paper_order_rows(trading_date, next_trading_date, paper_review))
    rows.extend(auction_rows(trading_date, next_trading_date, auction_review))
    rows.extend(paper_position_rows(trading_date, next_trading_date, paper_snapshot))

    with csv_path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=CANONICAL_COLUMNS)
        writer.writeheader()
        for row in rows:
            writer.writerow({col: _safe(row.get(col, "")) for col in CANONICAL_COLUMNS})

    payloads = _event_payloads(trading_date, next_trading_date, signal_review, paper_review, auction_review, paper_snapshot)
    with jsonl_path.open("w", encoding="utf-8") as f:
        for payload in payloads:
            f.write(json.dumps(payload, ensure_ascii=False, default=str) + "\n")

    quality = _quality_summary(rows, signal_review, paper_review, auction_review)
    summary = {
        "trading_date": trading_date,
        "next_trading_date": next_trading_date,
        "generated_at": generated_at,
        "schema": "mira_daily_evidence_log_v1",
        "csv_path": str(csv_path),
        "jsonl_path": str(jsonl_path),
        "summary_path": str(summary_path),
        **quality,
    }
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    with history_path.open("a", encoding="utf-8") as f:
        f.write(json.dumps({
            "trading_date": trading_date,
            "next_trading_date": next_trading_date,
            "generated_at": generated_at,
            "total_claims": quality["total_claims"],
            "by_treatment": quality["by_treatment"],
            "by_readiness_impact": quality["by_readiness_impact"],
            "signal_metrics": quality["signal_metrics"],
            "paper_order_count": quality["paper_order_count"],
            "paper_order_all_count": quality["paper_order_all_count"],
            "paper_trade_attempt_count": quality["paper_trade_attempt_count"],
            "paper_fill_count": quality["paper_fill_count"],
            "paper_rejected_count": quality["paper_rejected_count"],
            "paper_cancel_count": quality["paper_cancel_count"],
            "discipline_passed": quality["discipline_passed"],
            "discipline_failed": quality["discipline_failed"],
            "discipline_missing": quality["discipline_missing"],
            "auction_candidate_count": quality["auction_candidate_count"],
            "summary_path": str(summary_path),
        }, ensure_ascii=False, default=str) + "\n")
    return summary


def markdown_section(summary: dict[str, Any] | None) -> list[str]:
    if not summary:
        return [
            "## 九、Mira研究台账",
            "- 结构化 evidence log 未生成；本次只能阅读报告正文，不能纳入后续自动统计。",
        ]
    by_area = summary.get("by_area") or {}
    by_treatment = summary.get("by_treatment") or {}
    metrics = summary.get("signal_metrics") or {}
    lines = [
        "## 九、Mira研究台账",
        f"- 台账状态：已生成 `{summary.get('schema', 'mira_daily_evidence_log_v1')}`，共 {summary.get('total_claims', 0)} 条 claim。",
        f"- 覆盖范围：实时信号 {by_area.get('realtime_signal_quality', 0)} 条；模拟订单 {by_area.get('paper_trade_execution_quality', 0)} 条；竞价候选 {by_area.get('auction_candidate_quality', 0)} 条；模拟持仓 {by_area.get('paper_position_mark_to_market', 0)} 条。",
        f"- 处理姿态：正常/敏感使用 {by_treatment.get('sensitize', 0)} 条；降权 {by_treatment.get('haircut', 0)} 条；开放项 {by_treatment.get('open_item', 0)} 条；监控 {by_treatment.get('monitor', 0)} 条。",
        f"- 信号指标：P0有效 {metrics.get('p0_effective', 0)}/{metrics.get('p0_total', 0)}；加仓合理 {metrics.get('add_reasonable', 0)}/{metrics.get('add_total', 0)}；风控需降噪 {metrics.get('reduce_noisy', 0)} 条。",
        f"- 执行指标：模拟交易成交 {summary.get('paper_fill_count', 0)}/{summary.get('paper_trade_attempt_count', 0)}；策略拦截 {summary.get('paper_rejected_count', 0)}；取消计划 {summary.get('paper_cancel_count', 0)}。",
        f"- 文件：`{summary.get('csv_path')}`；事件明细：`{summary.get('jsonl_path')}`；摘要：`{summary.get('summary_path')}`。",
    ]
    if summary.get("open_items"):
        lines.append("- 开放问题：" + "；".join(summary["open_items"][:3]))
    if summary.get("iteration_items"):
        lines.append("- 迭代动作：" + "；".join(summary["iteration_items"][:3]))
    lines.append("- 使用边界：这份台账用于复盘信号质量和规则迭代，不作为单独买卖依据。")
    return lines
