"""Explainable V2 opportunity rating with a small, explicit hard-veto set."""

from __future__ import annotations

from typing import Any


COMPONENT_MAX = {
    "emotion": 15,
    "chips_location": 20,
    "major_cycle": 20,
    "setup_15m": 15,
    "execution_5m": 10,
    "volume": 10,
    "executability": 10,
}


def _f(value: Any, default: float | None = None) -> float | None:
    try:
        parsed = float(value)
        return parsed if parsed == parsed else default
    except (TypeError, ValueError):
        return default


def _clamp(value: float, maximum: int) -> int:
    return max(0, min(maximum, int(round(value))))


def _grade(score: int, position_risk: bool = False) -> str:
    if position_risk:
        return "R"
    if score >= 80:
        return "A"
    if score >= 70:
        return "B"
    if score >= 55:
        return "C"
    return "D"


def _blocker_category(reason: str) -> str:
    text = str(reason or "")
    if any(token in text for token in ("已收盘5m/15m不足", "分钟线不可用", "历史不足", "交叉校验不一致", "数据降级")):
        return "DATA"
    if any(token in text for token in ("120分钟BEAR", "结构状态 BEAR", "120分钟历史不足", "UNKNOWN")):
        return "MAJOR_CYCLE"
    if any(token in text for token in ("系统性风险", "CORE_GLOBAL_RISK_BLOCKED")):
        return "SYSTEMIC_RISK"
    if any(token in text for token in ("GAP_FAILURE", "DISTRIBUTION_SHOCK")):
        return "PATH_SHOCK"
    if any(token in text for token in ("涨停附近", "锁死涨停", "LIMIT_LOCKED")):
        return "LIMIT_LOCKED"
    if any(token in text for token in ("RoomATR", "盈亏比不足", "RR不足", "结构盈亏比不足")):
        return "NET_RR"
    if any(token in text for token in ("14:45后", "交易时段", "T+1", "100股")):
        return "EXCHANGE_RULE"
    if any(token in text for token in ("盘前仓位计划", "盘前计划=")):
        return "BUDGET"
    if any(token in text for token in ("板块", "行业暂缺", "题材")):
        return "EMOTION"
    if any(token in text for token in ("MID_AIR", "延伸", "CLIMAX", "追价", "位置")):
        return "LOCATION"
    if "15分钟" in text or "Setup" in text:
        return "SETUP_15M"
    if "5分钟" in text or "VWAP" in text or "抬高低点" in text:
        return "EXECUTION_5M"
    if "量能" in text or "放量" in text or "缩量" in text:
        return "VOLUME"
    return "OTHER"


def _data_health(timing: dict[str, Any]) -> tuple[bool, list[str]]:
    quality = timing.get("data_quality") or {}
    market = quality.get("market_data") or {}
    blockers = []
    closed5 = int(quality.get("closed_5m_bars") or 0)
    closed15 = int(quality.get("closed_15m_bars") or 0)
    minute_available = bool(quality.get("minute_available"))
    required5 = int(quality.get('required_closed_5m', 3))
    required15 = int(quality.get('required_closed_15m', 20))
    if not minute_available:
        blockers.append("分钟线不可用")
    if closed5 < required5:
        blockers.append(f"已收盘5分钟K线不足：{closed5}/{required5}")
    if closed15 < required15:
        blockers.append(f"已收盘15分钟K线不足：{closed15}/{required15}")
    if market.get("entry_ready") is False:
        for reason in market.get("blockers") or []:
            if _blocker_category(str(reason)) == "DATA":
                blockers.append(str(reason))
    return not blockers, list(dict.fromkeys(blockers))


def rate_signal(signal: dict[str, Any]) -> dict[str, Any]:
    timing = signal.get("timing_v2") or {}
    scenario = str(signal.get("scenario") or "")
    blockers = [str(item) for item in (timing.get("blockers") or signal.get("reasons") or []) if str(item).strip()]
    categories = [_blocker_category(item) for item in blockers]
    data_ok, data_gaps = _data_health(timing)
    regime = str(timing.get("regime") or "UNKNOWN")
    location = str(timing.get("location") or "UNKNOWN")
    setup = str(timing.get("setup_15m") or "WAIT")
    execution = str(timing.get("execution_5m") or "WAIT")
    gates = timing.get("execution_gates") or {}
    room = timing.get("room_risk") or {}
    path = str(timing.get("path_state") or "NORMAL")
    rr = _f(room.get("reward_risk") if room else signal.get("reward_risk"))
    room_atr = _f(room.get("room_atr") if room else signal.get("room_atr"))

    market_gate = (((timing.get("data_quality") or {}).get("market_data") or {}).get("market_gate") or {})
    market_allowed = market_gate.get("allowed") is not False
    sector_rotation = signal.get("sector_rotation") or {}
    sector_confirmed = bool(
        sector_rotation.get("sustained") and sector_rotation.get("leader_healthy")
    )
    emotion = 12 if market_allowed else 3
    if sector_confirmed:
        emotion = 15
    elif "EMOTION" in categories:
        emotion = min(emotion, 7)

    location_scores = {
        "TIER1_SUPPORT": 20,
        "MA20_RECLAIM": 18,
        "DAILY_MA5": 20,
        "DAILY_MA20": 18,
        "STRUCTURAL_RECLAIM": 18,
        "BREAKOUT_RETEST": 17,
        "REPAIR_UPGRADE": 17,
        "MID_AIR": 5,
        "EXTENDED": 2,
        "CLIMAX": 0,
    }
    chips_location = location_scores.get(location, 10)
    extension = str(timing.get("extension_state") or "NORMAL")
    if extension == "EXTENDED":
        chips_location = min(chips_location, 6)
    elif extension == "CLIMAX":
        chips_location = 0

    major_cycle = {"TREND": 20, "REPAIR": 13, "BEAR": 0, "UNKNOWN": 0}.get(regime, 5)
    setup_15m = 15 if setup != "WAIT" else (9 if len([x for x in blockers if _blocker_category(x) == "SETUP_15M"]) <= 1 else 5)
    execution_5m = 10 if execution in {"VWAP_RECLAIM", "DAILY_MA5_RECLAIM"} else (6 if execution not in {"WAIT", "FAILURE", ""} else 3)
    volume = 0
    if gates.get("volume_confirmed"):
        volume = 10
    elif gates.get("volume_1m_ok") or gates.get("volume_5m_ok"):
        volume = 6
    elif "VOLUME" not in categories:
        volume = 4

    executability = 2
    if rr is not None and rr >= 1.5:
        executability += 4
    elif rr is not None and rr >= 1.2:
        executability += 2
    if room_atr is not None and room_atr >= 1.0:
        executability += 2
    if gates.get("vwap_distance_ok"):
        executability += 2
    executability = min(10, executability)

    quality = timing.get('entry_quality') or {}
    leadership = quality.get('leader_identity') or {}
    weak_repair = quality.get('weak_repair') or {}
    quality_gaps = []
    if leadership and not leadership.get('confirmed'):
        emotion = min(emotion, 7)
        setup_15m = min(setup_15m, 5)
        quality_gaps.append('本票领导力待确认')
    if weak_repair.get('required'):
        emotion = min(emotion, 10)
        chips_location = min(chips_location, 14)
        if not weak_repair.get('confirmed'):
            setup_15m, execution_5m = min(setup_15m, 5), min(execution_5m, 3)
            quality_gaps.append('弱势修复缺少方向性转强')
    if (quality.get('target_evidence') or {}).get('unconfirmed_reclaims'):
        executability = min(executability, 2)
        quality_gaps.append('目标前压力尚未闭合突破')
    if quality.get('confirmation_reason'):
        execution_5m, setup_15m = 0, min(setup_15m, 5)
        chips_location = min(chips_location, 10)
        quality_gaps.append(str(quality['confirmation_reason']))

    components = {
        "emotion": _clamp(emotion, 15),
        "chips_location": _clamp(chips_location, 20),
        "major_cycle": _clamp(major_cycle, 20),
        "setup_15m": _clamp(setup_15m, 15),
        "execution_5m": _clamp(execution_5m, 10),
        "volume": _clamp(volume, 10),
        "executability": _clamp(executability, 10),
    }
    score = sum(components.values())

    hard_veto: list[dict[str, str]] = []
    if not data_ok:
        hard_veto.extend({"code": "DATA", "reason": reason} for reason in data_gaps)
    if regime in {"BEAR", "UNKNOWN"}:
        hard_veto.append({"code": "MAJOR_CYCLE", "reason": f"120分钟结构={regime}"})
    if path in {"GAP_FAILURE", "DISTRIBUTION_SHOCK"}:
        hard_veto.append({"code": "PATH_SHOCK", "reason": f"路径状态={path}"})
    for reason, category in zip(blockers, categories):
        if category in {"SYSTEMIC_RISK", "LIMIT_LOCKED", "EXCHANGE_RULE"}:
            hard_veto.append({"code": category, "reason": reason})
    if setup != "WAIT" and rr is not None and rr < 1.5:
        hard_veto.append({"code": "NET_RR", "reason": f"成本前结构RR仅{rr:.2f}，尚未达到1.50"})
    hard_veto = list({(item["code"], item["reason"]): item for item in hard_veto}.values())

    soft_gaps = []
    for reason, category in zip(blockers, categories):
        if category not in {item["code"] for item in hard_veto} and category not in {"DATA", "MAJOR_CYCLE", "SYSTEMIC_RISK", "PATH_SHOCK", "LIMIT_LOCKED", "EXCHANGE_RULE", "NET_RR"}:
            soft_gaps.append({"category": category, "reason": reason})
    soft_gaps = list({(item["category"], item["reason"]): item for item in soft_gaps}.values())

    position_risk = scenario in {"V2_REDUCE", "V2_TAKE_PROFIT", "V2_STRUCTURAL_EXIT", "V2_NO_ADD"}
    grade = _grade(score, position_risk=position_risk)
    return {
        "score": score,
        "grade": grade,
        "components": components,
        "component_max": dict(COMPONENT_MAX),
        "hard_veto": hard_veto,
        "hard_vetoed": bool(hard_veto),
        "soft_gaps": soft_gaps,
        "soft_gap_count": len(soft_gaps),
        "data_ready": data_ok,
        "quality_gaps": quality_gaps,
        "score_is_probability": False,
        "execution_ready": bool(timing.get('entry_allowed') and not hard_veto),
        "evidence": {
            "regime": regime,
            "location": location,
            "setup_15m": setup,
            "execution_5m": execution,
            "path_state": path,
            "reward_risk": rr,
            "room_atr": room_atr,
        },
    }
