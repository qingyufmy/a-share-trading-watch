#!/usr/bin/env python3
"""Point-in-time replay for one three-method candidate using persisted bars.

The filename is retained for existing operator commands. Raw V2 patterns are
timing evidence only; this replay always requires a named strategy contract.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from core import intraday_timing_v2, local_market_data, observation_strategy_router


def _float(value: Any, default: float | None = None) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _minute_features(rows: list[dict[str, Any]], current_price: float) -> dict[str, Any]:
    prices = [float(row["p"]) for row in rows]
    volumes = [float(row.get("v") or 0) for row in rows]
    diffs = [abs(prices[index] - prices[index - 1]) for index in range(1, len(prices))]
    prior = volumes[-21:-1] or volumes[:-1]
    median_volume = statistics.median(prior) if prior else 0.0
    return {
        "available": bool(rows),
        "vwap": _float(rows[-1].get("avg_p"), current_price) if rows else None,
        "amount_ratio_1m": volumes[-1] / median_volume if median_volume else 0.0,
        "amount_ratio_5m": sum(volumes[-5:]) / (median_volume * min(5, len(volumes))) if median_volume else 0.0,
        "atr1m": diffs[-1] if diffs else 0.0,
        "atr5m": statistics.mean(diffs[-5:]) if diffs else 0.0,
    }


def _premarket_row(base_dir: Path, code: str, date_text: str) -> dict[str, Any]:
    report = base_dir / "web_dashboard" / "data" / "reports" / f"premarket_{date_text.replace('-', '')}.json"
    if not report.exists():
        return {}
    payload = json.loads(report.read_text(encoding="utf-8"))
    return next((row for row in payload.get("rows") or [] if str(row.get("代码") or "") == code), {})


def _plan_number(plan: dict[str, Any], *keys: str) -> float | None:
    for key in keys:
        value = _float(plan.get(key))
        if value is not None:
            return value
    return None


def _strategy_contract(plan: dict[str, Any], override_key: str | None = None) -> dict[str, Any]:
    key = str(override_key or plan.get("策略键") or "").strip()
    if key not in observation_strategy_router.STRATEGY_ENTRY_SCENARIOS:
        return observation_strategy_router.contract_from_level({})
    meta = observation_strategy_router.STRATEGY_META[key]
    patterns = list(meta.get("allowed_patterns") or [])
    profile = str(plan.get("龙头形态") or "")
    if key == observation_strategy_router.LEADER:
        patterns = (
            ["V2_E5A_LEADER_OPENING_REVERSAL", "V2_E5_LEADER_SECOND_LEG"]
            if profile == "FAILED_LIMIT_REVERSAL"
            else ["V2_E5B_LEADER_OPENING_HOLD", "V2_E5_LEADER_SECOND_LEG"]
        )
    return {
        "key": key,
        "name": meta["name"],
        "style": meta["style"],
        "allowed_patterns": patterns,
        "entry_rule": meta["entry_rule"],
        "exit_rule": meta["exit_rule"],
        "is_observation_strategy": True,
        "version": observation_strategy_router.STRATEGY_CONTRACT_VERSION,
        "daily_qualified": str(plan.get("日线策略资格") or "") == "通过",
        "daily_gate_reason": str(plan.get("日线策略证据") or "回放计划未提供日线证据"),
        "daily_metrics": {"leader_profile": profile or None},
    }


def replay(args: argparse.Namespace) -> dict[str, Any]:
    base_dir = Path(args.base_dir).expanduser()
    trading_day = datetime.strptime(args.date, "%Y-%m-%d")
    cutoff = trading_day.replace(hour=9, minute=29)
    plan = _premarket_row(base_dir, args.code, args.date)
    strategy_contract = _strategy_contract(plan, args.strategy_key)
    raw = [
        row for row in local_market_data.load_bars(base_dir, args.code, "1m", "tencent")
        if str(row.get("bar_end") or "").startswith(args.date)
    ]
    history120, quality120 = local_market_data.history_120m(base_dir, args.code, cutoff)
    history15, quality15 = local_market_data.history_15m(base_dir, args.code, cutoff)
    if not raw:
        raise RuntimeError(f"{args.code} {args.date} has no persisted Tencent 1m bars")

    config = intraday_timing_v2.load_config()
    if args.disable_e7:
        config["flat_base_breakout"] = {**(config.get("flat_base_breakout") or {}), "enabled": False}
    prev_close = float(args.prev_close)
    defense = _plan_number(plan, "防守") or float(args.defense)
    repair = _plan_number(plan, "修复") or float(args.repair)
    pressure = _plan_number(plan, "压力", "压力/减仓") or float(args.pressure)
    sector_confirmed_at = datetime.strptime(f"{args.date} {args.sector_confirmed_at}", "%Y-%m-%d %H:%M")
    timeline = []

    for raw_point in raw:
        now = datetime.strptime(str(raw_point["bar_end"]), "%Y-%m-%d %H:%M:%S")
        if now.time().strftime("%H:%M") < args.start_time or now.time().strftime("%H:%M") > args.end_time:
            continue
        used_raw = [row for row in raw if str(row["bar_end"]) <= str(raw_point["bar_end"])]
        minute_rows = local_market_data._as_minute_rows(used_raw)
        close = float(used_raw[-1]["close"])
        quote = {
            "close": close,
            "open": float(raw[0]["open"]),
            "prev_close": prev_close,
            "high": max(float(row["high"]) for row in used_raw),
            "low": min(float(row["low"]) for row in used_raw),
            "pct": (close / prev_close - 1) * 100,
            "amount_wan": sum(
                ((float(row["high"]) + float(row["low"]) + float(row["close"])) / 3)
                * float(row.get("volume") or 0) * 100
                for row in used_raw
            ) / 10000,
        }
        sector_ready = now >= sector_confirmed_at
        row = {
            "code": args.code,
            "name": plan.get("名称") or args.name,
            "state": "强趋势延续",
            "defense": defense,
            "repair": repair,
            "pressure": pressure,
            "strategy_contract": strategy_contract,
            "quote": quote,
            "rt_features": _minute_features(minute_rows, close),
            "sector_momentum": {
                "emotion_ok": sector_ready,
                "board_name": args.sector,
                "board_pct": float(args.board_pct) if sector_ready else None,
            },
            "sector_rotation": {
                "available": sector_ready,
                "sustained": sector_ready,
                "leader_healthy": sector_ready,
            },
        }
        market_data = {
            "entry_ready": bool(quality120.get("entry_ready") and quality15.get("entry_ready")),
            "blockers": list(quality120.get("blockers") or []) + list(quality15.get("blockers") or []),
            "market_gate": {
                "allowed": sector_ready,
                "stage": "THREE_METHOD_SECTOR_CONFIRMED" if sector_ready else "THREE_METHOD_SECTOR_BLOCKED",
                "reason": f"{args.sector}{'已' if sector_ready else '未'}完成当日板块共振假设",
            },
        }
        decision = intraday_timing_v2.evaluate(
            row,
            minute_rows,
            now=now,
            history_120m=history120,
            history_15m=history15,
            config=config,
            market_data=market_data,
        )
        if (
            decision.get("entry_allowed")
            or decision.get("candidate_entry_pattern")
            or now.strftime("%H:%M") in {"10:30", "11:30", "13:15", "13:30", "14:00", "14:15"}
        ):
            timeline.append({
                "time": now.strftime("%H:%M"),
                "price": round(close, 3),
                "pct": round(float(quote["pct"]), 3),
                "closed_15m": decision["data_quality"]["intraday_closed_15m_bars"],
                "setup": decision.get("setup_15m"),
                "entry_allowed": bool(decision.get("entry_allowed")),
                "pattern": decision.get("candidate_entry_pattern"),
                "execution_band": [
                    decision["room_risk"].get("execution_band_low"),
                    decision["room_risk"].get("execution_band_high"),
                ],
                "entry_invalidation": decision["levels"].get("entry_invalidation"),
                "structural_invalidation": decision["levels"].get("structural_invalidation"),
                "resistance": decision["levels"].get("nearest_resistance"),
                "room_atr": decision["room_risk"].get("room_atr"),
                "reward_risk": decision["room_risk"].get("reward_risk"),
                "market_gate_override": bool(decision.get("market_gate_override")),
                "blockers": list(decision.get("blockers") or [])[:5],
            })

    entries = [item for item in timeline if item["entry_allowed"]]
    return {
        "replay_version": "three_method_point_in_time_replay_v1",
        "symbol": args.code,
        "name": plan.get("名称") or args.name,
        "trading_date": args.date,
        "minute_rows": len(raw),
        "timestamp_convention": local_market_data._minute_timestamp_convention(raw),
        "history_quality": {"120m": quality120, "15m": quality15},
        "premarket_plan": plan,
        "strategy_contract": strategy_contract,
        "assumption": {
            "sector": args.sector,
            "sector_confirmed_at": args.sector_confirmed_at,
            "board_pct": float(args.board_pct),
            "note": "板块确认时点为显式回放假设；价格、成交量及K线仅使用该时点前本地持久化数据。",
        },
        "legacy_timing_entry_disabled": True,
        "first_entry": entries[0] if entries else None,
        "entry_count": len(entries),
        "timeline": timeline,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-dir", default="~/Library/Application Support/a-share-trading-watch")
    parser.add_argument("--code", default="600227")
    parser.add_argument("--name", default="赤天化")
    parser.add_argument("--date", default="2026-08-28")
    parser.add_argument("--prev-close", type=float, default=4.16)
    parser.add_argument("--defense", type=float, default=4.01)
    parser.add_argument("--repair", type=float, default=4.29)
    parser.add_argument("--pressure", type=float, default=4.29)
    parser.add_argument("--sector", default="氮肥")
    parser.add_argument("--sector-confirmed-at", default="13:15")
    parser.add_argument("--board-pct", type=float, default=5.61)
    parser.add_argument("--strategy-key", choices=sorted(observation_strategy_router.STRATEGY_ENTRY_SCENARIOS))
    parser.add_argument("--start-time", default="09:35")
    parser.add_argument("--end-time", default="14:45")
    parser.add_argument("--disable-e7", action="store_true")
    parser.add_argument("--output")
    args = parser.parse_args()
    result = replay(args)
    rendered = json.dumps(result, ensure_ascii=False, indent=2)
    if args.output:
        output = Path(args.output)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(rendered + "\n", encoding="utf-8")
    print(rendered)


if __name__ == "__main__":
    main()
