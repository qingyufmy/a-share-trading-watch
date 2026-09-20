"""Closed-bar intraday timing gate for the V2 multi-period execution framework.

The engine deliberately separates a full 120-minute structural decision from
the executable 5/15-minute gate.  When the historical 120-minute feed is not
available it remains explicit about that limitation and only enforces the
closed-bar execution/path protections that have sufficient data.
"""

from __future__ import annotations

from datetime import datetime, time as dtime
import hashlib
import json
import math
from pathlib import Path
from typing import Any
from core import method_entry, method_replay, repair_observation, entry_quality, timing_replay
from core import strategy_gap_research, observation_strategy_router


DEFAULT_CONFIG_PATH = Path(__file__).resolve().parents[1] / "configs" / "intraday_timing_v2.json"


def daily_anchor_execution_ready(contract, method_detail, config):
    """Use the MA5 method's closed-bar reclaim, not a second intraday anchor."""
    return bool(contract.get('key') == 'TREND_MA5' and contract.get('daily_qualified') is True
                and method_detail.get('eligible') is True and method_detail.get('anchor_name') == 'MA5'
                and method_detail.get('trigger_period') == '5m_closed'
                and ((config.get('method_execution') or {}).get('TREND_MA5') or {}).get('require_vwap_reclaim') is False)


def risk_reward_price_ceiling(target, invalidation, minimum_rr):
    try:
        target, invalidation, minimum_rr = map(float, (target, invalidation, minimum_rr))
        if not all(math.isfinite(x) for x in (target, invalidation, minimum_rr)) or not target > invalidation > 0 or minimum_rr <= 0:
            return None
        cap = (target + minimum_rr * invalidation) / (1 + minimum_rr)
        return round(math.floor(cap * 100 + 1e-9) / 100, 2)
    except (ValueError, TypeError):
        return None

TIMING_PATTERNS = {
    "V2_E1_TREND_PULLBACK_RECLAIM",
    "V2_E2_STRUCTURAL_SUPPORT_REVERSAL",
    "V2_E3_MA20_STRUCTURAL_RECLAIM",
    "V2_E4_BREAKOUT_RETEST",
    "V2_E5_LEADER_SECOND_LEG",
    "V2_E5A_LEADER_OPENING_REVERSAL",
    "V2_E5B_LEADER_OPENING_HOLD",
    "V2_E6_REPAIR_REGIME_UPGRADE",
    "V2_E7_FLAT_BASE_BREAKOUT",
}
# Only these method-owned scenarios may reach order and tracking layers.
# Raw timing patterns are evidence and can never create an order directly.
ENTRY_SCENARIOS = {
    "STRATEGY_LEADER_ENTRY",
    "STRATEGY_520_ENTRY",
    "STRATEGY_MA5_ENTRY",
}
EXIT_SCENARIOS = {
    "V2_REDUCE",
    "V2_TAKE_PROFIT",
    "V2_STRUCTURAL_EXIT",
}
BUY_SCENARIOS = ENTRY_SCENARIOS


def load_config(path: Path | None = None) -> dict[str, Any]:
    try:
        return json.loads((path or DEFAULT_CONFIG_PATH).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {
            "strategy_version": "three_method_strategy_v1",
            "fail_closed": True,
            "history": {"min_120m_bars": 240, "ma_periods": [5, 20, 99, 128, 225], "slope_lookback": 3, "atr_period": 14},
            "extension": {"extended_atr": 1.5, "climax_atr": 2.5},
            "location": {"near_level_atr": 0.35, "support_lookback_15m": 5, "resistance_lookback_15m": 20, "structural_lookback_120m": 3},
            "room_risk": {"min_room_atr": 1.0, "min_reward_risk": 1.5, "execution_band_atr": 0.1, "invalidation_atr": 0.35},
            "path": {"weak_close": 0.2, "rally_failure_high_to_now_atr": 0.9, "shock_high_to_now_atr": 1.5, "shock_rvol": 1.5},
            "execution": {
                "min_closed_5m_bars": 3,
                "min_closed_15m_bars": 1,
                "require_vwap_reclaim": True,
                "require_higher_low": True,
                "min_amount_ratio_1m": 1.0,
                "min_amount_ratio_5m": 1.0,
                "min_amount_ratio_1m_floor": 0.55,
                "min_amount_ratio_5m_floor": 0.8,
                "volume_confirmation_mode": "either",
                "max_vwap_distance_atr": 0.35,
            },
            "leader_second_leg": {
                "enabled": True,
                "min_intraday_15m_bars": 3,
                "min_pct": 3.0,
                "max_pct": 8.8,
                "min_amount_yi": 8.0,
                "max_vwap_distance_pct": 3.0,
                "min_pullback_retrace": 0.10,
                "max_pullback_retrace": 0.618,
                "base_volume_bars": 3,
                "min_resume_volume_ratio": 1.20,
                "require_5m_breakout": True,
                "min_limit_room_pct": 0.8,
                "strong_board_pct": 1.5,
            },
            "leader_opening_reversal": {
                "enabled": True,
                "start": "09:35",
                "end": "10:15",
                "min_closed_5m_bars": 2,
                "min_pct": 0.5,
                "max_pct": 6.5,
                "min_open_gap_pct": -7.0,
                "max_open_gap_pct": 3.0,
                "min_amount_yi": 0.5,
                "min_projected_amount_yi": 8.0,
                "min_turnover": 4.0,
                "max_vwap_distance_pct": 5.0,
                "min_limit_room_pct": 2.0,
                "strong_board_pct": 1.5,
            },
            "leader_opening_hold": {
                "enabled": True,
                "start": "09:35",
                "end": "10:00",
                "min_closed_5m_bars": 1,
                "min_pct": 1.0,
                "max_pct": 7.5,
                "min_open_gap_pct": 0.0,
                "max_open_gap_pct": 5.0,
                "min_projected_amount_yi": 8.0,
                "min_turnover": 4.0,
                "max_vwap_distance_pct": 3.0,
                "min_limit_room_pct": 2.0,
            },
            "method_execution": {
                "TREND_520": {"max_vwap_distance_atr": 0.60},
                "TREND_MA5": {"max_vwap_distance_atr": 0.60},
            },
            "repair_upgrade": {
                "enabled": True,
                "min_intraday_15m_bars": 4,
                "min_pct": 1.5,
                "max_pct": 6.5,
                "max_pullback_retrace": 0.6,
                "min_resume_volume_ratio": 0.9,
                "max_vwap_distance_pct": 1.8,
                "allowed_market_states": ["growth_lead", "broad_strong", "recovery", "rotation"],
                "probe_multiplier": 0.2,
            },
            "flat_base_breakout": {
                "enabled": True,
                "min_intraday_15m_bars": 6,
                "base_bars": 4,
                "min_pct": 1.0,
                "max_pct": 6.5,
                "min_amount_yi": 5.0,
                "max_base_range_atr": 5.0,
                "max_base_range_pct": 2.8,
                "min_breakout_atr": 0.25,
                "min_breakout_volume_ratio": 1.35,
                "max_vwap_distance_pct": 3.2,
                "min_limit_room_pct": 2.0,
                "strong_board_pct": 1.5,
                "probe_multiplier": 0.2,
            },
            "time": {"no_new_entry_after": "14:45", "risk_only_after": "14:15"},
            "exit_confirmation": {
                "soft_exit_not_before": "09:35",
                "min_closed_5m_bars": 1,
                "min_intraday_closed_15m_bars": 1,
            },
            "position": {"probe_multiplier": 0.33, "standard_multiplier": 1.0},
        }


def config_hash(config: dict[str, Any]) -> str:
    raw = json.dumps(config, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]


def _f(value: Any, default: float | None = None) -> float | None:
    try:
        parsed = float(value)
        return parsed if parsed == parsed else default
    except (TypeError, ValueError):
        return default


def _hm(value: Any) -> dtime | None:
    text = str(value or "")
    try:
        return datetime.strptime(text[-8:], "%H:%M:%S").time()
    except ValueError:
        try:
            return datetime.strptime(text[-5:], "%H:%M").time()
        except ValueError:
            return None


def _minute_timestamp_convention(rows: list[dict[str, Any]]) -> str:
    sources = {str(row.get("source") or "").lower() for row in (rows or [])}
    if sources & {"tencent", "sina"}:
        return "end"
    times = {str(row.get("m") or row.get("time") or "")[-8:] for row in (rows or [])}
    if "13:00:00" in times:
        return "start"
    if times & {"11:30:00", "15:00:00"}:
        return "end"
    return "start"


def _session_minute(point: dtime, convention: str = "start") -> int | None:
    total = point.hour * 60 + point.minute
    if convention == "end":
        if 9 * 60 + 31 <= total <= 11 * 60 + 30:
            return total - (9 * 60 + 31)
        if 13 * 60 + 1 <= total <= 15 * 60:
            return 120 + total - (13 * 60 + 1)
        return None
    if 9 * 60 + 30 <= total < 11 * 60 + 30:
        return total - (9 * 60 + 30)
    if 13 * 60 <= total < 15 * 60:
        return 120 + total - 13 * 60
    return None


def _completed_session_minutes(point: dtime) -> int:
    """Return the amount of the A-share session that is already complete."""
    total = point.hour * 60 + point.minute
    if total < 9 * 60 + 30:
        return 0
    if total < 11 * 60 + 30:
        return total - (9 * 60 + 30)
    if total < 13 * 60:
        return 120
    if total < 15 * 60:
        return 120 + total - 13 * 60
    return 240


def closed_bars(minute_rows: list[dict[str, Any]], minutes: int, now: datetime) -> list[dict[str, Any]]:
    """Build session-aware bars; lunch is never treated as a tradable interval."""
    convention = _minute_timestamp_convention(minute_rows)
    buckets: dict[int, dict[int, dict[str, Any]]] = {}
    for raw in minute_rows or []:
        point = _hm(raw.get("m") or raw.get("time"))
        price = _f(raw.get("c") if "c" in raw else raw.get("p") if "p" in raw else raw.get("close"))
        slot = _session_minute(point, convention) if point else None
        if slot is None or price is None or point > now.time():
            continue
        # Provider retries can repeat a minute. Keying by the session slot
        # keeps duplicates from hiding a missing minute and closing a bar.
        buckets.setdefault(slot // minutes, {})[slot] = {
            "time": point,
            "slot": slot,
            "price": price,
            "open": _f(raw.get("o") if "o" in raw else raw.get("open"), price) or price,
            "high": _f(raw.get("h") if "h" in raw else raw.get("high"), price) or price,
            "low": _f(raw.get("l") if "l" in raw else raw.get("low"), price) or price,
            "close": price,
            "volume": _f(raw.get("v") if "v" in raw else raw.get("volume"), 0.0) or 0.0,
        }
    out = []
    for bucket, slot_points in sorted(buckets.items()):
        points = list(slot_points.values())
        points.sort(key=lambda item: item["slot"])
        expected = min(minutes, 240 - bucket * minutes)
        end_slot = min(240, (bucket + 1) * minutes)
        required_slots = set(range(bucket * minutes, end_slot))
        if set(slot_points) != required_slots or len(points) != expected or _completed_session_minutes(now.time()) < end_slot:
            continue
        close_time = points[-1]["time"].strftime("%H:%M:%S")
        out.append({
            "open": points[0]["open"],
            "high": max(item["high"] for item in points),
            "low": min(item["low"] for item in points),
            "close": points[-1]["close"],
            "volume": sum(item["volume"] for item in points),
            "close_time": close_time,
            "bar_end": f"{now:%Y-%m-%d} {close_time}",
            "timestamp_convention": convention,
        })
    return out


def _merge_intraday_15m(
    history: list[dict[str, Any]], intraday: list[dict[str, Any]], now: datetime
) -> list[dict[str, Any]]:
    """Use locally completed bars as the authority for the current session."""
    current_day = now.strftime("%Y-%m-%d")
    merged = [bar for bar in (history or []) if not str(bar.get("bar_end") or bar.get("time") or "").startswith(current_day)]
    merged.extend(intraday or [])
    merged.sort(key=lambda bar: str(bar.get("bar_end") or bar.get("time") or ""))
    return merged


def _ma(values: list[float], period: int) -> float | None:
    return sum(values[-period:]) / period if len(values) >= period else None


def _atr(bars: list[dict[str, Any]], period: int) -> float | None:
    if len(bars) < 2:
        return None
    tr = []
    previous = bars[0]["close"]
    for bar in bars[1:]:
        tr.append(max(bar["high"] - bar["low"], abs(bar["high"] - previous), abs(bar["low"] - previous)))
        previous = bar["close"]
    sample = tr[-period:] if len(tr) >= period else tr
    return sum(sample) / len(sample) if sample else None


def _bar_close_time(bar: dict[str, Any]) -> str | None:
    """Return a displayable close timestamp for live and persisted bars."""
    value = bar.get("close_time") or bar.get("bar_end") or bar.get("time")
    text = str(value or "")
    return text[-8:] if text else None


def _daily_bridge_regime(row: dict[str, Any]) -> str:
    text = " ".join(str(row.get(key) or "") for key in ("state", "premarket_advice", "focus"))
    if any(token in text for token in ("强趋势", "趋势延续", "多头", "趋势")):
        return "TREND"
    if any(token in text for token in ("修复", "站回", "回踩")):
        return "REPAIR"
    return "UNKNOWN"


def _full_regime(history_120m: list[dict[str, Any]], config: dict[str, Any]) -> tuple[str, dict[str, Any], list[str]]:
    history_cfg = config["history"]
    closes = [_f(bar.get("close")) for bar in history_120m or []]
    closes = [value for value in closes if value is not None]
    minimum = int(history_cfg["min_120m_bars"])
    if len(closes) < minimum:
        return "UNKNOWN", {"bars": len(closes), "required": minimum}, ["120分钟历史不足，按fail-closed不能确认结构"]
    ma = {str(period): _ma(closes, int(period)) for period in history_cfg["ma_periods"]}
    slope_lookback = int(history_cfg["slope_lookback"])
    ma20_then = sum(closes[-20 - slope_lookback:-slope_lookback]) / 20
    ma20_now = ma.get("20")
    close = closes[-1]
    slope_up = ma20_now is not None and ma20_now > ma20_then
    if close < ma["225"] and not slope_up:
        regime = "BEAR"
    elif close > ma["99"] and close > ma["128"] and close > ma["225"] and slope_up:
        regime = "TREND"
    else:
        regime = "REPAIR"
    return regime, {"bars": len(closes), "ma": ma, "ma20_slope_up": slope_up}, []


def _slope(values: list[float], lookback: int = 3) -> float | None:
    if len(values) <= lookback:
        return None
    return values[-1] - values[-1 - lookback]


def _near(price: float, level: float | None, atr: float, multiple: float) -> bool:
    return level is not None and abs(price - level) <= max(atr, 0.01) * multiple


def _first_above(values: list[float], price: float) -> float | None:
    return min((value for value in values if value > price), default=None)


def _dynamic_levels(
    bars15: list[dict[str, Any]],
    history120: list[dict[str, Any]],
    full_detail: dict[str, Any],
    price: float,
    atr: float,
    config: dict[str, Any],
    row: dict[str, Any] | None = None,
) -> dict[str, float | None]:
    row = row or {}
    location_cfg = config.get("location") or {}
    rr_cfg = config.get("room_risk") or {}
    support_window = max(3, int(location_cfg.get("support_lookback_15m", 5)))
    resistance_window = max(5, int(location_cfg.get("resistance_lookback_15m", 20)))
    structural_window = max(2, int(location_cfg.get("structural_lookback_120m", 3)))
    closes15 = [bar["close"] for bar in bars15]
    ma5 = _ma(closes15, 5)
    ma20 = _ma(closes15, 20)
    support_slice = bars15[-support_window:]
    tier1 = min((bar["low"] for bar in support_slice), default=None)
    ma = full_detail.get("ma") if isinstance(full_detail, dict) else {}
    band_values = [value for value in (_f((ma or {}).get("99")), _f((ma or {}).get("128"))) if value]
    structural_band_low = min(band_values) if band_values else None
    structural_band_high = max(band_values) if band_values else None
    resistance_values = [bar["high"] for bar in bars15[-resistance_window:-1]]
    resistance_values.extend(bar.get("high") for bar in history120[-structural_window:] if _f(bar.get("high")) is not None)
    quote = row.get("quote") or {}
    dynamic = row.get("dynamic") or {}
    planned_resistance = [
        _f(row.get("pressure")),
        _f(dynamic.get("execution_pressure")),
        _f(dynamic.get("trend_pressure")),
        _f(quote.get("limit_up")),
    ]
    resistance_values.extend(value for value in planned_resistance if value is not None)
    min_resistance_atr = float(rr_cfg.get("min_resistance_distance_atr", 0.75))
    resistance_floor = price + max(atr, 0.01) * min_resistance_atr
    nearest_resistance = _first_above(
        [float(value) for value in resistance_values if _f(value) is not None],
        resistance_floor,
    )

    structural_candidates = [
        value for value in (tier1, ma20, structural_band_low, _f(row.get("defense")))
        if value is not None
    ]
    structural_base = min(structural_candidates) if structural_candidates else None
    structural_invalidation = (
        structural_base - max(atr * float(rr_cfg.get("invalidation_atr", 0.35)), 0.01)
        if structural_base is not None
        else None
    )
    min_entry_risk_atr = float(rr_cfg.get("min_entry_risk_atr", 0.75))
    entry_ceiling = price - max(atr, 0.01) * min_entry_risk_atr
    entry_candidates = [
        value for value in (
            ma5,
            ma20,
            tier1,
            _f(row.get("repair")),
            _f(row.get("defense")),
            _f(dynamic.get("pullback_support")),
            _f(dynamic.get("strong_invalid")),
        )
        if value is not None and 0 < float(value) <= entry_ceiling
    ]
    entry_base = max(entry_candidates) if entry_candidates else None
    entry_invalidation = (
        entry_base - max(atr * float(rr_cfg.get("entry_invalidation_atr", 0.2)), 0.01)
        if entry_base is not None
        else structural_invalidation
    )
    return {
        "tier1_support": tier1,
        "tier2_ma20": ma20,
        "ma5_15": ma5,
        "structural_band_low": structural_band_low,
        "structural_band_high": structural_band_high,
        "nearest_resistance": nearest_resistance,
        "entry_invalidation": entry_invalidation,
        "structural_invalidation": structural_invalidation,
    }


def _setup_state(
    regime: str,
    path_state: str,
    extension_state: str,
    bars5: list[dict[str, Any]],
    bars15: list[dict[str, Any]],
    levels: dict[str, float | None],
    price: float,
    vwap_reclaim: bool,
    higher_low: bool,
    atr: float,
    config: dict[str, Any],
    allowed_patterns: set[str] | None = None,
) -> tuple[str, list[str]]:
    """Return the one V2 entry pattern that the closed bars have earned."""
    blockers: list[str] = []
    if regime == "BEAR":
        return "WAIT", ["120分钟BEAR，不允许新开多"]
    if regime == "UNKNOWN":
        return "WAIT", ["120分钟历史不足，V2主引擎fail-closed"]
    if path_state in {"GAP_FAILURE", "DISTRIBUTION_SHOCK"}:
        return "WAIT", [f"路径硬否决：{path_state}"]
    if extension_state in {"EXTENDED", "CLIMAX"}:
        return "WAIT", [f"延伸状态 {extension_state}，禁止追价"]
    if len(bars15) < 20:
        return "WAIT", ["15分钟已收盘历史不足，无法确认Setup"]

    near_multiple = float((config.get("location") or {}).get("near_level_atr", 0.35))
    ma5 = levels.get("ma5_15")
    ma20 = levels.get("tier2_ma20")
    tier1 = levels.get("tier1_support")
    previous = bars15[-2]
    current = bars15[-1]
    ma5_series = [_ma([bar["close"] for bar in bars15[:index]], 5) for index in range(5, len(bars15) + 1)]
    ma5_series = [value for value in ma5_series if value is not None]
    ma5_up = bool((_slope(ma5_series) or 0) > 0)
    support_hold = bool(
        tier1 is not None
        and current["low"] >= tier1 - atr * near_multiple
        and current["close"] >= current["low"] + (current["high"] - current["low"]) * 0.45
    )
    selling_pressure_easing = bool(current["volume"] <= previous["volume"] * 1.15 or current["close"] >= previous["close"])
    reclaim = bool(ma20 is not None and previous["close"] < ma20 <= current["close"] and ma5_up)
    recent_high = max((bar["high"] for bar in bars15[-8:-1]), default=None)
    breakout_retest = bool(
        recent_high is not None
        and previous["high"] >= recent_high
        and current["low"] >= recent_high - atr * near_multiple
        and current["close"] >= recent_high
    )
    trend_pullback = bool(
        regime == "TREND"
        and ma20 is not None
        and any(bar["low"] <= ma20 + atr * near_multiple for bar in bars15[-3:])
        and current["close"] >= ma5
        and ma5_up
    )

    # A 15-minute setup is structural evidence.  The 5-minute VWAP/higher-low
    # trigger is evaluated later as an independent execution gate.  Coupling
    # both here required them to become true on the same refresh and could
    # discard a valid setup before its short-cycle trigger arrived.
    structural_candidates = (
        ("E1_TREND_PULLBACK_RECLAIM", regime == "TREND" and trend_pullback),
        ("E2_STRUCTURAL_SUPPORT_REVERSAL", support_hold and selling_pressure_easing),
        ("E3_MA20_STRUCTURAL_RECLAIM", reclaim),
        ("E4_BREAKOUT_RETEST", breakout_retest),
    )
    disallowed = []
    for candidate, formed in structural_candidates:
        if not formed:
            continue
        if allowed_patterns is None or f"V2_{candidate}" in allowed_patterns:
            return candidate, blockers
        disallowed.append(candidate)
    if disallowed:
        return "WAIT", [
            "策略合同：15分钟结构已出现，但不属于当前方法允许路径：" + "、".join(disallowed)
        ]
    if regime == "REPAIR":
        blockers.append("REPAIR只允许E2/E3/E4确认，当前15分钟Setup未完成")
    else:
        blockers.append("15分钟Setup未完成")
    return "WAIT", blockers


def recent_breakout_evidence(bars5, price, atr, max_age_bars=3):
    """Reconstruct a bounded confirmation from closed bars, with invalidation.

    A lunch break cannot carry morning evidence into the afternoon. The event
    must hold its breakout level and its low until the current decision.
    """
    if len(bars5) < 3:
        return {"confirmed": False}
    latest_time = _bar_time(bars5[-1])
    for index in range(len(bars5) - 1, max(1, len(bars5) - 2 - max_age_bars), -1):
        bar = bars5[index]
        if _f(bar.get('low')) is None:
            continue
        level = max(float(x['high']) for x in bars5[index - 2:index])
        if float(bar['close']) < level or float(bar['close']) <= float(bars5[index - 1]['close']):
            continue
        event_time = _bar_time(bar)
        if latest_time and event_time and (latest_time - event_time).total_seconds() > max_age_bars * 300:
            continue
        event_atr = _atr(bars5[:index + 1], 14) or atr
        invalid = float(bar['low']) - max(event_atr * 0.2, 0.01)
        held = all(float(x['low']) > invalid and float(x['close']) >= level for x in bars5[index + 1:])
        if held and price >= level and price > invalid:
            return {"confirmed": True, "bar_end": bar.get('bar_end'), "age_bars": len(bars5) - 1 - index,
                    "level": level, "invalidation": invalid}
    return {"confirmed": False}


def _bar_time(bar):
    try:
        return datetime.fromisoformat(str(bar.get('bar_end')))
    except (TypeError, ValueError):
        return None


def _leader_second_leg_state(
    row: dict[str, Any],
    intraday_bars15: list[dict[str, Any]],
    bars5: list[dict[str, Any]],
    vwap: float | None,
    atr: float,
    price: float,
    regime: str,
    path_state: str,
    vwap_reclaim: bool,
    higher_low: bool,
    config: dict[str, Any],
) -> dict[str, Any]:
    """Recognise a liquid sector leader only after its first pullback resumes."""
    cfg = config.get("leader_second_leg") or {}
    quote = row.get("quote") or {}
    pct = _f(quote.get("pct"))
    prev = _f(quote.get("prev_close"))
    if pct is None and prev and price:
        pct = (price - prev) / prev * 100
    pct = pct or 0.0
    amount_yi = (_f(quote.get("amount_wan"), 0.0) or 0.0) / 10000
    momentum = row.get("sector_momentum") or {}
    rotation = row.get("sector_rotation") or {}
    board_pct = _f(momentum.get("board_pct"))
    sector_confirmed = bool(
        momentum.get("emotion_ok") is True
        and (
            (rotation.get("sustained") and rotation.get("leader_healthy"))
            or (board_pct is not None and board_pct >= float(cfg.get("strong_board_pct", 1.5)))
        )
    )
    detail: dict[str, Any] = {
        "eligible": False,
        "sector_confirmed": sector_confirmed,
        "board_name": momentum.get("board_name") or momentum.get("sector_name"),
        "board_pct": board_pct,
        "pct": round(pct, 3),
        "amount_yi": round(amount_yi, 3),
        "reason": "尚未进入强板块龙头二次转强评估",
    }
    if not cfg.get("enabled", True):
        detail["reason"] = "E5强板块龙头二次转强通道未启用"
        return detail
    minimum = max(3, int(cfg.get("min_intraday_15m_bars", 3)))
    if len(intraday_bars15) < minimum:
        detail["reason"] = f"E5等待首波、回踩与二次转强：当日15m {len(intraday_bars15)}/{minimum}"
        return detail
    if regime not in {"TREND", "REPAIR"} or path_state in {"GAP_FAILURE", "DISTRIBUTION_SHOCK"}:
        detail["reason"] = f"E5结构/路径不合格：{regime}/{path_state}"
        return detail
    if not (float(cfg.get("min_pct", 3.0)) <= pct <= float(cfg.get("max_pct", 8.8))):
        detail["reason"] = f"E5涨幅不在受控区间：{pct:.2f}%"
        return detail
    if amount_yi < float(cfg.get("min_amount_yi", 8.0)):
        detail["reason"] = f"E5成交辨识度不足：{amount_yi:.2f}亿"
        return detail
    if not sector_confirmed:
        detail["reason"] = "E5板块情绪/持续性未确认，单股脉冲不入场"
        return detail
    if vwap is None:
        detail["reason"] = "E5缺少VWAP，无法计算受控执行位置"
        return detail
    vwap_distance_pct = (price - vwap) / vwap * 100 if vwap else None
    detail["vwap_distance_pct"] = round(vwap_distance_pct, 3) if vwap_distance_pct is not None else None
    if vwap_distance_pct is None or not (0 <= vwap_distance_pct <= float(cfg.get("max_vwap_distance_pct", 3.0))):
        detail["reason"] = f"E5距VWAP {vwap_distance_pct:.2f}% 超出受控区间" if vwap_distance_pct is not None else "E5缺少VWAP"
        return detail

    current = intraday_bars15[-1]
    prior = intraday_bars15[:-1]
    minimum_retrace = float(cfg.get("min_pullback_retrace", 0.10))
    maximum_retrace = float(cfg.get("max_pullback_retrace", 0.618))
    structure = None
    # Freeze the first completed intraday impulse.  Using the rolling maximum
    # migrated the impulse anchor forward on every new high, repeatedly reset
    # the pullback clock and made a valid leader wait until the move was over.
    for impulse_index in range(0, len(prior) - 1):
        impulse_bar = prior[impulse_index]
        next_bar = prior[impulse_index + 1]
        impulse_high = float(impulse_bar["high"])
        if impulse_high < float(next_bar["high"]):
            continue
        impulse_low = min(float(bar["low"]) for bar in intraday_bars15[: impulse_index + 1])
        impulse_range = max(impulse_high - impulse_low, atr, 0.01)
        pullback_bars = prior[impulse_index + 1:]
        if not pullback_bars:
            continue
        pullback = min(pullback_bars, key=lambda bar: float(bar["low"]))
        pullback_low = float(pullback["low"])
        retrace = (impulse_high - pullback_low) / impulse_range
        if minimum_retrace <= retrace <= maximum_retrace:
            structure = (impulse_index, impulse_high, impulse_low, pullback_bars, pullback_low, retrace)
            break
    if structure is None:
        detail["reason"] = "E5首波后尚未形成受控回踩（需10%-61.8%回撤）"
        return detail

    impulse_index, impulse_high, impulse_low, pullback_bars, pullback_low, retrace = structure
    base_count = max(1, int(cfg.get("base_volume_bars", 3)))
    base_bars = pullback_bars[-base_count:]
    base_average_volume = sum(float(bar.get("volume") or 0) for bar in base_bars) / max(len(base_bars), 1)
    resume_volume_ratio = float(current.get("volume") or 0) / max(base_average_volume, 1.0)
    repair = _f(row.get("repair"))
    last_base_high = float(pullback_bars[-1]["high"])
    limit_price = _f(quote.get('limit_up'))
    if limit_price and last_base_high >= limit_price - 1e-8:
        detail.update(status='NOT_APPLICABLE_LIMIT_CEILING',
                      reason='E5回踩段前高已是涨停价，无法再严格突破；回封属于独立形态，不能无限等待或自动放行')
        return detail
    repair_ceiling = min(repair, impulse_high) if repair else last_base_high
    resume_level = max(last_base_high, repair_ceiling)
    breakout_evidence = recent_breakout_evidence(bars5, price, atr, int(cfg.get('breakout_valid_bars', 3)))
    five_minute_breakout = breakout_evidence['confirmed']
    resumed = bool(
        float(current["close"]) >= resume_level
        and float(current["high"]) > last_base_high
        and float(current["close"]) > float(intraday_bars15[-2]["close"])
        and resume_volume_ratio >= float(cfg.get("min_resume_volume_ratio", 1.20))
        and (five_minute_breakout or not cfg.get("require_5m_breakout", True))
    )
    pressure = _f(row.get("pressure"))
    limit_up = _f((row.get('quote') or {}).get('limit_up'))
    if pressure and limit_up:
        pressure = min(pressure, limit_up)
    target_room_pct = (pressure - price) / price * 100 if pressure and pressure > price else None
    limit_room_pct = (limit_up - price) / price * 100 if limit_up else None
    detail.update({
        "impulse_high": round(impulse_high, 6),
        "impulse_low": round(impulse_low, 6),
        "impulse_index": impulse_index,
        "impulse_bar_end": intraday_bars15[impulse_index].get("bar_end"),
        "pullback_low": round(pullback_low, 6),
        "retrace": round(retrace, 3),
        "resume_level": round(resume_level, 6),
        "base_average_volume": round(base_average_volume, 3),
        "resume_volume_ratio": round(resume_volume_ratio, 3),
        "five_minute_breakout": five_minute_breakout,
        "breakout_evidence": breakout_evidence,
        "target_room_pct": round(target_room_pct, 3) if target_room_pct is not None else None,
        "limit_room_pct": round(limit_room_pct, 3) if limit_room_pct is not None else None,
    })
    if not resumed:
        missing = []
        if float(current["close"]) < resume_level:
            missing.append(f"15m收盘未站上{resume_level:.2f}")
        if resume_volume_ratio < float(cfg.get("min_resume_volume_ratio", 1.20)):
            missing.append(f"转强量比{resume_volume_ratio:.2f}<1.20")
        if cfg.get("require_5m_breakout", True) and not five_minute_breakout:
            missing.append("5m未突破最近两根闭合高点")
        detail["reason"] = "E5回踩后二次转强尚未确认：" + "；".join(missing or ["价格未转强"])
        return detail
    if target_room_pct is None or target_room_pct < float(cfg.get("min_limit_room_pct", 0.8)):
        detail["reason"] = "E5至结构目标空间不足，禁止追价（不等于距涨停空间）"
        return detail
    if limit_room_pct is not None and limit_room_pct < float(cfg.get("min_limit_room_pct", 0.8)):
        detail["reason"] = "E5距实际涨停价空间不足，禁止追价"
        return detail
    detail["eligible"] = True
    detail["invalidation"] = round(pullback_low - max(atr * 0.2, 0.01), 6)
    detail["target"] = pressure
    detail["reason"] = "强板块高辨识度龙头：首段冲高已锁定，受控回踩后15m放量转强且5m突破成立"
    return detail


def _leader_opening_reversal_state(
    row: dict[str, Any],
    bars5: list[dict[str, Any]],
    vwap: float | None,
    atr: float,
    price: float,
    regime: str,
    path_state: str,
    now: datetime,
    config: dict[str, Any],
) -> dict[str, Any]:
    """Confirm a prior failed-limit leader's next-day opening weak-to-strong move."""
    cfg = config.get("leader_opening_reversal") or {}
    quote = row.get("quote") or {}
    contract = row.get("strategy_contract") or {}
    metrics = contract.get("daily_metrics") or {}
    pct = _f(quote.get("pct"))
    prev = _f(quote.get("prev_close"))
    open_price = _f(quote.get("open"), price) or price
    if pct is None and prev:
        pct = (price - prev) / prev * 100
    pct = pct or 0.0
    gap_pct = (open_price - prev) / prev * 100 if prev else 0.0
    amount_yi = (_f(quote.get("amount_wan"), 0.0) or 0.0) / 10000.0
    turnover = _f(quote.get("turnover"), 0.0) or 0.0
    elapsed_fraction = max(0.05, min(1.0, _completed_session_minutes(now.time()) / 240.0))
    projected_amount_yi = amount_yi / elapsed_fraction
    momentum = row.get("sector_momentum") or {}
    rotation = row.get("sector_rotation") or {}
    board_pct = _f(momentum.get("board_pct"))
    sector_confirmed = bool(
        momentum.get("emotion_ok") is True
        and rotation.get("sustained")
        and rotation.get("leader_healthy")
        and board_pct is not None
        and board_pct >= 0.8
    )
    detail: dict[str, Any] = {
        "eligible": False,
        "sector_confirmed": sector_confirmed,
        "board_name": momentum.get("board_name") or momentum.get("sector_name"),
        "board_pct": board_pct,
        "pct": round(pct, 3),
        "gap_pct": round(gap_pct, 3),
        "amount_yi": round(amount_yi, 3),
        "projected_amount_yi": round(projected_amount_yi, 3),
        "turnover": round(turnover, 3),
        "reason": "尚未进入E5A开盘弱转强评估",
    }
    if not cfg.get("enabled", True):
        detail["reason"] = "E5A开盘弱转强通道未启用"
        return detail
    if str(contract.get("key") or "") != "LEADER_EMOTION":
        detail["reason"] = "E5A仅对盘前龙头战法合同开放"
        return detail
    if not (
        str(metrics.get("leader_profile") or "") == "FAILED_LIMIT_REVERSAL"
        and metrics.get("emotion_pool_cross_verified") is True
        and metrics.get("prior_day_touched_limit") is True
        and metrics.get("prior_day_closed_limit") is not True
    ):
        detail["reason"] = "E5A缺少昨日触板回落与东财/开盘啦双榜证据"
        cross_status = metrics.get('emotion_pool_cross_status')
        if cross_status:
            detail['cross_status'] = cross_status
            detail['reason'] += '；' + {'stale': '开盘啦榜单过期', 'unavailable': '开盘啦榜单缺失',
                'not_listed': '完整榜单未上榜', 'not_observed_partial': '部分榜单尚未观察到',
                'verified': '双榜已验证，昨日触板回落形态仍须满足'}.get(cross_status, '双榜状态待核对')
        return detail
    start = datetime.strptime(str(cfg.get("start", "09:35")), "%H:%M").time()
    end = datetime.strptime(str(cfg.get("end", "10:15")), "%H:%M").time()
    if not (start <= now.time() <= end):
        detail["reason"] = f"E5A只在{start:%H:%M}-{end:%H:%M}评估开盘弱转强"
        return detail
    minimum_bars = max(2, int(cfg.get("min_closed_5m_bars", 2)))
    if len(bars5) < minimum_bars:
        detail["reason"] = f"E5A等待闭合5m确认：{len(bars5)}/{minimum_bars}"
        return detail
    if regime not in {"TREND", "REPAIR"} or path_state in {"GAP_FAILURE", "DISTRIBUTION_SHOCK"}:
        detail["reason"] = f"E5A结构/路径不合格：{regime}/{path_state}"
        return detail
    if not (float(cfg.get("min_open_gap_pct", -7.0)) <= gap_pct <= float(cfg.get("max_open_gap_pct", 3.0))):
        detail["reason"] = f"E5A开盘缺口不在受控区间：{gap_pct:.2f}%"
        return detail
    if not (float(cfg.get("min_pct", 0.5)) <= pct <= float(cfg.get("max_pct", 6.5))):
        detail["reason"] = f"E5A涨幅不在受控区间：{pct:.2f}%"
        return detail
    liquidity_ok = bool(
        (amount_yi >= float(cfg.get("min_amount_yi", 0.5))
         and projected_amount_yi >= float(cfg.get("min_projected_amount_yi", 8.0)))
        or turnover >= float(cfg.get("min_turnover", 4.0))
    )
    detail["liquidity_ok"] = liquidity_ok
    if not liquidity_ok:
        detail["reason"] = f"E5A开盘承接不足：成交{amount_yi:.2f}亿/折算{projected_amount_yi:.2f}亿，换手{turnover:.2f}%"
        return detail
    if not sector_confirmed:
        detail["reason"] = "E5A当日板块连续共振或领涨承接未确认"
        return detail
    if vwap is None:
        detail["reason"] = "E5A缺少VWAP"
        return detail
    vwap_distance_pct = (price - vwap) / vwap * 100 if vwap else None
    detail["vwap_distance_pct"] = round(vwap_distance_pct, 3) if vwap_distance_pct is not None else None
    if vwap_distance_pct is None or not (0 <= vwap_distance_pct <= float(cfg.get("max_vwap_distance_pct", 5.0))):
        detail["reason"] = f"E5A距VWAP {vwap_distance_pct:.2f}% 超出受控区间" if vwap_distance_pct is not None else "E5A缺少VWAP"
        return detail
    prior_bar, current_bar = bars5[-2], bars5[-1]
    # The first bar may still be below the evolving session VWAP after a deep
    # negative gap.  It must recover from its open; the second closed bar owns
    # the actual VWAP reclaim and breakout confirmation.
    vwap_hold = bool(
        float(prior_bar["close"]) > float(prior_bar["open"])
        and float(current_bar["close"]) >= vwap
    )
    higher_low = bool(float(current_bar["low"]) > float(prior_bar["low"]))
    breakout = bool(
        float(current_bar["close"]) >= float(prior_bar["high"])
        and float(current_bar["close"]) > float(prior_bar["close"])
    )
    detail.update({"vwap_hold": vwap_hold, "higher_low": higher_low, "five_minute_breakout": breakout})
    if not (vwap_hold and higher_low and breakout):
        missing = []
        if not vwap_hold:
            missing.append("首根5m未修复或第二根5m未收复VWAP")
        if not higher_low:
            missing.append("5m低点未抬高")
        if not breakout:
            missing.append("最新5m未突破前一根高点")
        detail["reason"] = "E5A弱转强闭合未完成：" + "；".join(missing)
        return detail
    pressure = _f(row.get("pressure"))
    limit_room_pct = (pressure - price) / price * 100 if pressure and pressure > price else None
    detail["limit_room_pct"] = round(limit_room_pct, 3) if limit_room_pct is not None else None
    if limit_room_pct is None or limit_room_pct < float(cfg.get("min_limit_room_pct", 2.0)):
        detail["reason"] = "E5A已接近涨停/压力边界，禁止追价"
        return detail
    detail["eligible"] = True
    detail["invalidation"] = round(max(vwap, float(current_bar["low"])) - max(atr * 0.15, 0.01), 6)
    detail["target"] = pressure
    detail["reason"] = "昨日触板回落双榜候选：当日板块连续共振，两根闭合5m站稳VWAP、低点抬高并突破前高"
    return detail


def _leader_opening_hold_state(
    row: dict[str, Any],
    bars5: list[dict[str, Any]],
    vwap: float | None,
    atr: float,
    price: float,
    regime: str,
    path_state: str,
    now: datetime,
    config: dict[str, Any],
) -> dict[str, Any]:
    """Confirm an already-qualified leader holding a controlled opening gap."""
    cfg = config.get("leader_opening_hold") or {}
    quote = row.get("quote") or {}
    contract = row.get("strategy_contract") or {}
    prev = _f(quote.get("prev_close"))
    open_price = _f(quote.get("open"), price) or price
    pct = _f(quote.get("pct"))
    if pct is None and prev:
        pct = (price - prev) / prev * 100.0
    pct = pct or 0.0
    gap_pct = (open_price - prev) / prev * 100.0 if prev else 0.0
    amount_yi = (_f(quote.get("amount_wan"), 0.0) or 0.0) / 10000.0
    turnover = _f(quote.get("turnover"), 0.0) or 0.0
    elapsed_fraction = max(0.05, min(1.0, _completed_session_minutes(now.time()) / 240.0))
    projected_amount_yi = amount_yi / elapsed_fraction
    momentum = row.get("sector_momentum") or {}
    rotation = row.get("sector_rotation") or {}
    board_pct = _f(momentum.get("board_pct"))
    sector_confirmed = bool(
        momentum.get("emotion_ok") is True
        and rotation.get("sustained")
        and rotation.get("leader_healthy")
        and board_pct is not None
        and board_pct >= 0.8
    )
    detail: dict[str, Any] = {
        "eligible": False,
        "sector_confirmed": sector_confirmed,
        "board_name": momentum.get("board_name") or momentum.get("sector_name"),
        "board_pct": board_pct,
        "pct": round(pct, 3),
        "gap_pct": round(gap_pct, 3),
        "amount_yi": round(amount_yi, 3),
        "projected_amount_yi": round(projected_amount_yi, 3),
        "turnover": round(turnover, 3),
        "reason": "尚未进入龙头开盘强承接评估",
    }
    if not cfg.get("enabled", True):
        detail["reason"] = "龙头开盘强承接通道未启用"
        return detail
    if str(contract.get("key") or "") != "LEADER_EMOTION" or contract.get("daily_qualified") is not True:
        detail["reason"] = "开盘强承接仅对盘前合格龙头合同开放"
        return detail
    start = datetime.strptime(str(cfg.get("start", "09:35")), "%H:%M").time()
    end = datetime.strptime(str(cfg.get("end", "10:00")), "%H:%M").time()
    if not (start <= now.time() <= end):
        detail["reason"] = f"开盘强承接只在{start:%H:%M}-{end:%H:%M}评估"
        return detail
    minimum_bars = max(1, int(cfg.get("min_closed_5m_bars", 1)))
    if len(bars5) < minimum_bars:
        detail["reason"] = f"开盘强承接等待闭合5m确认：{len(bars5)}/{minimum_bars}"
        return detail
    if regime not in {"TREND", "REPAIR"} or path_state in {"GAP_FAILURE", "DISTRIBUTION_SHOCK"}:
        detail["reason"] = f"开盘强承接结构/路径不合格：{regime}/{path_state}"
        return detail
    min_gap, max_gap = float(cfg.get('min_open_gap_pct', 0.0)), float(cfg.get('max_open_gap_pct', 5.0))
    if not prev or prev <= 0 or not (min_gap <= gap_pct <= max_gap):
        detail["reason"] = f"龙头开盘缺口不在{min_gap:g}%-{max_gap:g}%受控区间或昨收缺失：{gap_pct:.2f}%"
        return detail
    if gap_pct < 0:
        detail['entry_variant'] = 'SMALL_GAP_RECLAIM'
        if (len(bars5) < 2 or vwap is None
                or any(float(b['close']) < max(prev, vwap) for b in bars5[-2:])
                or float(bars5[-1]['low']) < float(bars5[-2]['low'])
                or float(bars5[-1]['close']) <= float(bars5[-2]['high'])
                or price < max(prev, vwap)):
            detail['reason'] = '轻微低开龙头等待两根闭合5m收复昨收/VWAP、抬高低点并突破前高'
            return detail
    if not (float(cfg.get("min_pct", 1.0)) <= pct <= float(cfg.get("max_pct", 7.5))):
        detail["reason"] = f"龙头涨幅不在开盘承接受控区间：{pct:.2f}%"
        return detail
    liquidity_ok = bool(
        projected_amount_yi >= float(cfg.get("min_projected_amount_yi", 8.0))
        or turnover >= float(cfg.get("min_turnover", 4.0))
    )
    detail["liquidity_ok"] = liquidity_ok
    if not liquidity_ok:
        detail["reason"] = f"龙头开盘流动性不足：折算成交{projected_amount_yi:.2f}亿，换手{turnover:.2f}%"
        return detail
    if not sector_confirmed:
        detail["reason"] = "龙头开盘强承接仍缺当日板块连续共振与领涨健康"
        return detail
    if vwap is None:
        detail["reason"] = "龙头开盘强承接缺少VWAP"
        return detail
    current_bar = bars5[-1]
    bar_range = max(float(current_bar["high"]) - float(current_bar["low"]), 0.01)
    close_location = (float(current_bar["close"]) - float(current_bar["low"])) / bar_range
    controlled_low = float(current_bar["low"]) >= min(open_price, vwap) - max(atr * 0.25, 0.01)
    vwap_hold = float(current_bar["close"]) >= vwap
    opening_hold = float(current_bar["close"]) >= open_price and close_location >= 0.60
    vwap_distance_pct = (price - vwap) / vwap * 100.0 if vwap else None
    detail.update({
        "controlled_low": controlled_low,
        "vwap_hold": vwap_hold,
        "opening_hold": opening_hold,
        "close_location": round(close_location, 3),
        "vwap_distance_pct": round(vwap_distance_pct, 3) if vwap_distance_pct is not None else None,
    })
    if not (controlled_low and vwap_hold and opening_hold):
        missing = []
        if not controlled_low:
            missing.append("首根闭合5m下探失控")
        if not vwap_hold:
            missing.append("首根闭合5m未站稳VWAP")
        if not opening_hold:
            missing.append("首根闭合5m未站回开盘价或收盘位置偏弱")
        detail["reason"] = "龙头开盘强承接未完成：" + "；".join(missing)
        return detail
    max_distance = float(cfg.get("max_vwap_distance_pct", 3.0))
    if vwap_distance_pct is None or not (0 <= vwap_distance_pct <= max_distance):
        detail["reason"] = f"龙头开盘距VWAP过远：{vwap_distance_pct:.2f}% > {max_distance:.2f}%"
        return detail
    targets = entry_quality.target_evidence(row, price, float(current_bar['close']),
                                            _estimated_limit_up(row, prev))
    detail['target_evidence'] = targets
    pressure = targets['target']
    if targets['unconfirmed_reclaims']:
        detail['reason'] = '旧压力尚未由闭合5分钟突破，不能跳到更远目标'
        return detail
    limit_room_pct = (pressure - price) / price * 100.0 if pressure and pressure > price else None
    detail["limit_room_pct"] = round(limit_room_pct, 3) if limit_room_pct is not None else None
    if limit_room_pct is None or limit_room_pct < float(cfg.get("min_limit_room_pct", 2.0)):
        detail["reason"] = "龙头开盘已接近涨停/压力边界，禁止追价"
        return detail
    detail["eligible"] = True
    detail["invalidation"] = round(max(vwap, min(open_price, float(current_bar["low"]))) - max(atr * 0.20, 0.01), 6)
    detail["target"] = pressure
    detail["reason"] = "盘前合格龙头：0%-5%受控高开，板块连续共振，首根闭合5m站稳开盘价与VWAP且承接位置强"
    return detail


def _repair_upgrade_state(
    row: dict[str, Any],
    intraday_bars15: list[dict[str, Any]],
    vwap: float | None,
    atr: float,
    price: float,
    regime: str,
    path_state: str,
    extension_state: str,
    vwap_reclaim: bool,
    higher_low: bool,
    market_data: dict[str, Any],
    config: dict[str, Any],
) -> dict[str, Any]:
    """Upgrade a REPAIR candidate only after a broad/sector-backed first retest."""
    cfg = config.get("repair_upgrade") or {}
    quote = row.get("quote") or {}
    pct = _f(quote.get("pct"), 0.0) or 0.0
    market = market_data.get("a_share_market_regime") or {}
    market_state = str(market.get("state") or "unknown")
    rotation = row.get("sector_rotation") or {}
    sector_confirmed = bool(rotation.get("sustained") and rotation.get("leader_healthy"))
    detail: dict[str, Any] = {
        "eligible": False,
        "market_state": market_state,
        "sector_confirmed": sector_confirmed,
        "pct": round(pct, 3),
        "reason": "尚未进入修复升级评估",
    }
    if not cfg.get("enabled", True):
        detail["reason"] = "E6修复升级通道未启用"
        return detail
    if regime != "REPAIR" or path_state in {"GAP_FAILURE", "DISTRIBUTION_SHOCK"}:
        detail["reason"] = f"E6仅接受REPAIR且无路径硬失败：{regime}/{path_state}"
        return detail
    if extension_state in {"EXTENDED", "CLIMAX"}:
        detail["reason"] = f"E6位置已延伸：{extension_state}"
        return detail
    if market_state not in set(cfg.get("allowed_market_states") or []):
        detail["reason"] = f"E6市场修复环境未确认：{market_state}"
        return detail
    if not sector_confirmed:
        detail["reason"] = "E6板块连续性或龙头健康度未确认"
        return detail
    minimum = int(cfg.get("min_intraday_15m_bars", 4))
    if len(intraday_bars15) < minimum:
        detail["reason"] = f"E6等待首次健康回踩与再收复：当日15m {len(intraday_bars15)}/{minimum}"
        return detail
    if not (float(cfg.get("min_pct", 1.5)) <= pct <= float(cfg.get("max_pct", 6.5))):
        detail["reason"] = f"E6涨幅不在受控修复区间：{pct:.2f}%"
        return detail
    if vwap is None:
        detail["reason"] = "E6缺少VWAP，无法计算受控执行位置"
        return detail
    vwap_distance_pct = (price - vwap) / vwap * 100
    detail["vwap_distance_pct"] = round(vwap_distance_pct, 3)
    if not (0 <= vwap_distance_pct <= float(cfg.get("max_vwap_distance_pct", 1.8))):
        detail["reason"] = f"E6距VWAP {vwap_distance_pct:.2f}% 超出受控区间"
        return detail

    current = intraday_bars15[-1]
    pullback = intraday_bars15[-2]
    prior = intraday_bars15[:-2]
    impulse_high = max(float(bar["high"]) for bar in prior)
    impulse_low = min(float(bar["low"]) for bar in prior)
    impulse_range = max(impulse_high - impulse_low, atr, 0.01)
    retrace = max(0.0, (impulse_high - float(pullback["low"])) / impulse_range)
    resume_volume_ratio = float(current.get("volume") or 0) / max(float(pullback.get("volume") or 0), 1.0)
    resume_level = float(pullback["high"])
    resumed = bool(
        float(current["close"]) >= resume_level
        and float(current["close"]) > float(pullback["close"])
        and resume_volume_ratio >= float(cfg.get("min_resume_volume_ratio", 0.9))
    )
    detail.update({
        "impulse_high": round(impulse_high, 6),
        "pullback_low": round(float(pullback["low"]), 6),
        "retrace": round(retrace, 3),
        "resume_level": round(resume_level, 6),
        "resume_volume_ratio": round(resume_volume_ratio, 3),
    })
    if retrace > float(cfg.get("max_pullback_retrace", 0.6)):
        detail["reason"] = f"E6首次回踩过深：{retrace:.2f}"
        return detail
    if not resumed:
        detail["reason"] = "E6首次健康回踩后的15分钟再收复尚未完成"
        return detail
    pressure = _f(row.get("pressure"))
    detail["eligible"] = True
    detail["invalidation"] = round(float(pullback["low"]) - max(atr * 0.2, 0.01), 6)
    detail["target"] = pressure
    detail["reason"] = "市场修复与板块连续共振，15m首次健康回踩后再收复结构成立"
    return detail


def _estimated_limit_up(row: dict[str, Any], prev_close: float | None) -> float | None:
    if not prev_close or prev_close <= 0:
        return None
    quote = row.get("quote") or {}
    explicit = _f(quote.get("limit_up"))
    if explicit:
        return explicit
    code = str(row.get("code") or quote.get("code") or "")
    name = str(row.get("name") or quote.get("name") or "").upper()
    if "ST" in name:
        ratio = 1.05
    elif code.startswith(("300", "301", "688")):
        ratio = 1.20
    elif code.startswith(("4", "8", "92")):
        ratio = 1.30
    else:
        ratio = 1.10
    return round(prev_close * ratio + 1e-9, 2)


def _flat_base_breakout_state(
    row: dict[str, Any],
    intraday_bars15: list[dict[str, Any]],
    vwap: float | None,
    atr: float,
    price: float,
    regime: str,
    path_state: str,
    vwap_reclaim: bool,
    higher_low: bool,
    config: dict[str, Any],
) -> dict[str, Any]:
    """Admit a small probe when a prequalified trend name exits a real base.

    This route is deliberately distinct from E5.  It does not require a prior
    impulse/pullback pair, but it does require a completed 15m base, a
    volume-backed close above that base, strong sector evidence and room before
    the daily limit.  It is therefore an event-upgrade route, not a chase rule.
    """
    cfg = config.get("flat_base_breakout") or {}
    quote = row.get("quote") or {}
    prev = _f(quote.get("prev_close"))
    pct = _f(quote.get("pct"))
    if pct is None and prev:
        pct = (price - prev) / prev * 100
    pct = pct or 0.0
    amount_yi = (_f(quote.get("amount_wan"), 0.0) or 0.0) / 10000
    momentum = row.get("sector_momentum") or {}
    rotation = row.get("sector_rotation") or {}
    board_pct = _f(momentum.get("board_pct"))
    full_sector_confirmation = bool(rotation.get("sustained") and rotation.get("leader_healthy"))
    strong_board_confirmation = bool(
        momentum.get("emotion_ok") is True
        and board_pct is not None
        and board_pct >= float(cfg.get("strong_board_pct", 1.5))
    )
    sector_confirmed = bool(full_sector_confirmation or strong_board_confirmation)
    detail: dict[str, Any] = {
        "eligible": False,
        "sector_confirmed": sector_confirmed,
        "sector_confirmation": "FULL" if full_sector_confirmation else "STRONG_BOARD_PROXY" if strong_board_confirmation else "NONE",
        "board_name": momentum.get("board_name") or momentum.get("sector_name"),
        "board_pct": board_pct,
        "pct": round(pct, 3),
        "amount_yi": round(amount_yi, 3),
        "reason": "尚未进入横盘突破评估",
    }
    if not cfg.get("enabled", True):
        detail["reason"] = "E7横盘突破通道未启用"
        return detail
    minimum = int(cfg.get("min_intraday_15m_bars", 6))
    base_count = max(3, int(cfg.get("base_bars", 4)))
    if len(intraday_bars15) < max(minimum, base_count + 1):
        detail["reason"] = f"E7等待完整日内平台：15m {len(intraday_bars15)}/{max(minimum, base_count + 1)}"
        return detail
    if regime not in {"TREND", "REPAIR"} or path_state in {"GAP_FAILURE", "DISTRIBUTION_SHOCK"}:
        detail["reason"] = f"E7结构/路径不合格：{regime}/{path_state}"
        return detail
    if not (float(cfg.get("min_pct", 1.0)) <= pct <= float(cfg.get("max_pct", 6.5))):
        detail["reason"] = f"E7涨幅不在可试错区间：{pct:.2f}%"
        return detail
    if amount_yi < float(cfg.get("min_amount_yi", 5.0)):
        detail["reason"] = f"E7成交辨识度不足：{amount_yi:.2f}亿"
        return detail
    if not sector_confirmed:
        detail["reason"] = "E7板块强度或连续承接未确认"
        return detail
    if vwap is None:
        detail["reason"] = "E7缺少VWAP，无法计算受控执行位置"
        return detail
    vwap_distance_pct = (price - vwap) / vwap * 100
    detail["vwap_distance_pct"] = round(vwap_distance_pct, 3)
    if not (0 <= vwap_distance_pct <= float(cfg.get("max_vwap_distance_pct", 3.2))):
        detail["reason"] = f"E7距VWAP {vwap_distance_pct:.2f}% 超出受控区间"
        return detail

    current = intraday_bars15[-1]
    base = intraday_bars15[-1 - base_count:-1]
    base_high = max(float(bar["high"]) for bar in base)
    base_low = min(float(bar["low"]) for bar in base)
    base_range = max(base_high - base_low, 0.0)
    base_range_atr = base_range / max(atr, 0.01)
    base_range_pct = base_range / max(base_low, 0.01) * 100
    base_volumes = sorted(float(bar.get("volume") or 0) for bar in base)
    middle = len(base_volumes) // 2
    median_base_volume = (
        base_volumes[middle]
        if len(base_volumes) % 2
        else (base_volumes[middle - 1] + base_volumes[middle]) / 2
    )
    breakout_volume_ratio = float(current.get("volume") or 0) / max(median_base_volume, 1.0)
    breakout_atr = (float(current["close"]) - base_high) / max(atr, 0.01)
    current_range = max(float(current["high"]) - float(current["low"]), 0.01)
    current_clv = (float(current["close"]) - float(current["low"])) / current_range
    detail.update({
        "base_high": round(base_high, 6),
        "base_low": round(base_low, 6),
        "base_range_atr": round(base_range_atr, 3),
        "base_range_pct": round(base_range_pct, 3),
        "breakout_atr": round(breakout_atr, 3),
        "breakout_volume_ratio": round(breakout_volume_ratio, 3),
        "breakout_clv": round(current_clv, 3),
    })
    if (
        base_range_atr > float(cfg.get("max_base_range_atr", 5.0))
        and base_range_pct > float(cfg.get("max_base_range_pct", 2.8))
    ):
        detail["reason"] = f"E7平台过宽：{base_range_atr:.2f}ATR/{base_range_pct:.2f}%"
        return detail
    if breakout_atr < float(cfg.get("min_breakout_atr", 0.25)) or current_clv < 0.65:
        detail["reason"] = "E7已收盘15分钟尚未有效突破平台上沿"
        return detail
    if breakout_volume_ratio < float(cfg.get("min_breakout_volume_ratio", 1.35)):
        detail["reason"] = f"E7突破量能不足：{breakout_volume_ratio:.2f}"
        return detail

    pressure = _f(row.get("pressure"))
    limit_up = _estimated_limit_up(row, prev)
    targets = [value for value in (pressure, limit_up) if value is not None and value > price]
    target = min(targets) if targets else None
    limit_room_pct = (float(target) - price) / price * 100 if target else None
    detail["limit_room_pct"] = round(limit_room_pct, 3) if limit_room_pct is not None else None
    if limit_room_pct is None or limit_room_pct < float(cfg.get("min_limit_room_pct", 2.0)):
        detail["reason"] = "E7上方压力/涨停空间不足，禁止追价"
        return detail
    detail["eligible"] = True
    detail["invalidation"] = round(base_high - max(atr * 0.25, 0.01), 6)
    detail["target"] = target
    detail["reason"] = "盘前合格趋势票：15m平台放量突破，板块强度共振"
    return detail


def _blocker_diagnostics(blockers: list[str]) -> list[dict[str, Any]]:
    hard_tokens = ("BEAR", "UNKNOWN", "路径硬否决", "交叉校验不一致", "数据降级", "后不新开仓")
    out = []
    for reason in blockers:
        hard = any(token in reason for token in hard_tokens)
        out.append({"reason": reason, "severity": "HARD" if hard else "TEMPORARY", "retryable": not hard})
    return out


def _exit_state(
    price: float,
    vwap: float | None,
    bars15: list[dict[str, Any]],
    history120: list[dict[str, Any]],
    levels: dict[str, float | None],
    path_state: str,
    extension_state: str,
    sellable_qty: int,
    bars5: list[dict[str, Any]] | None = None,
    intraday_bars15: list[dict[str, Any]] | None = None,
    now: datetime | None = None,
    config: dict[str, Any] | None = None,
) -> tuple[str, str, float | None]:
    """Apply the V2 exit hierarchy to sellable positions only."""
    if sellable_qty <= 0:
        return "NO_POSITION_ACTION", "无可卖仓位，T+1锁定部分仅记录风险", None
    invalidation = levels.get("structural_invalidation")
    if (
        invalidation is not None
        and price < float(invalidation)
        and path_state in {"GAP_FAILURE", "DISTRIBUTION_SHOCK"}
    ):
        return "STRUCTURAL_EXIT", "数据降级下价格跌破结构失效位且路径硬失败，风险退出优先", invalidation
    if len(bars15) < 20:
        return "NO_ADD", "15分钟历史不足：已有仓位停止加仓，等待数据恢复；硬失效仍可退出", invalidation
    closes = [bar["close"] for bar in bars15]
    ma5 = levels.get("ma5_15")
    ma20 = levels.get("tier2_ma20")
    ma5_series = [_ma(closes[:index], 5) for index in range(5, len(closes) + 1)]
    ma5_series = [value for value in ma5_series if value is not None]
    ma5_down = bool((_slope(ma5_series) or 0) < 0)
    lower_high = len(bars15) >= 3 and bars15[-1]["high"] < bars15[-2]["high"] <= bars15[-3]["high"]
    vwap_loss = bool(vwap and price < vwap)
    rally_failure = path_state == "RALLY_FAILURE" or (vwap_loss and len(bars15) >= 2 and bars15[-1]["close"] < bars15[-2]["close"])
    band_low = levels.get("structural_band_low")
    support120 = min((float(bar.get("low")) for bar in history120[-3:] if _f(bar.get("low")) is not None), default=None)
    structural_break = bool(
        band_low is not None
        and support120 is not None
        and price < band_low
        and price < support120
        and len(history120) >= 2
        and float(history120[-1].get("close") or price) < band_low
    )
    if structural_break:
        return "STRUCTURAL_EXIT", "MA99/128结构带与120分钟关键支撑同步失守", levels.get("structural_invalidation")
    exit_cfg = (config or {}).get("exit_confirmation") or {}
    minimum_5m = int(exit_cfg.get("min_closed_5m_bars", 1))
    minimum_15m = int(exit_cfg.get("min_intraday_closed_15m_bars", 1))
    not_before = datetime.strptime(str(exit_cfg.get("soft_exit_not_before", "09:35")), "%H:%M").time()
    current_time = (now or datetime.now()).time()
    soft_exit_mature = bool(
        current_time >= not_before
        and len(bars5 or []) >= minimum_5m
        and len(intraday_bars15 or []) >= minimum_15m
    )
    if not soft_exit_mature:
        return (
            "NO_ADD",
            f"开盘软退出待确认：当日已收盘5m {len(bars5 or [])}/{minimum_5m}、"
            f"15m {len(intraday_bars15 or [])}/{minimum_15m}；结构硬失效仍即时执行",
            invalidation,
        )
    if extension_state in {"EXTENDED", "CLIMAX"} and vwap_loss and lower_high:
        return "TAKE_PROFIT", "延伸后突破失败、VWAP失守且5/15分钟高点下移", ma20
    if ma20 is not None and ma5 is not None and price < ma20 and ma5 < ma20 and ma5_down and rally_failure:
        return "REDUCE", "价格跌破15分钟MA20、MA5下穿且反抽/VWAP修复失败", ma20
    if vwap_loss and ma5 is not None and price < ma5 and ma5_down:
        return "NO_ADD", "SOFT_RISK：跌破VWAP且15分钟MA5下拐，停止加仓并观察", ma5
    return "HOLD", "未触发V2减仓/退出组合条件", None


def evaluate(
    row: dict[str, Any],
    minute_rows: list[dict[str, Any]],
    now: datetime | None = None,
    history_120m: list[dict[str, Any]] | None = None,
    history_15m: list[dict[str, Any]] | None = None,
    config: dict[str, Any] | None = None,
    market_data: dict[str, Any] | None = None,
    position: dict[str, Any] | None = None,
) -> dict[str, Any]:
    config = config or load_config()
    now = now or datetime.now()
    quote = row.get("quote") or {}
    feat = row.get("rt_features") or {}
    price = _f(quote.get("close"), 0.0) or 0.0
    bars5 = closed_bars(minute_rows, 5, now)
    intraday_bars15 = closed_bars(minute_rows, 15, now)
    bars15 = _merge_intraday_15m(list(history_15m or []), intraday_bars15, now)
    full_regime, full_detail, full_blockers = _full_regime(history_120m or [], config)
    has_full_structure = bool(history_120m) and full_regime != "UNKNOWN"
    # V2 must fail closed.  The old daily-text bridge can be retained for
    # observability only, but it must not restore entry permission when the
    # required 120m history is missing.
    regime = full_regime
    mode = "FULL_120M" if has_full_structure else "EXECUTION_GUARD"
    blockers = []
    market_data = market_data if isinstance(market_data, dict) else {}
    market_entry_ready = market_data.get("entry_ready", True)
    if market_entry_ready is False:
        blockers.extend(str(item) for item in (market_data.get("blockers") or ["本地行情数据降级"]))
    reasons = list(full_blockers if not has_full_structure else [])
    strategy_contract = row.get("strategy_contract") or {}
    contract_patterns = set(str(item) for item in strategy_contract.get("allowed_patterns") or [])
    contract_key = str(strategy_contract.get("key") or "")
    readiness = observation_strategy_router.contract_readiness(strategy_contract)
    contract_ready = readiness['ready']
    if not contract_ready:
        blockers.append(readiness['reason'])
    required_5m = int(config["execution"]["min_closed_5m_bars"])
    if "V2_E5B_LEADER_OPENING_HOLD" in contract_patterns:
        required_5m = min(
            required_5m,
            max(1, int((config.get("leader_opening_hold") or {}).get("min_closed_5m_bars", 1))),
        )
    if "V2_E5A_LEADER_OPENING_REVERSAL" in contract_patterns:
        required_5m = min(
            required_5m,
            max(2, int((config.get("leader_opening_reversal") or {}).get("min_closed_5m_bars", 2))),
        )
    daily_method = contract_key in {'TREND_520', 'TREND_MA5'}
    required_15m = 0 if daily_method else int(config["history"].get("min_15m_bars", config["execution"]["min_closed_15m_bars"]))
    data_ready = len(bars5) >= required_5m and len(bars15) >= required_15m and price > 0
    if not data_ready:
        blockers.append(f"已收盘5m/15m不足：{len(bars5)}/{required_5m}、{len(bars15)}/{required_15m}")

    atr5 = _atr(bars5, int(config["history"]["atr_period"])) or _f(feat.get("atr5m"), 0.0) or max(price * 0.003, 0.01)
    high = _f(quote.get("high"), price) or price
    low = _f(quote.get("low"), price) or price
    open_price = _f(quote.get("open"), price) or price
    prev = _f(quote.get("prev_close"), price) or price
    vwap = _f(feat.get("vwap"))
    clv = (price - low) / (high - low) if high > low else 0.5
    high_to_now_atr = (high - price) / max(atr5, 0.01)
    rvol = _f(feat.get("amount_ratio_5m"), 0.0) or 0.0
    rvol_1m = _f(feat.get("amount_ratio_1m"), 0.0) or 0.0
    gap_pct = (open_price - prev) / prev * 100 if prev else 0.0
    path_cfg = config["path"]
    if vwap and price < vwap and high_to_now_atr >= float(path_cfg["shock_high_to_now_atr"]) and clv <= float(path_cfg["weak_close"]) and rvol >= float(path_cfg["shock_rvol"]):
        path_state = "DISTRIBUTION_SHOCK"
    elif gap_pct < -2 and price <= min(open_price, _f(row.get("defense"), price) or price):
        path_state = "GAP_FAILURE"
    elif vwap and price < vwap and high_to_now_atr >= float(path_cfg["rally_failure_high_to_now_atr"]):
        path_state = "RALLY_FAILURE"
    elif gap_pct > 1 and vwap and price >= vwap:
        path_state = "GAP_HOLD"
    else:
        path_state = "NORMAL"
    hard_block = path_state in {"DISTRIBUTION_SHOCK", "GAP_FAILURE"}
    if hard_block:
        blockers.append(f"路径硬否决：{path_state}")

    closes5 = [bar["close"] for bar in bars5]
    lows5 = [bar["low"] for bar in bars5]
    vwap_reclaim = bool(vwap and len(closes5) >= 2 and closes5[-1] >= vwap and closes5[-2] >= vwap)
    higher_low = len(lows5) >= 3 and lows5[-1] >= lows5[-2] and lows5[-2] >= lows5[-3]
    vwap_hold = bool(vwap and closes5 and closes5[-1] >= vwap)
    execution_state = "VWAP_RECLAIM" if vwap_reclaim and higher_low else "VWAP_HOLD" if vwap_hold else "FAILURE"
    ma20_15 = _ma([bar["close"] for bar in bars15], 20)
    extension = (price - ma20_15) / max(atr5, 0.01) if ma20_15 else 0.0
    if extension >= float(config["extension"]["climax_atr"]):
        extension_state = "CLIMAX"
    elif extension >= float(config["extension"]["extended_atr"]):
        extension_state = "EXTENDED"
    else:
        extension_state = "NORMAL"

    levels = _dynamic_levels(bars15, history_120m or [], full_detail, price, atr5, config, row=row)
    exit_levels = dict(levels)
    method_detail = method_entry.evaluate(strategy_contract, bars5, price, now,
                                         config.get('daily_method_entry') or {}) if daily_method else {}
    weak_repair = entry_quality.weak_repair_evidence(strategy_contract, bars5, price, vwap, regime, path_state)
    if weak_repair['required'] and not weak_repair['confirmed']:
        blockers.append('MA5弱势修复尚未完成：需闭合5分钟突破前高、阳线抬低且转强量不低于回踩量')
    leadership = entry_quality.leader_identity(row, config) if contract_key == 'LEADER_EMOTION' else {}
    if leadership and not leadership['confirmed']:
        blockers.append(leadership['reason'])
    entry_higher_low = (len(lows5) >= 2 and lows5[-1] >= lows5[-2]) if daily_method else higher_low
    if daily_method:
        execution_state = 'VWAP_RECLAIM' if vwap_reclaim and entry_higher_low else 'VWAP_HOLD' if vwap_hold else 'FAILURE'
    near_multiple = float((config.get("location") or {}).get("near_level_atr", 0.35))
    if _near(price, levels.get("tier1_support"), atr5, near_multiple):
        location = "TIER1_SUPPORT"
    elif _near(price, levels.get("tier2_ma20"), atr5, near_multiple):
        location = "TIER2_MA20"
    elif levels.get("tier2_ma20") is not None and price >= float(levels["tier2_ma20"]):
        location = "STRUCTURAL_RECLAIM"
    else:
        location = "MID_AIR"
    def contract_allows(pattern: str) -> bool:
        return contract_ready and pattern in contract_patterns

    leader_second_leg = _leader_second_leg_state(
        row,
        intraday_bars15,
        bars5,
        vwap,
        atr5,
        price,
        regime,
        path_state,
        vwap_reclaim,
        higher_low,
        config,
    )
    leader_opening_reversal = _leader_opening_reversal_state(
        row,
        bars5,
        vwap,
        atr5,
        price,
        regime,
        path_state,
        now,
        config,
    )
    leader_opening_hold = _leader_opening_hold_state(
        row,
        bars5,
        vwap,
        atr5,
        price,
        regime,
        path_state,
        now,
        config,
    )
    repair_upgrade = _repair_upgrade_state(
        row,
        intraday_bars15,
        vwap,
        atr5,
        price,
        regime,
        path_state,
        extension_state,
        vwap_reclaim,
        higher_low,
        market_data,
        config,
    )
    flat_base_breakout = _flat_base_breakout_state(
        row,
        intraday_bars15,
        vwap,
        atr5,
        price,
        regime,
        path_state,
        vwap_reclaim,
        higher_low,
        config,
    )
    leader_route_windows = {}
    for pattern, name in [('V2_E5B_LEADER_OPENING_HOLD', 'leader_opening_hold'),
                          ('V2_E5A_LEADER_OPENING_REVERSAL', 'leader_opening_reversal')]:
        route_config = config.get(name) or {}
        start = datetime.strptime(str(route_config.get('start', '09:35')), '%H:%M').time()
        end = datetime.strptime(str(route_config.get('end', '10:00' if name == 'leader_opening_hold' else '10:15')), '%H:%M').time()
        leader_route_windows[pattern] = 'pending' if now.time() < start else 'expired' if now.time() > end else 'active'
    if daily_method:
        setup_15m = method_detail.get('pattern') if method_detail.get('eligible') else 'WAIT'
        setup_blockers = list(method_detail.get('blockers') or [])
        if setup_15m != 'WAIT' and not contract_allows(f'V2_{setup_15m}'):
            setup_15m = 'WAIT'
            setup_blockers.append('日线方法触发未在盘前合同授权范围内')
        location = 'DAILY_' + str(method_detail.get('anchor_name') or 'WAIT')
        levels['nearest_resistance'] = method_detail.get('target')
        limit_up = _f(quote.get('limit_up'))
        if levels['nearest_resistance'] and limit_up:
            levels['nearest_resistance'] = min(levels['nearest_resistance'], limit_up)
        levels['entry_invalidation'] = method_detail.get('invalidation')
    elif leader_opening_hold.get("eligible") and contract_allows("V2_E5B_LEADER_OPENING_HOLD"):
        setup_15m, setup_blockers = "E5B_LEADER_OPENING_HOLD", []
        location = "LEADER_OPENING_HOLD"
        levels["nearest_resistance"] = leader_opening_hold.get("target")
        levels["entry_invalidation"] = leader_opening_hold.get("invalidation")
    elif leader_opening_reversal.get("eligible") and contract_allows("V2_E5A_LEADER_OPENING_REVERSAL"):
        setup_15m, setup_blockers = "E5A_LEADER_OPENING_REVERSAL", []
        location = "LEADER_OPENING_REVERSAL"
        levels["nearest_resistance"] = leader_opening_reversal.get("target")
        levels["entry_invalidation"] = leader_opening_reversal.get("invalidation")
    elif leader_second_leg.get("eligible") and contract_allows("V2_E5_LEADER_SECOND_LEG"):
        setup_15m, setup_blockers = "E5_LEADER_SECOND_LEG", []
        location = "LEADER_SECOND_LEG"
        levels["nearest_resistance"] = leader_second_leg.get("target")
        levels["entry_invalidation"] = leader_second_leg.get("invalidation")
    elif repair_upgrade.get("eligible") and contract_allows("V2_E6_REPAIR_REGIME_UPGRADE"):
        setup_15m, setup_blockers = "E6_REPAIR_REGIME_UPGRADE", []
        location = "REPAIR_UPGRADE"
        levels["nearest_resistance"] = repair_upgrade.get("target")
        levels["entry_invalidation"] = repair_upgrade.get("invalidation")
    elif flat_base_breakout.get("eligible") and contract_allows("V2_E7_FLAT_BASE_BREAKOUT"):
        setup_15m, setup_blockers = "E7_FLAT_BASE_BREAKOUT", []
        location = "FLAT_BASE_BREAKOUT"
        levels["nearest_resistance"] = flat_base_breakout.get("target")
        levels["entry_invalidation"] = flat_base_breakout.get("invalidation")
    elif contract_ready and contract_key == "LEADER_EMOTION":
        # Retain expired routes in audit metadata, not the actionable queue.
        route_details = {
            "V2_E5B_LEADER_OPENING_HOLD": leader_opening_hold,
            "V2_E5A_LEADER_OPENING_REVERSAL": leader_opening_reversal,
            "V2_E5_LEADER_SECOND_LEG": leader_second_leg,
        }
        setup_15m = "WAIT"
        setup_blockers = [str(route_details[pattern].get("reason") or "龙头路径待确认")
                          for pattern in strategy_contract.get("allowed_patterns") or []
                          if pattern in route_details and leader_route_windows.get(pattern) != 'expired']
        if not setup_blockers:
            setup_blockers = [str(route_details[p].get('reason') or '授权路径窗口已结束')
                              for p in strategy_contract.get('allowed_patterns') or [] if p in route_details]
    else:
        setup_15m, setup_blockers = _setup_state(
            regime,
            path_state,
            extension_state,
            bars5,
            bars15,
            levels,
            price,
            vwap_reclaim,
            higher_low,
            atr5,
            config,
            contract_patterns,
        )
    blockers.extend(setup_blockers)
    leader_cfg = config.get("leader_second_leg") or {}
    if (
        not leader_opening_hold.get("eligible")
        and not leader_opening_reversal.get("eligible")
        and not leader_second_leg.get("eligible")
        and not repair_upgrade.get("eligible")
        and not flat_base_breakout.get("eligible")
        and float(leader_second_leg.get("pct") or 0) >= float(leader_cfg.get("min_pct", 3.0))
        and float(leader_second_leg.get("amount_yi") or 0) >= float(leader_cfg.get("min_amount_yi", 8.0))
        and contract_allows("V2_E5_LEADER_SECOND_LEG")
    ):
        blockers.append(str(leader_second_leg.get("reason") or "E5二次转强未完成"))
    opening_cfg = config.get("leader_opening_reversal") or {}
    opening_hold_cfg = config.get("leader_opening_hold") or {}
    if (
        not leader_opening_hold.get("eligible")
        and contract_allows("V2_E5B_LEADER_OPENING_HOLD")
        and datetime.strptime(str(opening_hold_cfg.get("start", "09:35")), "%H:%M").time() <= now.time()
        <= datetime.strptime(str(opening_hold_cfg.get("end", "10:00")), "%H:%M").time()
    ):
        blockers.append(str(leader_opening_hold.get("reason") or "龙头开盘强承接未完成"))
    opening_profile = str((strategy_contract.get("daily_metrics") or {}).get("leader_profile") or "")
    if (
        not leader_opening_reversal.get("eligible")
        and opening_profile == "FAILED_LIMIT_REVERSAL"
        and contract_allows("V2_E5A_LEADER_OPENING_REVERSAL")
        and datetime.strptime(str(opening_cfg.get("start", "09:35")), "%H:%M").time() <= now.time()
        <= datetime.strptime(str(opening_cfg.get("end", "10:15")), "%H:%M").time()
    ):
        blockers.append(str(leader_opening_reversal.get("reason") or "E5A开盘弱转强未完成"))
    opening_hold_route = setup_15m == "E5B_LEADER_OPENING_HOLD"
    opening_reversal_route = setup_15m == "E5A_LEADER_OPENING_REVERSAL"
    opening_route = opening_hold_route or opening_reversal_route
    anchor_execution = daily_anchor_execution_ready(strategy_contract, method_detail, config)
    if anchor_execution:
        execution_state = "DAILY_MA5_RECLAIM"
    if config["execution"]["require_vwap_reclaim"] and not vwap_reclaim and not opening_route and not anchor_execution:
        blockers.append("5分钟已收盘VWAP收复未完成")
    if config["execution"]["require_higher_low"] and not entry_higher_low and not opening_route:
        blockers.append("5分钟未形成抬高低点")
    if location == "MID_AIR" and setup_15m not in {"E5_LEADER_SECOND_LEG", "E5A_LEADER_OPENING_REVERSAL", "E5B_LEADER_OPENING_HOLD"}:
        blockers.append("位置处于MID_AIR，非支撑/结构收复入场")
    if regime in {"BEAR", "UNKNOWN"}:
        blockers.append(f"结构状态 {regime} 不允许新开多")

    # V2 owns the complete execution gate.  A later order layer must not
    # recompute different volume, VWAP-distance, or pressure rules and turn a
    # displayed V2 entry into an opaque rejection.
    execution_cfg = config.get("execution") or {}
    min_rvol_1m = float(execution_cfg.get("min_amount_ratio_1m", 1.0))
    min_rvol_5m = float(execution_cfg.get("min_amount_ratio_5m", 1.0))
    floor_rvol_1m = float(execution_cfg.get("min_amount_ratio_1m_floor", 0.55))
    floor_rvol_5m = float(execution_cfg.get("min_amount_ratio_5m_floor", 0.8))
    volume_confirmation_mode = str(execution_cfg.get("volume_confirmation_mode", "either")).lower()
    method_cfg = (config.get("method_execution") or {}).get(contract_key) or {}
    max_vwap_distance_atr = float(method_cfg.get("max_vwap_distance_atr", execution_cfg.get("max_vwap_distance_atr", 0.35)))
    vwap_distance_period = str(method_cfg.get('vwap_distance_period', '5m')) if daily_method else '5m'
    distance_atr = (float(method_detail.get('daily_atr14') or 0) if vwap_distance_period == '1d'
                    else atr5 if vwap_distance_period == '5m' else 0)
    vwap_distance_atr = (price - vwap) / max(distance_atr, 0.01) if vwap is not None and distance_atr > 0 else None
    volume_1m_ok = rvol_1m >= min_rvol_1m
    volume_5m_ok = rvol >= min_rvol_5m
    volume_floors_ok = rvol_1m >= floor_rvol_1m and rvol >= floor_rvol_5m
    volume_confirmed = volume_floors_ok and (
        volume_1m_ok and volume_5m_ok
        if volume_confirmation_mode == "both"
        else volume_1m_ok or volume_5m_ok
    )
    leader_route = setup_15m in {"E5_LEADER_SECOND_LEG", "E5A_LEADER_OPENING_REVERSAL", "E5B_LEADER_OPENING_HOLD"}
    repair_route = setup_15m == "E6_REPAIR_REGIME_UPGRADE"
    breakout_route = setup_15m == "E7_FLAT_BASE_BREAKOUT"
    vwap_distance_ok = bool(
        (leader_opening_hold if opening_hold_route else leader_opening_reversal if opening_reversal_route else leader_second_leg).get("vwap_distance_pct") is not None
        and 0 <= float((leader_opening_hold if opening_hold_route else leader_opening_reversal if opening_reversal_route else leader_second_leg)["vwap_distance_pct"])
        <= float((opening_hold_cfg if opening_hold_route else opening_cfg if opening_reversal_route else leader_cfg).get("max_vwap_distance_pct", 3.0))
    ) if leader_route else (
        bool(
            repair_upgrade.get("vwap_distance_pct") is not None
            and 0 <= float(repair_upgrade["vwap_distance_pct"]) <= float((config.get("repair_upgrade") or {}).get("max_vwap_distance_pct", 1.8))
        ) if repair_route else (
            bool(
                flat_base_breakout.get("vwap_distance_pct") is not None
                and 0 <= float(flat_base_breakout["vwap_distance_pct"])
                <= float((config.get("flat_base_breakout") or {}).get("max_vwap_distance_pct", 3.2))
            ) if breakout_route else bool(vwap_distance_atr is not None and vwap_distance_atr <= max_vwap_distance_atr)
        )
    )
    if setup_15m != "WAIT" and not volume_confirmed:
        blockers.append(
            "量能未形成可执行确认："
            f"1m {rvol_1m:.2f}/{min_rvol_1m:.2f}（底线{floor_rvol_1m:.2f}），"
            f"5m {rvol:.2f}/{min_rvol_5m:.2f}（底线{floor_rvol_5m:.2f}），"
            f"模式{volume_confirmation_mode}"
        )
    if setup_15m != "WAIT" and not vwap_distance_ok:
        if leader_route:
            leader_detail = leader_opening_hold if opening_hold_route else leader_opening_reversal if opening_reversal_route else leader_second_leg
            route_cfg = opening_hold_cfg if opening_hold_route else opening_cfg if opening_reversal_route else leader_cfg
            distance_pct = leader_detail.get("vwap_distance_pct")
            detail = "VWAP不可用" if distance_pct is None else (
                f"{'E5B' if opening_hold_route else 'E5A' if opening_reversal_route else 'E5'}现价距VWAP过远：{float(distance_pct):.2f}% > {float(route_cfg.get('max_vwap_distance_pct', 3.0)):.2f}%"
            )
        elif repair_route:
            distance_pct = repair_upgrade.get("vwap_distance_pct")
            detail = "VWAP不可用" if distance_pct is None else (
                f"E6现价距VWAP过远：{float(distance_pct):.2f}%"
            )
        elif breakout_route:
            distance_pct = flat_base_breakout.get("vwap_distance_pct")
            max_distance_pct = float((config.get("flat_base_breakout") or {}).get("max_vwap_distance_pct", 3.2))
            detail = "VWAP不可用" if distance_pct is None else (
                f"E7现价距VWAP过远：{float(distance_pct):.2f}% > {max_distance_pct:.2f}%"
            )
        else:
            detail = "VWAP不可用" if vwap_distance_atr is None else (
                f"现价距VWAP过远：{vwap_distance_atr:.2f}ATR > {max_vwap_distance_atr:.2f}ATR（周期{vwap_distance_period}）"
            )
        blockers.append(detail)

    market_gate = dict(market_data.get("market_gate") or {})
    market_gate_override = False
    if market_gate.get("allowed") is False:
        market_stage = str(market_gate.get("stage") or "UNKNOWN")
        market_reason = str(market_gate.get("reason") or "市场与板块门控未通过")
        route_sector_confirmed = bool(
            (leader_route and (leader_opening_hold if opening_hold_route else leader_opening_reversal if opening_reversal_route else leader_second_leg).get("sector_confirmed"))
            or (repair_route and repair_upgrade.get("sector_confirmed"))
            or (breakout_route and flat_base_breakout.get("sector_confirmed"))
        )
        # The route already proves the candidate's own sector strength.  Do not
        # count the same weak global rotation sample a second time.  This narrow
        # override never bypasses broad-market, data-quality, or path hard vetoes.
        if market_stage == "CORE_SECTOR_ROTATION_BLOCKED" and route_sector_confirmed:
            market_gate_override = True
            reasons.append(f"候选板块已由{setup_15m}独立确认，解除重复板块轮动扣分；仅允许小额试仓")
        else:
            blockers.append(f"V2市场/板块门控[{market_stage}]：{market_reason}")

    targets = entry_quality.target_evidence(row, price, float(bars5[-1]['close']) if bars5 else None,
                                           levels.get('nearest_resistance'))
    levels['nearest_resistance'] = targets['target']
    if setup_15m != 'WAIT' and targets['unconfirmed_reclaims']:
        blockers.append('旧压力尚未由闭合5分钟突破，不能跳到更远目标')
    confirmation = {}
    confirmation_reason = None
    if setup_15m != 'WAIT' and bars5:
        trigger_level = weak_repair.get('breakout_level') if weak_repair['required'] else None
        if opening_reversal_route or (opening_hold_route and leader_opening_hold.get('entry_variant') == 'SMALL_GAP_RECLAIM'):
            trigger_level = float(bars5[-2]['high']) if len(bars5) >= 2 else None
        if setup_15m == 'E5_LEADER_SECOND_LEG':
            trigger_level = (leader_second_leg.get('breakout_evidence') or {}).get('level')
        floor = max(float(bars5[-1]['low']), float(method_detail.get('anchor') or 0))
        confirmation = entry_quality.confirmation_guard(bars5[-1], now, floor, trigger_level)
        confirmation_reason = entry_quality.guard_reason(confirmation, price, now)
        if confirmation_reason:
            blockers.append(confirmation_reason)
    resistance = levels.get("nearest_resistance")
    invalidation = levels.get("entry_invalidation") or levels.get("structural_invalidation")
    entry_reference = price
    risk_atr = float(method_detail.get('daily_atr14') or atr5) if daily_method else atr5
    room_atr = (float(resistance) - entry_reference) / max(risk_atr, 0.01) if resistance else None
    reward_risk = (
        (float(resistance) - entry_reference) / (entry_reference - float(invalidation))
        if resistance and invalidation and entry_reference > float(invalidation)
        else None
    )
    rr_cfg = config.get("room_risk") or {}
    minimum_rr = float(rr_cfg.get("min_reward_risk", 1.5))
    rr_max_entry = risk_reward_price_ceiling(resistance, invalidation, minimum_rr)
    min_room_atr = float(method_cfg.get('min_room_atr', rr_cfg.get('min_room_atr', 1.0))) if daily_method else float(rr_cfg.get('min_room_atr', 1.0))
    if setup_15m != "WAIT" and (resistance is None or invalidation is None):
        blockers.append("动态阻力或入场失效位不完整，无法计算Room/RR")
    elif setup_15m != "WAIT" and not float(resistance) > price > float(invalidation) > 0:
        blockers.append("目标/现价/失效位顺序无效，不能以缺失RR放行")
    if setup_15m != "WAIT" and room_atr is not None and room_atr < min_room_atr:
        blockers.append(f"上方空间不足：RoomATR {room_atr:.2f}")
    if setup_15m != "WAIT" and reward_risk is not None and reward_risk < minimum_rr:
        blockers.append(f"结构盈亏比不足：RR {reward_risk:.2f} < {minimum_rr:.2f}；当前目标/失效位下参考价格上限{rr_max_entry}，须重新确认买点，不是挂单指令")

    no_entry_after = datetime.strptime(config["time"]["no_new_entry_after"], "%H:%M").time()
    if now.time() >= no_entry_after:
        blockers.append(f"{no_entry_after:%H:%M}后不新开仓")
    blockers = list(dict.fromkeys(str(item) for item in blockers if str(item).strip()))
    # setup_15m is intentionally human-readable while entry_pattern retains
    # the externally stable V2 scenario name.
    entry_allowed = contract_ready and data_ready and not hard_block and setup_15m != "WAIT" and not blockers
    candidate_entry_pattern = f"V2_{setup_15m}" if setup_15m != "WAIT" else None
    trigger_missed_reason = next((item for item in blockers if "后不新开仓" in item), None)
    only_time_blocked = bool(contract_ready and data_ready and not hard_block
                             and candidate_entry_pattern and len(blockers) == 1 and trigger_missed_reason)
    band_atr = float(rr_cfg.get("execution_band_atr", 0.1))
    execution_band_low = max(0.0, price - atr5 * band_atr) if entry_allowed else None
    execution_band_high = price + atr5 * band_atr if entry_allowed else None
    if execution_band_high is not None and rr_max_entry is not None:
        execution_band_high = min(execution_band_high, rr_max_entry)
    if entry_allowed:
        reasons.append(f"三策略时机 {setup_15m}：120m结构、位置、Room/RR与5m执行确认全部通过")
    elif not reasons:
        reasons.append("；".join(blockers[:4]) or "等待多周期确认")
    position = position or {}
    position_action, position_reason, exit_reference = _exit_state(
        price,
        vwap,
        bars15,
        history_120m or [],
        exit_levels,
        path_state,
        extension_state,
        int(position.get("sellable") or 0),
        bars5=bars5,
        intraday_bars15=intraday_bars15,
        now=now,
        config=config,
    )
    effective_extension_state = (
        "CONTROLLED_DAILY_ANCHOR" if daily_method and method_detail.get('eligible')
        else "CONTROLLED_OPENING_REVERSAL" if opening_route
        else "CONTROLLED_SECOND_LEG" if leader_route
        else "CONTROLLED_BASE_BREAKOUT" if breakout_route
        else extension_state
    )
    # Research cannot mutate an execution decision or prevent position exits.
    try:
        gap_research = strategy_gap_research.capture_evaluate(
            row, closed_bars(minute_rows, 1, now), bars5, now, regime, market_data,
        )
    except Exception as exc:
        gap_research = {'mode': 'SHADOW_ONLY', 'entry_allowed': False,
                        'order_action': 'NO_ORDER', 'capture_error': type(exc).__name__}
    return {
        "contract_readiness": readiness,
        "gap_research": gap_research,
        "version": config.get("strategy_version", "three_method_strategy_v1"),
        "config_hash": config_hash(config),
        "mode": mode,
        "method_entry": method_detail,
        "entry_quality": {'leader_identity': leadership, 'weak_repair': weak_repair,
                          'target_evidence': targets, 'confirmation': confirmation,
                          'confirmation_valid': bool(confirmation and not confirmation_reason),
                          'confirmation_reason': confirmation_reason,
                          'near_vwap_obstacle': vwap if vwap and vwap > price else None},
        "confirmation_guard": confirmation,
        "timing_replay_input": timing_replay.capture(
            row, minute_rows, now, history_120m, history_15m, config, market_data, position,
            {'entry_allowed': entry_allowed, 'candidate_entry_pattern': candidate_entry_pattern,
             'blockers': blockers, 'target': resistance, 'invalidation': invalidation},
        ) if candidate_entry_pattern else {},
        "method_replay_input": method_replay.capture(
            strategy_contract, bars5, price, now, config.get('daily_method_entry') or {}, method_detail,
        ) if daily_method and strategy_contract.get('daily_qualified') is True else {},
        "repair_shadow": repair_observation.evaluate(strategy_contract, bars5, price, now, row, regime, config),
        "trigger_timeframe": '5m_closed_at_daily_anchor' if daily_method else '15m_setup_5m_confirmation',
        "risk_timeframe": '1d' if daily_method else 'intraday',
        "entry_allowed": entry_allowed,
        "action": "BUY_PROBE" if entry_allowed else "WAIT",
        "entry_pattern": candidate_entry_pattern if entry_allowed else None,
        "candidate_entry_pattern": candidate_entry_pattern,
        "trigger_missed": False,
        "time_only_blocked": only_time_blocked,
        "trigger_missed_reason": trigger_missed_reason,
        "position_multiplier": (
            float((config.get("repair_upgrade") or {}).get("probe_multiplier", 0.2))
            if entry_allowed and repair_route
            else float((config.get("flat_base_breakout") or {}).get("probe_multiplier", 0.2))
            if entry_allowed and breakout_route
            else config["position"]["probe_multiplier"] if entry_allowed else 0.0
        ),
        "position_action": position_action,
        "position_reason": position_reason,
        "exit_reference": exit_reference,
        "regime": regime,
        "full_structure": full_detail,
        "full_structure_confirmed": has_full_structure,
        "path_state": path_state,
        "path_hard_block": hard_block,
        "location": location,
        "setup_15m": setup_15m,
        "leader_second_leg": leader_second_leg,
        "leader_opening_reversal": leader_opening_reversal,
        "leader_opening_hold": leader_opening_hold,
        "leader_route_windows": leader_route_windows,
        "repair_upgrade": repair_upgrade,
        "flat_base_breakout": flat_base_breakout,
        "market_gate_override": market_gate_override,
        "execution_5m": execution_state,
        "extension_state": effective_extension_state,
        "data_quality": {
            "required_closed_5m": required_5m,
            "required_closed_15m": required_15m,
            "closed_5m_bars": len(bars5),
            "closed_15m_bars": len(bars15),
            "intraday_closed_15m_bars": len(intraday_bars15),
            "minute_available": bool(minute_rows),
            "market_data": market_data,
        },
        # Realtime bars expose ``close_time`` while persisted provider-native
        # 15m history uses ``bar_end``.  This audit field must never turn a
        # data-shape difference into a live-engine failure.
        "source_bar_close": {
            "5m": _bar_close_time(bars5[-1]) if bars5 else None,
            "15m": _bar_close_time(bars15[-1]) if bars15 else None,
        },
        "levels": levels,
        "room_risk": {
            "minimum_reward_risk": minimum_rr,
            "rr_max_entry_price": rr_max_entry,
            "rr_basis": "nearest_structural_target_vs_method_invalidation_before_costs",
            "min_room_atr": min_room_atr,
            "entry_reference": round(entry_reference, 6),
            "room_atr": round(room_atr, 3) if room_atr is not None else None,
            "reward_risk": round(reward_risk, 3) if reward_risk is not None else None,
            "execution_band_low": round(execution_band_low, 6) if execution_band_low is not None else None,
            "execution_band_high": round(execution_band_high, 6) if execution_band_high is not None else None,
        },
        "metrics": {
            "atr5": round(atr5, 6),
            "gap_pct": round(gap_pct, 4),
            "high_to_now_atr": round(high_to_now_atr, 3),
            "clv": round(clv, 3),
            "rvol_1m": round(rvol_1m, 3),
            "rvol_5m": round(rvol, 3),
            "vwap_distance_atr": round(vwap_distance_atr, 3) if vwap_distance_atr is not None else None,
            "ma20_extension_atr": round(extension, 3),
        },
        "execution_gates": {
            "volume_1m_ok": volume_1m_ok,
            "volume_5m_ok": volume_5m_ok,
            "volume_floors_ok": volume_floors_ok,
            "volume_confirmed": volume_confirmed,
            "volume_confirmation_mode": volume_confirmation_mode,
            "vwap_distance_ok": vwap_distance_ok,
            "min_amount_ratio_1m": min_rvol_1m,
            "min_amount_ratio_5m": min_rvol_5m,
            "min_amount_ratio_1m_floor": floor_rvol_1m,
            "min_amount_ratio_5m_floor": floor_rvol_5m,
            "max_vwap_distance_atr": max_vwap_distance_atr,
            "vwap_distance_period": vwap_distance_period,
            "vwap_distance_atr_value": distance_atr,
            "higher_low_required_bars": 2 if daily_method else 3,
            "risk_atr_period": '1d' if daily_method else '5m',
            "no_new_entry_after": config['time']['no_new_entry_after'],
            "leader_max_vwap_distance_pct": float(leader_cfg.get("max_vwap_distance_pct", 3.0)),
        },
        "reasons": reasons,
        "blockers": blockers,
        "blocker_diagnostics": _blocker_diagnostics(blockers),
    }


def gate_reason(decision: dict[str, Any]) -> str:
    return "；".join(str(item) for item in (decision.get("blockers") or decision.get("reasons") or [])[:4]) or "V2多周期确认未完成"
