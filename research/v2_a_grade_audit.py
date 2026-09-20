#!/usr/bin/env python3
"""Read-only audit of the V2 signal, execution, and market-data funnel."""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from datetime import date
import json
from pathlib import Path
import sqlite3
from typing import Any


DEFAULT_RUNTIME = Path("/Users/tonyyu/Library/Application Support/a-share-trading-watch")
CASE_SYMBOLS = {
    "002821": "凯莱英",
    "600227": "赤天化",
    "002407": "多氟多",
    "603650": "彤程新材",
}


def _connect_readonly(path: Path) -> sqlite3.Connection | None:
    if not path.exists():
        return None
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True)


def _tables(con: sqlite3.Connection | None) -> set[str]:
    if con is None:
        return set()
    return {row[0] for row in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def _json(value: Any, default: Any) -> Any:
    try:
        return json.loads(value) if isinstance(value, str) else value
    except (TypeError, json.JSONDecodeError):
        return default


def _range_clause(start: str, end: str) -> tuple[str, tuple[str, str]]:
    return "trading_date BETWEEN ? AND ?", (start, end)


def audit_signal_db(path: Path, start: str, end: str) -> dict[str, Any]:
    con = _connect_readonly(path)
    if con is None:
        return {"available": False, "path": str(path)}
    try:
        tables = _tables(con)
        where, args = _range_clause(start, end)
        snapshot_rows = []
        event_rows = []
        if "signals" in tables:
            snapshot_rows = con.execute(
                f"""SELECT trading_date, symbol, scenario, external_status, internal_state,
                           current_price, reason_json, last_update_ts
                    FROM signals WHERE {where} ORDER BY last_update_ts""",
                args,
            ).fetchall()
        if "signal_events" in tables:
            event_rows = con.execute(
                f"""SELECT trading_date, symbol, scenario, event_type, from_state, to_state,
                           price, payload_json, created_at
                    FROM signal_events WHERE {where} ORDER BY created_at""",
                args,
            ).fetchall()
    finally:
        con.close()

    states = Counter(row[4] or "UNKNOWN" for row in snapshot_rows)
    statuses = Counter(row[3] or "UNKNOWN" for row in snapshot_rows)
    scenarios = Counter(row[2] or "UNKNOWN" for row in snapshot_rows)
    transitions = Counter(f"{row[4] or 'IDLE'} -> {row[5] or 'UNKNOWN'}" for row in event_rows)
    blockers: Counter[str] = Counter()
    for row in snapshot_rows:
        for reason in _json(row[6], []) or []:
            blockers[str(reason).strip()] += 1
    for row in event_rows:
        payload = _json(row[7], {}) or {}
        timing = payload.get("timing_v2") or {}
        for reason in timing.get("blockers") or []:
            blockers[str(reason).strip()] += 1

    cases: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in event_rows:
        if row[1] in CASE_SYMBOLS:
            cases[row[1]].append({
                "at": row[8], "scenario": row[2], "event": row[3],
                "from": row[4], "to": row[5], "price": row[6],
            })
    for row in snapshot_rows:
        if row[1] in CASE_SYMBOLS and not cases[row[1]]:
            cases[row[1]].append({
                "at": row[7], "scenario": row[2], "event": "snapshot_only",
                "from": None, "to": row[4], "price": row[5],
            })
    return {
        "available": True,
        "path": str(path),
        "snapshot_count": len(snapshot_rows),
        "event_count": len(event_rows),
        "states": dict(states),
        "statuses": dict(statuses),
        "scenarios": dict(scenarios),
        "transitions": dict(transitions),
        "blockers": blockers.most_common(20),
        "cases": dict(cases),
    }


def audit_paper_db(path: Path, start: str, end: str) -> dict[str, Any]:
    con = _connect_readonly(path)
    if con is None:
        return {"available": False, "path": str(path)}
    try:
        tables = _tables(con)
        where, args = _range_clause(start, end)
        orders = []
        positions = []
        if "paper_orders" in tables:
            orders = con.execute(
                f"""SELECT trading_date, symbol, scenario, side, status, reason, created_at
                    FROM paper_orders WHERE {where} ORDER BY created_at""",
                args,
            ).fetchall()
        if "paper_positions" in tables:
            positions = con.execute(
                "SELECT symbol, quantity, sellable, avg_cost, source, updated_at FROM paper_positions"
            ).fetchall()
    finally:
        con.close()
    statuses = Counter(row[4] or "UNKNOWN" for row in orders)
    reasons = Counter(row[5] or "UNKNOWN" for row in orders if row[4] not in {"FILLED", "PARTIAL_FILLED"})
    return {
        "available": True,
        "path": str(path),
        "orders": len(orders),
        "fills": sum(statuses[key] for key in ("FILLED", "PARTIAL_FILLED")),
        "statuses": dict(statuses),
        "nonfill_reasons": reasons.most_common(15),
        "open_positions": len([row for row in positions if int(row[1] or 0) > 0]),
    }


def audit_market_db(path: Path, start: str, end: str) -> dict[str, Any]:
    con = _connect_readonly(path)
    if con is None:
        return {"available": False, "path": str(path)}
    try:
        if "market_bars" not in _tables(con):
            return {"available": True, "path": str(path), "bars": 0, "symbols": 0}
        rows = con.execute(
            """SELECT timeframe, source, COUNT(*), COUNT(DISTINCT symbol),
                      MIN(bar_end), MAX(bar_end)
               FROM market_bars WHERE substr(bar_end,1,10) BETWEEN ? AND ?
               GROUP BY timeframe, source ORDER BY timeframe, source""",
            (start, end),
        ).fetchall()
    finally:
        con.close()
    return {
        "available": True,
        "path": str(path),
        "segments": [
            {"timeframe": row[0], "source": row[1], "bars": row[2], "symbols": row[3], "first": row[4], "last": row[5]}
            for row in rows
        ],
    }


def build_audit(runtime: Path, start: str, end: str) -> dict[str, Any]:
    data = runtime / "data" / "runtime"
    return {
        "generated_for": {"start": start, "end": end, "runtime": str(runtime)},
        "signals": audit_signal_db(data / "signal_state.sqlite", start, end),
        "paper": audit_paper_db(data / "paper_trading.sqlite", start, end),
        "market": audit_market_db(data / "market_data.sqlite", start, end),
    }


def _table(counter: dict[str, Any], left: str, right: str) -> list[str]:
    lines = [f"| {left} | {right} |", "|---|---:|"]
    lines.extend(f"| {key} | {value} |" for key, value in sorted(counter.items(), key=lambda item: (-item[1], item[0])))
    return lines


def render_markdown(audit: dict[str, Any]) -> str:
    period = audit["generated_for"]
    signal = audit["signals"]
    paper = audit["paper"]
    market = audit["market"]
    lines = [
        "# V2 A级迭代基线审计",
        "",
        f"- 审计区间：{period['start']} 至 {period['end']}",
        f"- 数据目录：`{period['runtime']}`",
        "- 生成方式：只读 SQLite URI；不写运行数据库，不触发飞书或交易。",
        "",
        "## 信号漏斗基线",
        "",
        f"- 活动快照：{signal.get('snapshot_count', 0)}",
        f"- 有效状态事件：{signal.get('event_count', 0)}",
    ]
    lines.extend(_table(signal.get("states") or {}, "内部状态", "数量"))
    lines.extend(["", "### 状态迁移", ""])
    lines.extend(_table(signal.get("transitions") or {}, "迁移", "数量"))
    lines.extend(["", "### 主要阻断", "", "| 排名 | 原因 | 次数 |", "|---:|---|---:|"])
    lines.extend(f"| {index} | {reason} | {count} |" for index, (reason, count) in enumerate(signal.get("blockers") or [], 1))
    lines.extend([
        "",
        "## 模拟盘基线",
        "",
        f"- 订单：{paper.get('orders', 0)}",
        f"- 成交：{paper.get('fills', 0)}",
        f"- 当前开放持仓：{paper.get('open_positions', 0)}",
    ])
    lines.extend(_table(paper.get("statuses") or {}, "订单状态", "数量"))
    lines.extend(["", "## 市场数据分段", "", "| 周期 | 数据源 | K线 | 股票 | 首条 | 末条 |", "|---|---|---:|---:|---|---|"])
    for item in market.get("segments") or []:
        lines.append(f"| {item['timeframe']} | {item['source']} | {item['bars']} | {item['symbols']} | {item['first']} | {item['last']} |")
    lines.extend(["", "## 四个重点案例原始路径", ""])
    for symbol, name in CASE_SYMBOLS.items():
        lines.append(f"### {name}（{symbol}）")
        rows = (signal.get("cases") or {}).get(symbol) or []
        if not rows:
            lines.append("- 区间内没有状态事件或快照。")
        else:
            for item in rows[:40]:
                lines.append(
                    f"- {item['at']}｜{item['scenario']}｜{item['event']}｜"
                    f"{item['from'] or '-'} -> {item['to'] or '-'}｜价格 {item['price']}"
                )
        lines.append("")
    only_idle_wait = bool(signal.get("transitions")) and all(key.startswith("IDLE -> WAIT") for key in signal.get("transitions") or {})
    score = {
        "数据正确性与时效": 7,
        "候选发现与覆盖": 6,
        "逐股状态跟踪": 2 if only_idle_wait else 6,
        "策略识别质量": 5,
        "执行与成交能力": 2 if paper.get("orders", 0) == 0 else 6,
        "持仓和风险管理": 5,
        "复盘和证据能力": 4,
        "工作台与消息体验": 3,
    }
    total = sum(score.values())
    lines.extend([
        "## 当前评级",
        "",
        f"- 基线总分：**{total}/100（{'D' if total < 55 else 'C'}）**",
        "- 本分数是发布成熟度评级，不是收益评级；后续阶段必须用测试和回放证据替换人工保守分。",
        "",
        "| 维度 | 得分 |",
        "|---|---:|",
    ])
    lines.extend(f"| {name} | {value} |" for name, value in score.items())
    lines.extend([
        "",
        "## 基线结论",
        "",
        "1. 按 scenario 建主键会把同一股票的连续过程拆散，无法证明 WAIT 到 TRIGGER 的真实转移。",
        "2. 数据故障、结构等待和普通观察尚未在主状态层严格分开。",
        "3. 零订单不能仅解释为策略谨慎，因为上游状态漏斗没有保存足够的反事实证据。",
        "4. 后续先修台账和数据不变量，再评价阈值及收益表现。",
        "",
    ])
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--runtime", type=Path, default=DEFAULT_RUNTIME)
    parser.add_argument("--start", default="2026-08-14")
    parser.add_argument("--end", default=date.today().isoformat())
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    audit = build_audit(args.runtime, args.start, args.end)
    text = render_markdown(audit)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text, encoding="utf-8")
    else:
        print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
