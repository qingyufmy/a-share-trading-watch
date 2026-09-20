"""Reproducible higher-timeframe evidence for A-share candidate plans.

The post-close plan ranks names.  It deliberately does *not* manufacture an
intraday buy instruction: the live engine must still confirm the 60-minute and
minute-level structure before a paper order may be considered.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from statistics import mean
from typing import Any


VERSION = "emotion_chips_time_three_cycles_v2_monthly_daily_60m"


def _number(value: Any, default: float | None = None) -> float | None:
    try:
        value = float(value)
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


def _ma(values: list[float], period: int) -> float | None:
    return mean(values[-period:]) if len(values) >= period else None


def _macd(closes: list[float]) -> tuple[float | None, float | None, float | None]:
    if not closes:
        return None, None, None
    ema12 = ema26 = closes[0]
    dea = 0.0
    for close in closes:
        ema12 = ema12 * 11 / 13 + close * 2 / 13
        ema26 = ema26 * 25 / 27 + close * 2 / 27
        dif = ema12 - ema26
        dea = dea * 8 / 10 + dif * 2 / 10
    return dif, dea, (dif - dea) * 2


def kdj(rows: list[dict[str, Any]], period: int = 9, k_smooth: int = 3, d_smooth: int = 3) -> dict[str, float | None]:
    """Calculate KDJ from OHLC rows without relying on display-side values."""
    if len(rows) < period:
        return {"k": None, "d": None, "j": None}
    k_value = d_value = 50.0
    for index in range(period - 1, len(rows)):
        window = rows[index - period + 1:index + 1]
        high = max((_number(row.get("high"), 0.0) or 0.0) for row in window)
        low = min((_number(row.get("low"), 0.0) or 0.0) for row in window)
        close = _number(rows[index].get("close"), 0.0) or 0.0
        rsv = 50.0 if high <= low else (close - low) / (high - low) * 100
        k_value = ((k_smooth - 1) * k_value + rsv) / k_smooth
        d_value = ((d_smooth - 1) * d_value + k_value) / d_smooth
    return {"k": round(k_value, 1), "d": round(d_value, 1), "j": round(3 * k_value - 2 * d_value, 1)}


def weekly_bars(daily: list[dict[str, Any]]) -> list[dict[str, Any]]:
    groups: dict[tuple[int, int], list[dict[str, Any]]] = defaultdict(list)
    for row in daily:
        try:
            day = datetime.strptime(str(row.get("date") or "")[:10], "%Y-%m-%d").date()
        except ValueError:
            continue
        groups[day.isocalendar()[:2]].append(row)
    bars = []
    for _, rows in sorted(groups.items()):
        first, last = rows[0], rows[-1]
        highs = [_number(row.get("high"), 0.0) or 0.0 for row in rows]
        lows = [_number(row.get("low"), 0.0) or 0.0 for row in rows]
        amounts = [_number(row.get("amount_wan"), 0.0) or 0.0 for row in rows]
        bars.append({
            "date": last.get("date"),
            "open": _number(first.get("open"), _number(first.get("close"), 0.0)),
            "close": _number(last.get("close"), 0.0),
            "high": max(highs),
            "low": min(lows),
            "amount_wan": sum(amounts),
        })
    return bars


def monthly_bars(daily: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Aggregate daily bars into the large monthly cycle used by the plan."""
    groups: dict[tuple[int, int], list[dict[str, Any]]] = defaultdict(list)
    for row in daily:
        try:
            day = datetime.strptime(str(row.get("date") or "")[:10], "%Y-%m-%d").date()
        except ValueError:
            continue
        groups[(day.year, day.month)].append(row)
    bars = []
    for _, rows in sorted(groups.items()):
        first, last = rows[0], rows[-1]
        highs = [_number(row.get("high"), 0.0) or 0.0 for row in rows]
        lows = [_number(row.get("low"), 0.0) or 0.0 for row in rows]
        amounts = [_number(row.get("amount_wan"), 0.0) or 0.0 for row in rows]
        bars.append({
            "date": last.get("date"),
            "open": _number(first.get("open"), _number(first.get("close"), 0.0)),
            "close": _number(last.get("close"), 0.0),
            "high": max(highs),
            "low": min(lows),
            "amount_wan": sum(amounts),
        })
    return bars


def _cycle_state(rows: list[dict[str, Any]], fast: int, slow: int) -> dict[str, Any]:
    closes = [_number(row.get("close"), 0.0) or 0.0 for row in rows]
    close = closes[-1] if closes else None
    ma_fast, ma_slow = _ma(closes, fast), _ma(closes, slow)
    dif, dea, hist = _macd(closes)
    ready = bool(close and ma_fast and ma_slow and close >= ma_fast and ma_fast >= ma_slow and dif is not None and dea is not None and dif >= dea)
    state = "pass" if ready else ("weak" if close and ma_slow and close < ma_slow else "neutral")
    return {
        "state": state,
        "close": close,
        "ma_fast": ma_fast,
        "ma_slow": ma_slow,
        "macd_hist": hist,
        "reason": (
            f"收盘 {close:.2f}，MA{fast} {ma_fast:.2f}，MA{slow} {ma_slow:.2f}，MACD {'改善' if dif is not None and dea is not None and dif >= dea else '未改善'}"
            if close and ma_fast and ma_slow
            else "K线样本不足"
        ),
    }


def build_plan_context(daily: list[dict[str, Any]], quote: dict[str, Any]) -> dict[str, Any]:
    """Build auditable post-close evidence for ranking, not an order signal."""
    daily = [row for row in daily if _number(row.get("close"))]
    monthly = monthly_bars(daily)
    daily_cycle = _cycle_state(daily, 20, 60)
    monthly_cycle = _cycle_state(monthly, 5, 10)
    daily_kdj = kdj(daily, 9, 3, 3)
    monthly_kdj = kdj(monthly, 9, 3, 3)
    close = _number(quote.get("close"), daily_cycle.get("close"))
    recent = daily[-20:]
    amounts = [_number(row.get("amount_wan"), 0.0) or 0.0 for row in recent]
    closes = [_number(row.get("close"), 0.0) or 0.0 for row in recent]
    latest_amount = amounts[-1] if amounts else 0.0
    average_amount = mean(amounts[-6:-1]) if len(amounts) >= 6 else None
    volume_ratio = latest_amount / average_amount if average_amount else None
    distribution = bool(len(closes) >= 2 and closes[-1] < closes[-2] and volume_ratio and volume_ratio >= 1.5)
    chips_state = "distribution" if distribution else ("supportive" if daily_cycle["state"] == "pass" else "neutral")
    chips_reason = (
        "收跌且放量超过近5日均量，筹码有松动迹象"
        if distribution
        else ("日线均线与MACD结构未破坏，筹码代理暂可接受" if chips_state == "supportive" else "缺少可验证的日线承接优势")
    )
    slow_j = monthly_kdj.get("j")
    daily_j = daily_kdj.get("j")
    extended = bool((slow_j is not None and slow_j >= 90) or (daily_j is not None and daily_j >= 95))
    time_state = "protection" if extended else "normal"
    time_reason = "慢J进入高位保护区，仅提高承接要求，不因高J机械卖出" if extended else "未进入高位时间保护区"
    route = "TREND_CONTINUATION" if daily_cycle["state"] == "pass" and monthly_cycle["state"] == "pass" else "WATCH_REPAIR"
    score = 50
    score += 18 if monthly_cycle["state"] == "pass" else 0
    score += 18 if daily_cycle["state"] == "pass" else 0
    score += 8 if chips_state == "supportive" else -8 if chips_state == "distribution" else 0
    score += 4 if volume_ratio and volume_ratio >= 1.0 else 0
    score -= 8 if extended else 0
    large_cycle_ready = route == "TREND_CONTINUATION" and chips_state != "distribution"
    return {
        "version": VERSION,
        "plan_only": True,
        "route": route,
        "score": max(0, min(100, score)),
        "large_cycle_ready": large_cycle_ready,
        "daily": {**daily_cycle, "kdj": daily_kdj},
        "monthly": {**monthly_cycle, "kdj": monthly_kdj},
        "chips": {"state": chips_state, "reason": chips_reason, "volume_ratio": volume_ratio},
        "time": {"state": time_state, "reason": time_reason},
        "volume": {
            "role": "ranking_only",
            "ratio_to_5d": volume_ratio,
            "reason": "盘后量能只用于候选排序；不得独立触发盘中买点",
        },
        "execution_rule": "三周期：月线定大势、日线定中势、盘中60分钟定小势；盘后候选仅排序，盘中仍须主题/板块共振、VWAP/OR与分钟量能共同确认。",
    }


def execution_blockers(context: dict[str, Any] | None) -> list[str]:
    """Return only higher-timeframe blockers for a *new* entry."""
    if not context:
        return ["缺少盘后月线/日线/筹码/时间台账，只能观察"]
    blockers = []
    if not context.get("large_cycle_ready"):
        blockers.append("月线/日线主趋势未通过盘后验证")
    if (context.get("chips") or {}).get("state") == "distribution":
        blockers.append("筹码代理显示放量松动，禁止新增")
    if (context.get("time") or {}).get("state") == "protection":
        blockers.append("高位时间保护区，等待回踩承接，不直接追新仓")
    return blockers
