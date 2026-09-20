#!/usr/bin/env python3
"""Read-only V2 lifecycle replay and release-readiness acceptance report."""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from datetime import date
import json
from pathlib import Path
import sqlite3
from typing import Any


DEFAULT_RUNTIME = Path("/Users/tonyyu/Library/Application Support/a-share-trading-watch")
FUNNEL_STATES = (
    "DISCOVERED", "SETUP_FORMING", "NEAR_TRIGGER", "TRIGGERED",
    "ORDER_PENDING", "FILLED", "PARTIAL", "UNFILLED",
    "TRIGGERED_BUT_MISSED", "MANAGING_REENTRY",
)


def _ro(path: Path) -> sqlite3.Connection | None:
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True) if path.exists() else None


def _tables(con: sqlite3.Connection | None) -> set[str]:
    return {row[0] for row in con.execute("SELECT name FROM sqlite_master WHERE type='table'")} if con else set()


def _json(value: Any, default: Any) -> Any:
    try:
        return json.loads(value) if isinstance(value, str) else value
    except (TypeError, json.JSONDecodeError):
        return default


def replay(runtime: Path, start: str, end: str) -> dict[str, Any]:
    data = runtime / "data" / "runtime"
    signal_con = _ro(data / "signal_state.sqlite")
    paper_con = _ro(data / "paper_trading.sqlite")
    track_rows = []
    order_rows = []
    intent_rows = []
    try:
        if "signal_track_events" in _tables(signal_con):
            track_rows = signal_con.execute(
                """SELECT track_id,trading_date,symbol,old_state,new_state,event_type,
                          grade,score,scenario,price,hard_veto_json,gaps_json,payload_json,created_at
                   FROM signal_track_events WHERE trading_date BETWEEN ? AND ? ORDER BY created_at""",
                (start, end),
            ).fetchall()
        if "paper_orders" in _tables(paper_con):
            order_rows = paper_con.execute(
                """SELECT order_id,trading_date,symbol,scenario,side,status,reason,created_at
                   FROM paper_orders WHERE trading_date BETWEEN ? AND ? ORDER BY created_at""",
                (start, end),
            ).fetchall()
        if "paper_intent_marks" in _tables(paper_con):
            intent_rows = paper_con.execute(
                """SELECT order_id,order_status,return_from_signal_pct,mark_at
                   FROM paper_intent_marks WHERE trading_date BETWEEN ? AND ? ORDER BY mark_at""",
                (start, end),
            ).fetchall()
    finally:
        if signal_con:
            signal_con.close()
        if paper_con:
            paper_con.close()

    state_tracks: dict[str, set[str]] = {state: set() for state in FUNNEL_STATES}
    transitions = Counter()
    grades = Counter()
    vetoes = Counter()
    soft_gaps = Counter()
    admission_ablation = Counter()
    for row in track_rows:
        track, _day, _symbol, old, new, _event, grade, _score, _scenario, _price, veto_json, gaps_json, _payload, _at = row
        if new in state_tracks:
            state_tracks[new].add(track)
        transitions[f"{old or 'IDLE'} -> {new}"] += 1
        if new == "TRIGGERED":
            grades[str(grade or "-")] += 1
        hard = _json(veto_json, []) or []
        gaps = _json(gaps_json, []) or []
        codes = {str(item.get("code") or "OTHER") for item in hard}
        for code in codes:
            vetoes[code] += 1
        for item in gaps:
            soft_gaps[str(item.get("category") or "OTHER")] += 1
        if len(codes) == 1:
            admission_ablation[next(iter(codes))] += 1

    status_counts = Counter(str(row[5] or "-") for row in order_rows)
    latest_intent: dict[str, tuple[str, float, str]] = {}
    for order_id, status, return_pct, mark_at in intent_rows:
        if return_pct is not None:
            latest_intent[str(order_id)] = (str(status), float(return_pct), str(mark_at))
    outcomes: dict[str, list[float]] = defaultdict(list)
    for status, value, _at in latest_intent.values():
        outcomes[status].append(value)

    unique_tracks = {row[0] for row in track_rows}
    fills = status_counts["FILLED"] + status_counts["PARTIAL_FILLED"]
    triggered = len(state_tracks["TRIGGERED"])
    readiness = {
        "state_ledger": 15 if track_rows else 0,
        "data_and_evidence": 15 if track_rows and intent_rows else 8 if track_rows else 0,
        "candidate_funnel": 15 if unique_tracks and any(state_tracks[state] for state in ("SETUP_FORMING", "NEAR_TRIGGER")) else 5 if unique_tracks else 0,
        "strategy_activation": 15 if triggered else 5 if unique_tracks else 0,
        "execution_loop": 15 if order_rows else 5 if triggered else 0,
        "position_risk": 10,
        "replay_observability": 10 if track_rows and intent_rows else 5 if track_rows else 0,
        "message_dashboard": 0,
    }
    total = sum(readiness.values())
    grade = "A" if total >= 85 else "B" if total >= 70 else "C" if total >= 55 else "D"
    return {
        "period": {"start": start, "end": end, "runtime": str(runtime)},
        "tracks": len(unique_tracks),
        "events": len(track_rows),
        "funnel": {state: len(values) for state, values in state_tracks.items()},
        "transitions": dict(transitions),
        "trigger_grades": dict(grades),
        "hard_vetoes": dict(vetoes),
        "soft_gaps": dict(soft_gaps),
        "admission_only_ablation": dict(admission_ablation),
        "orders": len(order_rows),
        "order_statuses": dict(status_counts),
        "fills": fills,
        "intent_outcomes": {
            status: {"samples": len(values), "mean_return_pct": round(sum(values) / len(values), 4)}
            for status, values in outcomes.items()
        },
        "readiness": {"dimensions": readiness, "score": total, "grade": grade},
        "limitations": [
            "准入消融只统计去掉单一硬否决可能释放的事件数，不代表可成交或可盈利。",
            "无真实成交和后续价格标记时，不输出胜率、收益或策略有效性结论。",
            "回放只消费当时已落库事件和标记，不使用未来K线重写历史状态。",
        ],
    }


def render_markdown(result: dict[str, Any]) -> str:
    period = result["period"]
    lines = [
        "# V2 生命周期回放与发布验收", "",
        f"- 区间：{period['start']} 至 {period['end']}",
        f"- 标的轨迹：{result['tracks']}；状态事件：{result['events']}；订单：{result['orders']}；成交：{result['fills']}",
        f"- 发布成熟度：**{result['readiness']['score']}/100（{result['readiness']['grade']}）**", "",
        "## 信号漏斗", "", "| 状态 | 唯一轨迹数 |", "|---|---:|",
    ]
    lines.extend(f"| {state} | {count} |" for state, count in result["funnel"].items())
    lines.extend(["", "## 订单去向", "", "| 状态 | 数量 |", "|---|---:|"])
    lines.extend(f"| {state} | {count} |" for state, count in result["order_statuses"].items())
    lines.extend(["", "## 单一硬否决准入消融", "", "| 去掉的否决 | 可能释放事件 |", "|---|---:|"])
    lines.extend(f"| {key} | {value} |" for key, value in sorted(result["admission_only_ablation"].items(), key=lambda item: -item[1]))
    lines.extend(["", "## 意图后续标记", "", "| 原订单状态 | 样本 | 相对信号价平均变化 |", "|---|---:|---:|"])
    lines.extend(
        f"| {status} | {item['samples']} | {item['mean_return_pct']:.2f}% |"
        for status, item in result["intent_outcomes"].items()
    )
    lines.extend(["", "## 结论边界", ""])
    lines.extend(f"- {item}" for item in result["limitations"])
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--runtime", type=Path, default=DEFAULT_RUNTIME)
    parser.add_argument("--start", default="2026-08-14")
    parser.add_argument("--end", default=date.today().isoformat())
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = replay(args.runtime, args.start, args.end)
    text = render_markdown(result)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text, encoding="utf-8")
    else:
        print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
