"""Strategy discipline checks for simulated A-share orders.

The checks encode the user's trading framework into an auditable order note:
emotion, chips, time, three timeframes, follow-major-trend/reverse-minor-swing,
price-volume confirmation, and risk boundary. They are deliberately stricter
for BUY orders than SELL orders because risk controls must remain faster than
attack signals.
"""

from __future__ import annotations

import os
from typing import Any

from core import cycle_framework
from core import observation_strategy_router


BUY_SCENARIOS = set(observation_strategy_router.STRATEGY_ENTRY_SCENARIO_SET)
SELL_SCENARIOS = {
    "V2_REDUCE",
    "V2_TAKE_PROFIT",
    "V2_STRUCTURAL_EXIT",
}
FRAMEWORK_VERSION = observation_strategy_router.STRATEGY_CONTRACT_VERSION
# Leave room for spread, slippage, and a normal intraday retest.
DEFAULT_BUY_MIN_RISK_REWARD = 1.50
# 全市场机会不是自选持仓的替代品，必须留出更大的结构性安全垫。
# 1.80 只适合作为观察线；模拟成交要求额外留出价格波动和滑点缓冲。
DEFAULT_MARKET_BUY_MIN_RISK_REWARD = 2.25
DEFAULT_BUY_MIN_CONFIRMATION_SAMPLES = 3


def _f2(value: Any) -> str:
    try:
        return f"{float(value):.2f}"
    except Exception:
        return "-"


def _has_any(text: str, words: tuple[str, ...]) -> bool:
    return any(word in text for word in words)


def _as_float(value: Any, default=None):
    try:
        return float(value)
    except Exception:
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name) or default)
    except Exception:
        return float(default)


def _buy_entry_price(signal: dict[str, Any]) -> float | None:
    for key in ("execution_band_high", "trigger_price", "current_price"):
        value = _as_float(signal.get(key))
        if value is not None and value > 0:
            return value
    return None


def _nearest_pressure(signal: dict[str, Any], entry: float | None) -> float | None:
    if entry is None:
        return None
    candidates = []
    for key in (
        "pressure_price",
        "trend_pressure",
        "target_price",
        "pressure",
        "resistance",
        "h60",
        "h30",
        "or15_high",
        "or5_high",
        "prev_high",
        "limit_up",
    ):
        value = _as_float(signal.get(key))
        if value is not None and value > entry:
            candidates.append(value)
    return min(candidates) if candidates else None


def _buy_min_risk_reward(signal: dict[str, Any]) -> float:
    if signal.get("scenario") == "MARKET_OPPORTUNITY_ACTIONABLE":
        return _env_float("A_SHARE_MARKET_BUY_MIN_RISK_REWARD", DEFAULT_MARKET_BUY_MIN_RISK_REWARD)
    return _env_float("A_SHARE_BUY_MIN_RISK_REWARD", DEFAULT_BUY_MIN_RISK_REWARD)


def _buy_risk_reward(signal: dict[str, Any]) -> tuple[bool, str, float | None]:
    timing = signal.get("timing_v2") or {}
    if signal.get("scenario") in BUY_SCENARIOS and isinstance(timing, dict):
        room = timing.get("room_risk") or {}
        levels = timing.get("levels") or {}
        contract = signal.get('signal_contract') or {}
        entry = _as_float(room.get("entry_reference")) or _buy_entry_price(signal)
        invalid = (_as_float(contract.get('invalid_price')) or _as_float(levels.get('entry_invalidation'))
                   or _as_float(signal.get('invalid_price')) or _as_float(levels.get('structural_invalidation')))
        pressure = _as_float(contract.get('target_price')) or _as_float(levels.get("nearest_resistance")) or _as_float(signal.get("nearest_resistance")) or _nearest_pressure(signal, entry)
        net_ratio = _as_float(contract.get('net_reward_risk'))
        ratio = net_ratio if net_ratio is not None else _as_float(room.get("reward_risk"))
        min_rr = _buy_min_risk_reward(signal)
        if entry is None or invalid is None or invalid <= 0 or ratio is None:
            return False, f"V2动态Room/RR不完整：入场 {_f2(entry)}，失效 {_f2(invalid)}，RR {ratio if ratio is not None else '-'}", pressure
        if entry <= invalid:
            return False, f"V2结构失效位必须低于执行参考价：入场 {_f2(entry)}，失效 {_f2(invalid)}", pressure
        return (
            ratio >= min_rr,
            f"V2动态位：信号参考 {_f2(entry)}，入场失效 {_f2(invalid)}，第一目标阻力 {_f2(pressure)}，"
            f"{'执行契约成本后RR' if net_ratio is not None else '成本前结构RR'} {ratio:.2f}，门槛 {min_rr:.2f}",
            pressure,
        )
    entry = _buy_entry_price(signal)
    invalid = _as_float(signal.get("invalid_price"))
    pressure = _nearest_pressure(signal, entry)
    min_rr = _buy_min_risk_reward(signal)
    if entry is None or invalid is None or invalid <= 0:
        return False, f"入场价/失效价不完整：入场 {_f2(entry)}，失效 {_f2(invalid)}", None
    risk = entry - invalid
    if risk <= 0:
        return False, f"失效价必须低于买入执行价：入场 {_f2(entry)}，失效 {_f2(invalid)}", pressure
    if pressure is None:
        return True, f"入场 {_f2(entry)}，失效 {_f2(invalid)}；未识别明确上方压力，按警告纳入盘后复盘", None
    reward = pressure - entry
    ratio = reward / risk if risk > 0 else 0.0
    ok = ratio >= min_rr
    return (
        ok,
        f"入场 {_f2(entry)}，失效 {_f2(invalid)}，上方压力 {_f2(pressure)}，盈亏比 {ratio:.2f}，门槛 {min_rr:.2f}",
        pressure,
    )


def _trade_style(signal: dict[str, Any], text: str) -> tuple[str, str]:
    explicit = str(signal.get("framework_trade_style") or "").strip()
    explicit_reason = str(signal.get("framework_trade_style_reason") or "").strip()
    if explicit:
        return explicit, explicit_reason or "信号已标注交易风格"
    haystack = " ".join(
        str(x or "")
        for x in (
            text,
            signal.get("row_state"),
            signal.get("focus"),
            signal.get("axes_text"),
            signal.get("community"),
        )
    )
    emotion_hit = _has_any(haystack, ("涨停", "连板", "情绪", "高开", "炸板", "一致亢奋", "题材热", "接力", "游资"))
    trend_hit = _has_any(haystack, ("趋势", "回踩", "修复", "20日", "60日", "多头", "VWAP", "机构", "主线"))
    if emotion_hit and trend_hit:
        return "题材趋势混合", "同时包含题材/情绪与趋势修复特征，按更严格买入纪律执行"
    if emotion_hit:
        return "情绪/高斜率", "偏短线情绪或高斜率博弈，进攻只看主升确认，不做无量回调幻想"
    if trend_hit:
        return "趋势/波段", "偏趋势框架，重视回踩不破、放量站回和移动风控"
    return "未分类", "信号未能明确区分趋势票或情绪票"


def _buy_confirmation_ok(signal: dict[str, Any], text: str) -> tuple[bool, str]:
    scenario = signal.get("scenario") or ""
    timing = signal.get("timing_v2") or {}
    if scenario in BUY_SCENARIOS:
        pattern = str(timing.get("entry_pattern") or "")
        setup = str(timing.get("setup_15m") or "")
        execution = str(timing.get("execution_5m") or "")
        room = timing.get("room_risk") or {}
        gates = timing.get("execution_gates") or {}
        rr = _as_float(room.get("reward_risk"))
        volume_ok = gates.get("volume_confirmed", True)
        vwap_distance_ok = gates.get("vwap_distance_ok", True)
        strategy_contract = signal.get("strategy_contract") or {}
        expected_patterns = {str(item) for item in strategy_contract.get("allowed_patterns") or []}
        pattern_matches = bool(expected_patterns and pattern in expected_patterns)
        opening_pattern = pattern in {
            "V2_E5A_LEADER_OPENING_REVERSAL",
            "V2_E5B_LEADER_OPENING_HOLD",
        }
        execution_ok = execution == "VWAP_RECLAIM" or (opening_pattern and execution == "VWAP_HOLD")
        ok = bool(
            timing.get("entry_allowed")
            and pattern_matches
            and setup != "WAIT"
            and execution_ok
            and rr is not None
            and rr >= DEFAULT_BUY_MIN_RISK_REWARD
            and volume_ok
            and vwap_distance_ok
        )
        return (
            ok,
            f"闭合K线时机确认：模式={pattern or '-'}，策略证据={'匹配' if pattern_matches else '不匹配'}，触发周期={timing.get('trigger_timeframe') or '15m/5m'}，形态={setup or '-'}，5m={execution or '-'}，"
            f"RR={rr if rr is not None else '-'}，量能={'通过' if volume_ok else '未通过'}，"
            f"VWAP距离={'通过' if vwap_distance_ok else '过远'}",
        )
    quality_gate = str(signal.get("signal_quality_gate") or "")
    confirm = str(signal.get("confirm_rule") or "")
    amount_1m = _as_float(signal.get("amount_ratio_1m"), 0) or 0
    amount_5m = _as_float(signal.get("amount_ratio_5m"), 0) or 0
    strict_gate = quality_gate in {
        "vwap_pullback_reclaim",
        "or15_or_repair_reclaim",
        "left_vwap_then_pullback_reclaim",
        "repair_reclaim_volume_not_extended",
    }
    prices = []
    for value in signal.get("last3_prices") or []:
        parsed = _as_float(value)
        if parsed is not None:
            prices.append(parsed)
    required = int(_env_float("A_SHARE_BUY_MIN_CONFIRMATION_SAMPLES", DEFAULT_BUY_MIN_CONFIRMATION_SAMPLES))
    required = max(3, min(required, 5))
    vwap = _as_float(signal.get("vwap"))
    epsilon = _as_float(signal.get("epsilon"), 0) or 0
    if quality_gate in {"vwap_pullback_reclaim", "left_vwap_then_pullback_reclaim"}:
        confirm_line = vwap + epsilon if vwap is not None else None
    else:
        trigger = _as_float(signal.get("trigger_price"))
        confirm_line = trigger - epsilon if trigger is not None else vwap
    recent = prices[-required:]
    consecutive = bool(confirm_line is not None and len(recent) >= required and all(price >= confirm_line for price in recent))
    rising = len(prices) < 2 or prices[-1] >= prices[-2] - max(epsilon, 1e-9)
    samples_detail = f"连续确认 {len(recent)}/{required}，确认线 {_f2(confirm_line)}，末样本 {'向上' if rising else '未向上'}"

    if scenario == "MARKET_OPPORTUNITY_ACTIONABLE":
        ok = bool(vwap) and strict_gate and amount_1m >= 1.30 and amount_5m >= 1.15 and consecutive and rising
        return (
            ok,
            f"全市场机会严格门槛：{quality_gate or '-'}，VWAP={_f2(vwap)}，"
            f"1m/5m量能 {amount_1m:.2f}/{amount_5m:.2f}；{samples_detail}",
        )

    if not strict_gate:
        return False, f"未识别严格小周期形态：{quality_gate or confirm[:80] or '-'}"
    ok = bool(vwap) and amount_5m >= 1.10 and consecutive and rising
    return ok, f"{quality_gate}；5m量能 {amount_5m:.2f}/1.10；{samples_detail}"


def _buy_chase_ok(signal: dict[str, Any]) -> tuple[bool, str]:
    timing = signal.get("timing_v2") or {}
    if signal.get("scenario") in BUY_SCENARIOS and isinstance(timing, dict):
        room = timing.get("room_risk") or {}
        gates = timing.get("execution_gates") or {}
        current = _as_float(signal.get("current_price"))
        contract = signal.get('signal_contract') or {}
        exec_high = _as_float(contract.get('exec_high')) or _as_float(room.get("execution_band_high")) or _as_float(signal.get("execution_band_high"))
        exec_low = _as_float(contract.get('exec_low'))
        in_band = current is not None and exec_high is not None and current <= exec_high + 1e-9
        if exec_low is not None:
            in_band = in_band and current >= exec_low - 1e-9
        vwap_ok = gates.get("vwap_distance_ok", True)
        if not in_band:
            return False, f"三策略执行区外：现价 {_f2(current)}，上沿 {_f2(exec_high)}"
        if not vwap_ok:
            return False, "三策略距VWAP执行门槛未通过"
        return True, f"三策略执行区有效：现价 {_f2(current)}，上沿 {_f2(exec_high)}，未触发VWAP追价阻断"
    current = _as_float(signal.get("current_price"))
    exec_high = _as_float(signal.get("execution_band_high") or signal.get("add_price") or signal.get("trigger_price"))
    if current is None or exec_high is None:
        return False, f"现价 {_f2(current)}，上沿 {_f2(exec_high)}"
    if current > exec_high + 1e-9:
        return False, f"现价 {_f2(current)} 高于执行上沿 {_f2(exec_high)}"
    if signal.get("rapid_rise_blocked"):
        return False, "结构化字段 rapid_rise_blocked=true"
    if signal.get("over_extension_blocked"):
        return False, "结构化字段 over_extension_blocked=true"
    blockers = "；".join(str(x) for x in (signal.get("gate_blockers") or []))
    if _has_any(blockers, ("急拉状态，不追价", "现价离VWAP过远", "过度远离")):
        return False, blockers
    vwap = _as_float(signal.get("vwap"))
    max_chase = _as_float(signal.get("max_chase_distance") or (signal.get("signal_contract") or {}).get("max_chase_distance"))
    if current is not None and vwap is not None and max_chase is not None and current > vwap + max_chase + 1e-9:
        return False, f"距VWAP过远：现价 {_f2(current)} > VWAP {_f2(vwap)} + 最大追价 {_f2(max_chase)}"
    return True, f"现价 {_f2(current)} 在执行区间内，上沿 {_f2(exec_high)}，未触发结构化急拉/远离VWAP阻断"


def _check(name: str, passed: bool, detail: str, severity: str = "required") -> dict[str, Any]:
    return {"name": name, "passed": bool(passed), "detail": detail, "severity": severity}


def signal_text(signal: dict[str, Any]) -> str:
    parts = [
        signal.get("scenario"),
        signal.get("confirm_rule"),
        signal.get("cancel_rule"),
        signal.get("action"),
        signal.get("sim_reason"),
        "；".join(str(x) for x in (signal.get("reasons") or [])),
    ]
    return "；".join(str(x) for x in parts if x)


def build_strategy_rationale(signal: dict[str, Any], side: str, position: dict[str, Any] | None = None) -> dict[str, Any]:
    position = position or {}
    text = signal_text(signal)
    scenario = signal.get("scenario") or ""
    source = "全市场机会池" if signal.get("source") == "market_radar" else "自选/模拟持仓"
    style, style_reason = _trade_style(signal, text)
    rr_ok, rr_detail, pressure = _buy_risk_reward(signal) if side == "BUY" else (True, "-", None)
    contract = signal.get('signal_contract') or {}
    framework = {
        "emotion": "题材/市场环境只作门控和优先级，不单独触发交易；最终以价格量能确认。",
        "chips": f"策略动态结构：执行 {_f2(signal.get('trigger_price'))}，入场失效 {_f2(contract.get('invalid_price') or signal.get('invalid_price'))}，大结构参考 {_f2(signal.get('structural_invalidation'))}，第一目标 {_f2(contract.get('target_price') or signal.get('nearest_resistance'))}。",
        "time": "日线资格使用T-1闭合数据，盘中仅用闭合K线；14:45后不新开仓，普通股票严格执行T+1。",
        "three_timeframes": "趋势方法以日线MA5/MA20为锚点、5m确认；龙头按开盘承接或15m转强/5m突破；120m保留结构风控。",
        "major_trend_minor_swing": "各方法独立计算价格、失效和目标；路径硬否决与追价阻断不能被评分救回。",
        "style": f"{style}：{style_reason}",
        "space": rr_detail,
    }
    if side == "SELL":
        action_basis = [
            "风险减仓优先级高于进攻信号。",
            f"可卖数量 {position.get('sellable', 0)}，A股 T+1 已检查。",
            f"触发原因：{'；'.join((signal.get('reasons') or [])[:4]) or '-'}",
        ]
        if signal.get("open_noise_window"):
            action_basis.append(
                "开盘保护："
                + ("已确认OR/量能破位，允许硬风控。" if signal.get("opening_confirmed") else "未确认硬破位，应先观察。")
            )
    elif side == "BUY":
        action_basis = [
            "仅模拟盘前三策略合同内的首笔试仓，不追价。",
            f"执行区间 {_f2(contract.get('exec_low') or signal.get('execution_band_low') or signal.get('trigger_price'))}-{_f2(contract.get('exec_high') or signal.get('execution_band_high') or signal.get('trigger_price'))}。",
            f"交易风格：{style}；{style_reason}",
            f"空间纪律：{rr_detail}",
            f"确认条件：{signal.get('confirm_rule') or '-'}",
            f"取消条件：{signal.get('cancel_rule') or '-'}",
        ]
    else:
        action_basis = ["该信号不产生买卖成交，只记录计划变化。"]
    return {
        "framework_version": FRAMEWORK_VERSION,
        "source": source,
        "side": side,
        "scenario": scenario,
        "trade_style": style,
        "trade_style_reason": style_reason,
        "pressure_price": pressure,
        "risk_reward_ok": rr_ok,
        "framework": framework,
        "action_basis": action_basis,
        "raw_signal_basis": text,
    }


def discipline_checks(signal: dict[str, Any], side: str, position: dict[str, Any] | None = None, now_session_allows: bool | None = None) -> list[dict[str, Any]]:
    position = position or {}
    text = signal_text(signal)
    scenario = signal.get("scenario") or ""
    external = signal.get("external_status") or ""
    checks: list[dict[str, Any]] = [
        _check("状态必须为立即处理", external == "立即处理", f"当前状态：{external or '-'}"),
        _check("必须有触发价", signal.get("trigger_price") is not None, f"触发价：{_f2(signal.get('trigger_price'))}"),
        _check("必须有失效价", signal.get("invalid_price") is not None, f"失效价：{_f2(signal.get('invalid_price'))}"),
        _check("必须有确认条件", bool(signal.get("confirm_rule")), signal.get("confirm_rule") or "-"),
        _check("必须有取消条件", bool(signal.get("cancel_rule")), signal.get("cancel_rule") or "-"),
    ]
    if now_session_allows is not None:
        checks.append(_check("必须处于允许模拟成交时段", now_session_allows, "连续竞价可模拟；集合竞价/午休/收盘后只记录状态"))

    if side == "BUY":
        contract = signal.get("signal_contract") or {}
        strategy_contract = signal.get("strategy_contract") or {}
        if strategy_contract.get("is_observation_strategy"):
            checks.extend([
                _check(
                    "观察池板块共振必须先通过",
                    bool(signal.get("sector_resonance_ok")),
                    str(signal.get("sector_resonance_reason") or "未取得板块共振审计结果"),
                ),
                _check(
                    "观察池日线策略资格必须通过",
                    bool(signal.get("strategy_daily_qualified")),
                    str(signal.get("strategy_daily_evidence") or "未取得日线策略资格审计结果"),
                ),
            ])
            checks.append(_check(
                "观察池交易策略合同必须匹配",
                bool(signal.get("strategy_gate_ok")),
                str(signal.get("strategy_gate_reason") or "盘前策略路由未提供结果"),
            ))
        if scenario in BUY_SCENARIOS and contract:
            checks.extend([
                _check("策略执行契约必须完整", not signal.get("contract_errors"), ",".join(signal.get("contract_errors") or []) or "契约字段完整"),
                _check("策略执行契约必须授权", bool(contract.get("sim_allowed")), contract.get("sim_reason") or "契约已授权"),
                _check("策略执行契约必须可追溯", bool(contract.get("contract_id") and contract.get("contract_hash")), contract.get("contract_id") or "-"),
            ])
            return checks
        exec_low = signal.get("execution_band_low") or signal.get("add_price") or signal.get("trigger_price")
        exec_high = signal.get("execution_band_high") or signal.get("add_price") or signal.get("trigger_price")
        current = signal.get("current_price")
        amount_ratio = float(signal.get("amount_ratio_1m") or 0)
        confirmation_ok, confirmation_detail = _buy_confirmation_ok(signal, text)
        chase_ok, chase_detail = _buy_chase_ok(signal)
        style, style_detail = _trade_style(signal, text)
        rr_ok, rr_detail, pressure = _buy_risk_reward(signal)
        pressure_severity = "required" if pressure is not None else "warning"
        timing = signal.get("timing_v2") or {}
        timing_check = _check(
            "V2多周期已收盘确认",
            bool(isinstance(timing, dict) and timing and timing.get("entry_allowed")),
            "；".join(str(x) for x in (timing.get("blockers") or timing.get("reasons") or [])[:3]),
        )
        execution_gates = timing.get("execution_gates") or {}
        is_v2 = scenario in BUY_SCENARIOS and isinstance(timing, dict) and bool(timing)
        volume_ok = execution_gates.get("volume_confirmed", amount_ratio >= 1.0) if is_v2 else amount_ratio >= 1.0
        volume_detail = (
            f"V2 1m/5m量能 {timing.get('metrics', {}).get('rvol_1m', amount_ratio)}/"
            f"{timing.get('metrics', {}).get('rvol_5m', signal.get('amount_ratio_5m', '-'))}"
            if is_v2 else f"1m量能比 {amount_ratio:.2f}"
        )
        checks.extend([
            _check("场景必须允许进攻买入", scenario in BUY_SCENARIOS, scenario),
            _check("执行区间必须明确", exec_low is not None and exec_high is not None, f"{_f2(exec_low)}-{_f2(exec_high)}"),
            _check("现价不能高于执行上沿", current is not None and exec_high is not None and float(current) <= float(exec_high) + 1e-9, f"现价 {_f2(current)}，上沿 {_f2(exec_high)}"),
            _check("量能必须确认", volume_ok, volume_detail),
            _check(
                "分钟线必须新鲜",
                signal.get("minute_data_fresh") is not False,
                signal.get("minute_data_fresh_reason") or "分钟线新鲜度未标注",
            ),
            _check("必须是已定义策略入口模式", scenario in BUY_SCENARIOS, scenario),
            _check("必须符合小周期确认", confirmation_ok, confirmation_detail),
            timing_check,
            _check("不得急拉追价", chase_ok, chase_detail),
            _check("交易风格必须可复盘", style != "未分类", f"{style}：{style_detail}", "warning"),
            _check("必须记录上方压力/空间", pressure is not None, rr_detail, pressure_severity),
            _check("已知压力下盈亏比必须达标", rr_ok, rr_detail),
        ])
    elif side == "SELL":
        checks.extend([
            _check("场景必须允许风险卖出", scenario in SELL_SCENARIOS, scenario),
            _check("必须有可卖数量", int(position.get("sellable") or 0) > 0, f"可卖 {position.get('sellable', 0)}"),
            _check("必须有V2退出依据", scenario in SELL_SCENARIOS and bool(signal.get("timing_v2")), text[:180] or "-"),
        ])
    else:
        checks.append(_check("非买卖信号", False, f"side={side}", "blocking"))
    return checks


def build_discipline(signal: dict[str, Any], side: str, position: dict[str, Any] | None = None, now_session_allows: bool | None = None) -> dict[str, Any]:
    checks = discipline_checks(signal, side, position=position, now_session_allows=now_session_allows)
    blocking = [item for item in checks if not item["passed"] and item.get("severity", "required") in ("required", "blocking")]
    return {
        "passed": not blocking,
        "failed_checks": [item["name"] for item in blocking],
        "checks": checks,
        "summary": "纪律通过" if not blocking else "纪律未通过：" + "、".join(item["name"] for item in blocking[:5]),
    }


def order_notes(signal: dict[str, Any], side: str, position: dict[str, Any] | None = None, now_session_allows: bool | None = None) -> tuple[dict[str, Any], dict[str, Any]]:
    return (
        build_strategy_rationale(signal, side, position=position),
        build_discipline(signal, side, position=position, now_session_allows=now_session_allows),
    )
