from dataclasses import asdict, dataclass
from datetime import datetime
import math
from typing import Any, Dict, Optional

from .signal_contract import SignalContract, from_signal, validate_contract
from .trading_rules import TICK, ceil_tick, context_from_signal, floor_tick
from . import entry_quality


DEFAULT_BUY_QTY = 100
DEFAULT_SELL_QTY = 100
MAX_PARTICIPATION_3M = 0.05


@dataclass
class SimResult:
    status: str
    side: str
    qty: int = 0
    fill_price: Optional[float] = None
    limit_price: Optional[float] = None
    reason: str = ""
    theoretical_price: Optional[float] = None
    slippage: Optional[float] = None
    participation_rate: Optional[float] = None
    contract_hash: str = ""
    rule_context: Optional[Dict[str, Any]] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def simulate_signal(signal: Dict[str, Any], position: Dict[str, Any], now: datetime, buy_qty: int = DEFAULT_BUY_QTY, sell_qty: int = DEFAULT_SELL_QTY) -> SimResult:
    contract = from_signal(signal)
    rules = context_from_signal(signal, now)
    errors = validate_contract(contract)
    if errors and contract.status == "立即处理":
        return SimResult(
            status="REJECTED_CONTRACT",
            side=contract.side,
            reason="信号合同不完整：" + ",".join(errors),
            contract_hash=contract.contract_hash,
            rule_context=rules.to_dict(),
        )

    if contract.side == "NONE":
        return SimResult(
            status="NO_ORDER",
            side="NONE",
            reason=contract.sim_reason or "信号不对应模拟成交场景",
            contract_hash=contract.contract_hash,
            rule_context=rules.to_dict(),
        )

    if not contract.sim_allowed:
        return SimResult(
            status="REJECTED_CONTRACT",
            side=contract.side,
            reason=contract.sim_reason or "执行契约未授权模拟成交",
            contract_hash=contract.contract_hash,
            rule_context=rules.to_dict(),
        )

    if contract.expires_at:
        try:
            if now > datetime.strptime(contract.expires_at, "%Y-%m-%d %H:%M:%S"):
                return SimResult(
                    status="EXPIRED",
                    side=contract.side,
                    reason=f"执行契约已过期：{contract.expires_at}",
                    contract_hash=contract.contract_hash,
                    rule_context=rules.to_dict(),
                )
        except ValueError:
            return reject(contract, rules, "执行契约过期时间无效")

    current = float(contract.current_price or 0)
    if current <= 0:
        return reject(contract, rules, "价格无效")

    if contract.side == "BUY":
        if contract.confirmation_guard:
            reason = entry_quality.guard_reason(contract.confirmation_guard, current, now)
            if reason:
                return reject(contract, rules, reason)
        return simulate_buy(signal, contract, rules, current, buy_qty)
    return simulate_sell(signal, contract, rules, current, position, sell_qty)


def simulate_buy(signal, contract: SignalContract, rules, current: float, buy_qty: int) -> SimResult:
    if not rules.allow_attack_buy:
        return reject(contract, rules, "当前不是连续竞价进攻买入时段")
    if rules.limit_status == "LIMIT_UP_LOCKED":
        return unfilled(contract, rules, "涨停锁死，模拟盘不假设可以买到")

    qty = normalize_buy_qty(buy_qty, rules.buy_board_lot)
    if qty < rules.buy_board_lot:
        return reject(contract, rules, "买入数量不足100股整数倍")

    if contract.exec_low is not None and current < contract.exec_low - TICK:
        return unfilled(contract, rules, "现价未进入加仓执行区")
    if contract.exec_high is not None and current > contract.exec_high:
        return unfilled(contract, rules, "现价已高于加仓执行区上沿，不追价")

    max_fill_qty, participation = max_fill_by_recent_volume(signal, qty, rules.buy_board_lot)
    if max_fill_qty <= 0:
        return unfilled(contract, rules, "最近成交量不足，模拟不成交")

    fill_qty = min(qty, max_fill_qty)
    price, slippage = fill_price(signal, contract, "BUY", current)
    if contract.exec_high is not None and price > contract.exec_high:
        return unfilled(contract, rules, "滑点后价格超过加仓执行上限", theoretical_price=price, slippage=slippage)
    if rules.limit_up is not None and price > rules.limit_up:
        return reject(contract, rules, "买入价格超过涨停价", theoretical_price=price, slippage=slippage)

    return SimResult(
        status="FILLED" if fill_qty == qty else "PARTIAL_FILLED",
        side="BUY",
        qty=fill_qty,
        fill_price=price,
        limit_price=contract.exec_high,
        reason="按执行区、滑点和成交量参与率模拟成交",
        theoretical_price=price,
        slippage=slippage,
        participation_rate=participation,
        contract_hash=contract.contract_hash,
        rule_context=rules.to_dict(),
    )


def simulate_sell(signal, contract: SignalContract, rules, current: float, position: Dict[str, Any], sell_qty: int) -> SimResult:
    if not rules.allow_risk_sell_alert:
        return reject(contract, rules, "当前时段只记录状态，不模拟卖出")
    sellable = int(position.get("sellable") or 0)
    if sellable <= 0:
        return reject(contract, rules, "无可卖数量，T+1限制")
    if rules.limit_status == "LIMIT_DOWN_LOCKED":
        return unfilled(contract, rules, "跌停锁死，模拟盘不假设可以卖出")

    qty = normalize_sell_qty(sellable, sell_qty)
    price, slippage = fill_price(signal, contract, "SELL", current)
    min_accept = contract.exec_low if contract.exec_low is not None else rules.limit_down
    if min_accept is not None and price < min_accept:
        return unfilled(contract, rules, "滑点后价格低于可接受减仓价", theoretical_price=price, slippage=slippage)
    if rules.limit_down is not None and price < rules.limit_down:
        return reject(contract, rules, "卖出价格低于跌停价", theoretical_price=price, slippage=slippage)

    return SimResult(
        status="FILLED",
        side="SELL",
        qty=qty,
        fill_price=price,
        limit_price=min_accept,
        reason="按风险卖出滑点模型成交",
        theoretical_price=price,
        slippage=slippage,
        participation_rate=None,
        contract_hash=contract.contract_hash,
        rule_context=rules.to_dict(),
    )


def fill_price(signal, contract: SignalContract, side: str, current: float):
    spread = max(TICK, current * 0.0003)
    atr1 = float(signal.get("atr1m") or 0)
    amount_ratio = float(signal.get("amount_ratio_1m") or 0)
    bps = 8 if side == "BUY" and amount_ratio >= 1.5 else 12 if side == "BUY" else 10 if amount_ratio >= 1.0 else 16
    volatility_slip = min(current * 0.002, max(atr1 * 0.10, current * bps / 10000))
    slip = max(TICK, 0.5 * spread + volatility_slip)
    if side == "BUY":
        return ceil_tick(current + slip), slip
    return floor_tick(current - slip), slip


def max_fill_by_recent_volume(signal, requested_qty: int, lot: int):
    raw_volume = float(signal.get("last_volume") or 0)
    if raw_volume <= 0:
        return requested_qty, None
    comparable_volume = raw_volume if raw_volume > 10000 else raw_volume * 100
    max_qty = int(comparable_volume * MAX_PARTICIPATION_3M)
    max_qty = max_qty // lot * lot
    if max_qty <= 0:
        return 0, MAX_PARTICIPATION_3M
    return min(requested_qty, max_qty), MAX_PARTICIPATION_3M


def normalize_buy_qty(qty: int, lot: int) -> int:
    qty = int(qty or DEFAULT_BUY_QTY)
    return max(lot, qty // lot * lot)


def normalize_sell_qty(sellable: int, qty: int) -> int:
    qty = int(qty or DEFAULT_SELL_QTY)
    if sellable < 100:
        return sellable
    return max(0, min(sellable, qty // 100 * 100 or 100))


def reject(contract, rules, reason, theoretical_price=None, slippage=None):
    return SimResult(
        status="REJECTED",
        side=contract.side,
        reason=reason,
        theoretical_price=theoretical_price,
        slippage=slippage,
        contract_hash=contract.contract_hash,
        rule_context=rules.to_dict(),
    )


def unfilled(contract, rules, reason, theoretical_price=None, slippage=None):
    return SimResult(
        status="UNFILLED",
        side=contract.side,
        reason=reason,
        theoretical_price=theoretical_price,
        slippage=slippage,
        contract_hash=contract.contract_hash,
        rule_context=rules.to_dict(),
    )
