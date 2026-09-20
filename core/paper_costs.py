"""Prospective paper-only fees. Broker commission is a research assumption."""
import os
from datetime import date
from decimal import Decimal, ROUND_HALF_UP

EFFECTIVE_DATE = "2026-09-11"
VERSION = "paper_cn_equity_cost_v1"


def _setting(name, default, maximum):
    value = Decimal(os.environ.get(name, default))
    if not value.is_finite() or not Decimal(0) <= value <= Decimal(maximum):
        raise ValueError("Invalid paper cost setting: " + name)
    return value


def estimate(symbol, side, qty, price, trading_date):
    day = date.fromisoformat(trading_date)
    if side not in {"BUY", "SELL"} or not isinstance(qty, int) or qty <= 0:
        raise ValueError("Invalid fee side or filled quantity")
    price = Decimal(str(price))
    if not price.is_finite() or price <= 0:
        raise ValueError("Invalid fee fill price")
    if day < date.fromisoformat(EFFECTIVE_DATE):
        return {"model": "legacy_no_cost", "total": 0.0}
    code = str(symbol)
    equity = len(code) == 6 and code.isdigit() and code.startswith(("00", "30", "60", "68", "43", "83", "87", "92"))
    if not equity:
        raise ValueError("Paper cost model supports mainland equities only")
    rate = _setting("A_SHARE_PAPER_COMMISSION_RATE", "0.0003", "0.003")
    minimum = _setting("A_SHARE_PAPER_MIN_COMMISSION", "5", "100")
    transfer_rate = _setting("A_SHARE_PAPER_TRANSFER_RATE", "0.00001", "0.001")
    stamp_rate = _setting("A_SHARE_PAPER_STAMP_RATE", "0.0005", "0.01")
    amount = price * qty
    money = lambda value: value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    commission = money(max(minimum, amount * rate))
    transfer = money(amount * transfer_rate)
    stamp = money(amount * stamp_rate) if side == "SELL" else Decimal(0)
    return {"model": VERSION, "effective_date": EFFECTIVE_DATE,
            "assumption": "Research commission inclusive of handling/regulatory fees; not broker-confirmed",
            "commission_rate": float(rate), "min_commission": float(minimum),
            "transfer_rate": float(transfer_rate), "stamp_rate": float(stamp_rate),
            "commission": float(commission), "transfer": float(transfer), "stamp": float(stamp),
            "total": float(commission + transfer + stamp)}


def entry_check(symbol, qty, price, trading_date, contract):
    """Recheck the contract's minimum RR with the actual proposed fill size."""
    if trading_date < EFFECTIVE_DATE:
        return {"passed": True, "basis": "legacy_contract_cost_proxy"}
    buy = estimate(symbol, 'BUY', qty, price, trading_date)
    try:
        target, invalid = float(contract['target_price']), float(contract['invalid_price'])
        slip = float(contract.get('expected_slippage') or 0)
        if not target > price > invalid > 0 or slip < 0:
            raise ValueError('Invalid levels')
        sell = estimate(symbol, 'SELL', qty, target, trading_date)
        cost = (buy['total'] + sell['total']) / qty + slip
        rr = (target - price - cost) / (price - invalid + cost)
    except (KeyError, TypeError, ValueError):
        return {'passed': False, 'reason': '实际数量成本后RR计算依据缺失'}
    return {'passed': rr >= 1.5, 'net_reward_risk': rr, 'minimum_rr': 1.5,
            'estimated_roundtrip_cost_per_share': cost, 'buy_fees': buy, 'estimated_sell_fees': sell,
            'reason': '实际数量成本后RR通过' if rr >= 1.5 else '实际数量计佣金最低收费后RR不足1.5'}
