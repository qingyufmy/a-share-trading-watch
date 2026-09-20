from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
import hashlib
import json
import math
from typing import Any, Dict, List, Optional, Tuple
from core import entry_quality


ACTION_BY_SCENARIO = {
    # Raw V2 patterns are internal timing evidence. They must never become an
    # order contract without a named three-method strategy owning the entry.
    "V2_E1_TREND_PULLBACK_RECLAIM": ("NONE", "TIMING_EVIDENCE"),
    "V2_E2_STRUCTURAL_SUPPORT_REVERSAL": ("NONE", "TIMING_EVIDENCE"),
    "V2_E3_MA20_STRUCTURAL_RECLAIM": ("NONE", "TIMING_EVIDENCE"),
    "V2_E4_BREAKOUT_RETEST": ("NONE", "TIMING_EVIDENCE"),
    "V2_E5_LEADER_SECOND_LEG": ("NONE", "TIMING_EVIDENCE"),
    "V2_E5A_LEADER_OPENING_REVERSAL": ("NONE", "TIMING_EVIDENCE"),
    "V2_E5B_LEADER_OPENING_HOLD": ("NONE", "TIMING_EVIDENCE"),
    "V2_E6_REPAIR_REGIME_UPGRADE": ("NONE", "TIMING_EVIDENCE"),
    "V2_E7_FLAT_BASE_BREAKOUT": ("NONE", "TIMING_EVIDENCE"),
    "STRATEGY_LEADER_ENTRY": ("BUY", "BUY_PROBE"),
    "STRATEGY_520_ENTRY": ("BUY", "BUY_PROBE"),
    "STRATEGY_MA5_ENTRY": ("BUY", "BUY_PROBE"),
    "V2_REDUCE": ("SELL", "REDUCE"),
    "V2_TAKE_PROFIT": ("SELL", "TAKE_PROFIT"),
    "V2_STRUCTURAL_EXIT": ("SELL", "STRUCTURAL_EXIT"),
    "V2_NO_ADD": ("NONE", "NO_ADD"),
    "V2_WAIT": ("NONE", "WAIT"),
}


@dataclass
class SignalContract:
    trading_date: str
    symbol: str
    name: str
    scenario: str
    priority: str
    status: str
    side: str
    action_code: str
    current_price: float
    trigger_price: Optional[float]
    exec_low: Optional[float]
    exec_high: Optional[float]
    invalid_price: Optional[float]
    confirm_rule: str
    cancel_rule: str
    no_push_reason: Optional[str]
    max_chase_distance: Optional[float]
    stale_after_sec: int
    cooldown_sec: int
    sim_allowed: bool
    sim_reason: Optional[str]
    contract_hash: str = ""
    contract_id: str = ""
    contract_version: str = "three-method-execution-contract-1"
    generated_at: str = ""
    expires_at: str = ""
    target_price: Optional[float] = None
    expected_slippage: Optional[float] = None
    estimated_roundtrip_cost: Optional[float] = None
    net_reward_risk: Optional[float] = None
    rr_price_ceiling: Optional[float] = None
    rr_basis: str = "execution_band_upper_with_slippage_and_cost_proxy"
    position_cap_pct: Optional[float] = None
    opportunity_grade: Optional[str] = None
    opportunity_score: Optional[int] = None
    strategy_evidence: Dict[str, Any] = field(default_factory=dict)
    confirmation_guard: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        payload = asdict(self)
        if not payload.get("contract_hash"):
            payload["contract_hash"] = contract_hash(payload)
        return payload


def contract_hash(payload: Dict[str, Any]) -> str:
    keys = (
        "trading_date",
        "symbol",
        "scenario",
        "priority",
        "status",
        "side",
        "action_code",
        "trigger_price",
        "exec_low",
        "exec_high",
        "invalid_price",
        "target_price",
        "net_reward_risk",
        "expires_at",
        "confirmation_guard",
    )
    compact = {key: payload.get(key) for key in keys}
    text = json.dumps(compact, sort_keys=True, ensure_ascii=False)
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:16]


def infer_side_action(signal: Dict[str, Any]) -> Tuple[str, str]:
    scenario = signal.get("scenario") or ""
    side, action = ACTION_BY_SCENARIO.get(scenario, ("NONE", "WATCH_ONLY"))
    return side, signal.get("execution_action") or action


def cooldown_for(priority: str, status: str) -> int:
    if priority == "P0":
        return 60 if status == "立即处理" else 300
    if priority == "P1":
        return 300
    if priority == "P2":
        return 900
    return 86400


def from_signal(signal: Dict[str, Any]) -> SignalContract:
    side, action = infer_side_action(signal)
    current = float(signal.get("current_price") or 0)
    trigger = signal.get("trigger_price")
    invalid = signal.get("invalid_price")
    exec_low = signal.get("execution_band_low")
    exec_high = signal.get("execution_band_high")

    if side == "BUY":
        exec_low = exec_low if exec_low is not None else signal.get("add_price") or trigger
        exec_high = exec_high if exec_high is not None else signal.get("add_price") or trigger
    elif side == "SELL":
        exec_low = exec_low if exec_low is not None else signal.get("limit_down")
        exec_high = exec_high if exec_high is not None else signal.get("reduce_price") or current

    timing = signal.get("timing_v2") or {}
    rating = signal.get("opportunity_rating") or {}
    generated_at = str(signal.get("updated_at") or datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    stale_after = int(signal.get("stale_after_sec") or 90)
    try:
        expires_at = (datetime.strptime(generated_at, "%Y-%m-%d %H:%M:%S") + timedelta(seconds=stale_after)).strftime("%Y-%m-%d %H:%M:%S")
    except ValueError:
        generated_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        expires_at = (datetime.now() + timedelta(seconds=stale_after)).strftime("%Y-%m-%d %H:%M:%S")

    confirmation = timing.get('confirmation_guard') or {}
    evidence_required = 'entry_evidence' in str(timing.get('version') or '')
    confirmation_error = None
    if side == 'BUY' and (confirmation or evidence_required):
        confirmation_error = entry_quality.guard_reason(confirmation, current, datetime.fromisoformat(generated_at))
        if not confirmation_error:
            expires_at = min(expires_at, confirmation['valid_until'])

    expected_slippage = None
    target = to_float_or_none(signal.get("nearest_resistance") or signal.get("target_price") or signal.get("pressure_price"))
    net_rr = None
    roundtrip_cost = None
    rr_price_ceiling = None
    if side == "BUY" and current > 0 and trigger is not None:
        trigger_value = float(trigger)
        atr5 = max(0.0, float(signal.get("atr5m") or 0))
        spread = max(0.01, float(signal.get("spread") or current * 0.0003))
        atr1 = max(0.0, float(signal.get("atr1m") or 0))
        amount_ratio = float(signal.get("amount_ratio_1m") or 0)
        bps = 8 if amount_ratio >= 1.5 else 12
        model_slip = max(0.01, 0.5 * spread + min(current * 0.002, max(atr1 * 0.10, current * bps / 10000)))
        expected_slippage = _ceil_tick(float(signal.get("p95_slippage") or model_slip * 1.65))
        width = max(0.30 * atr5, 3 * 0.01, 2 * spread, expected_slippage)
        exec_low = min(to_float_or_none(exec_low) or trigger_value, trigger_value - width)
        # Preserve the executable liquidity allowance, but cap its upper edge
        # by the cost-adjusted RR below; width alone is not chase permission.
        exec_high = max(to_float_or_none(exec_high) or trigger_value, trigger_value + width)
        invalid_value = to_float_or_none(invalid)
        limit_up = to_float_or_none(signal.get("limit_up"))
        if invalid_value is not None:
            exec_low = max(exec_low, invalid_value + 0.01)
        if confirmation.get('price_floor') is not None:
            exec_low = max(exec_low, float(confirmation['price_floor']))
        if limit_up is not None:
            exec_high = min(exec_high, limit_up - 0.01)
        if target is not None and invalid_value is not None and target > invalid_value > 0:
            # Invert the same cost proxy used below at the minimum net RR 1.5.
            rr_price_ceiling = _floor_tick(
                (target + 1.5 * invalid_value - 2.5 * target * 0.0013) / (2.5 * 1.0003)
            )
            exec_high = min(exec_high, rr_price_ceiling)
        exec_low, exec_high = _floor_tick(exec_low), _floor_tick(exec_high)
        # exec_high caps the fill price, not the quote before slippage.
        expected_fill = _ceil_tick(max(current + expected_slippage, exec_high))
        if target is not None and invalid_value is not None and target > expected_fill > invalid_value:
            roundtrip_cost = expected_fill * 0.0003 + target * 0.0013
            net_reward = target - expected_fill - roundtrip_cost
            net_risk = expected_fill - invalid_value + roundtrip_cost
            net_rr = math.floor(net_reward / net_risk * 10000) / 10000 if net_risk > 0 else None

    max_chase = None
    if side == "BUY" and exec_high is not None and trigger is not None:
        try:
            max_chase = max(0.0, float(exec_high) - float(trigger))
        except Exception:
            max_chase = None

    timing_allows = bool(isinstance(timing, dict) and timing and timing.get("entry_allowed"))
    rating_ok = side != "BUY" or (
        str(rating.get("grade") or "") in {"A", "B"}
        and not rating.get("hard_vetoed")
    )
    net_rr_ok = side != "BUY" or (net_rr is not None and net_rr >= 1.5)
    sim_allowed = (
        side in ("BUY", "SELL")
        and str(signal.get("external_status") or "") == "立即处理"
        and (side != "BUY" or (timing_allows and rating_ok and net_rr_ok and not confirmation_error))
    )
    sim_reason = signal.get("sim_reason")
    if side == 'BUY' and confirmation_error:
        sim_reason = confirmation_error
    elif side == "BUY" and not rating_ok:
        sim_reason = "机会评级未达到A/B或存在硬否决"
    elif side == "BUY" and not net_rr_ok:
        sim_reason = "缺少可计算的成本后RR" if net_rr is None else f"滑点与交易成本后RR={net_rr:.2f}，低于1.50"
    contract = SignalContract(
        trading_date=str(signal.get("trading_date") or ""),
        symbol=str(signal.get("symbol") or ""),
        name=str(signal.get("name") or signal.get("symbol") or ""),
        scenario=str(signal.get("scenario") or ""),
        priority=str(signal.get("priority") or "P3"),
        status=str(signal.get("external_status") or "观察"),
        side=side,
        action_code=action,
        current_price=current,
        trigger_price=to_float_or_none(trigger),
        exec_low=to_float_or_none(exec_low),
        exec_high=to_float_or_none(exec_high),
        invalid_price=to_float_or_none(invalid),
        confirm_rule=str(signal.get("confirm_rule") or ""),
        cancel_rule=str(signal.get("cancel_rule") or ""),
        no_push_reason=signal.get("no_push_reason"),
        max_chase_distance=max_chase,
        stale_after_sec=stale_after,
        cooldown_sec=int(signal.get("cooldown_sec") or cooldown_for(str(signal.get("priority") or "P3"), str(signal.get("external_status") or "观察"))),
        sim_allowed=sim_allowed,
        sim_reason=sim_reason,
        generated_at=generated_at,
        expires_at=expires_at,
        target_price=target,
        expected_slippage=expected_slippage,
        estimated_roundtrip_cost=roundtrip_cost,
        net_reward_risk=net_rr,
        rr_price_ceiling=rr_price_ceiling,
        position_cap_pct=to_float_or_none(signal.get("planned_v2_probe_position_pct") or signal.get("entry_position_cap_pct")),
        opportunity_grade=rating.get("grade"),
        opportunity_score=int(rating.get("score")) if rating.get("score") is not None else None,
        strategy_evidence={
            "strategy_version": signal.get("strategy_version") or signal.get("timing_v2_version"),
            "strategy_key": signal.get("strategy_key"),
            "strategy_name": signal.get("strategy_name"),
            "strategy_style": signal.get("strategy_style"),
            "strategy_entry_pattern": signal.get("strategy_entry_pattern") or timing.get("entry_pattern"),
            "config_hash": signal.get("timing_v2_config_hash"),
            "source_bar_close": timing.get("source_bar_close"),
            "regime": timing.get("regime"),
            "location": timing.get("location"),
            "setup_15m": timing.get("setup_15m"),
            "execution_5m": timing.get("execution_5m"),
            "rating_components": rating.get("components"),
            "entry_quality": timing.get('entry_quality'),
        },
        confirmation_guard=confirmation,
    )
    contract.contract_hash = contract_hash(asdict(contract))
    contract.contract_id = f"{contract.trading_date}:{contract.symbol}:{contract.contract_hash}"
    return contract


def _ceil_tick(value: float) -> float:
    return round(math.ceil(float(value) / 0.01 - 1e-9) * 0.01, 2)


def _floor_tick(value: float) -> float:
    return round(math.floor(float(value) / 0.01 + 1e-9) * 0.01, 2)


def to_float_or_none(value: Any) -> Optional[float]:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except Exception:
        return None


def validate_contract(contract: SignalContract) -> List[str]:
    errors = []
    if contract.status == "立即处理":
        if not contract.trigger_price:
            errors.append("missing_trigger_price")
        if not contract.invalid_price:
            errors.append("missing_invalid_price")
        if not contract.confirm_rule:
            errors.append("missing_confirm_rule")
        if not contract.cancel_rule:
            errors.append("missing_cancel_rule")
        if contract.side == "NONE":
            errors.append("missing_side")
        if contract.side == "BUY" and (contract.exec_low is None or contract.exec_high is None):
            errors.append("missing_buy_execution_band")
        if contract.side == "BUY" and contract.exec_low is not None and contract.exec_high is not None:
            if contract.exec_low > contract.exec_high:
                errors.append("empty_rr_constrained_execution_band")
            if contract.current_price > contract.exec_high:
                errors.append("current_price_above_rr_execution_band")
        if contract.side == "BUY" and contract.target_price is None:
            errors.append("missing_target_price")
        if contract.side == "BUY" and contract.net_reward_risk is None:
            errors.append("missing_net_reward_risk")
        if contract.side == "BUY" and contract.net_reward_risk is not None and contract.net_reward_risk < 1.5:
            errors.append("net_reward_risk_below_1_5")
        if contract.side == "SELL" and contract.exec_high is None:
            errors.append("missing_sell_execution_price")
    return errors


def attach_contract(signal: Dict[str, Any]) -> Dict[str, Any]:
    contract = from_signal(signal)
    errors = validate_contract(contract)
    patch = {
        "signal_contract": contract.to_dict(),
        "contract_hash": contract.contract_hash,
        "contract_errors": errors,
    }
    if errors and signal.get("external_status") == "立即处理":
        patch["no_push_reason"] = "信号合同不完整：" + ",".join(errors)
    return patch
