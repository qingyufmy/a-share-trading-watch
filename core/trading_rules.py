from dataclasses import asdict, dataclass
from datetime import date, datetime, time as dtime
import math
from typing import Any, Dict, Optional


TICK = 0.01
RULE_2026_EFFECTIVE = date(2026, 7, 6)


@dataclass
class TradingRuleContext:
    symbol: str
    exchange: str
    board_type: str
    rule_version: str
    rule_effective_date: str
    session_type: str
    is_continuous_auction: bool
    allow_attack_buy: bool
    allow_risk_sell_alert: bool
    allow_market_order_sim: bool
    price_tick: float
    buy_board_lot: int
    sell_odd_lot_allowed: bool
    limit_up: Optional[float]
    limit_down: Optional[float]
    limit_status: str
    valid_buy_price_high: Optional[float]
    valid_sell_price_low: Optional[float]

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def infer_exchange(symbol: str) -> str:
    return "SSE" if str(symbol).startswith("6") else "SZSE"


def infer_board_type(symbol: str) -> str:
    symbol = str(symbol)
    if symbol.startswith("688"):
        return "STAR"
    if symbol.startswith(("300", "301")):
        return "CHINEXT"
    if symbol.startswith(("8", "4")):
        return "BSE"
    return "MAIN"


def rule_version_for(day: date) -> str:
    return "CN_A_2026_0706" if day >= RULE_2026_EFFECTIVE else "CN_A_2023"


def limit_rate(symbol: str, name: str = "") -> float:
    text = f"{symbol} {name}".upper()
    if "ST" in text:
        return 0.05
    if infer_board_type(symbol) in ("STAR", "CHINEXT"):
        return 0.20
    if infer_board_type(symbol) == "BSE":
        return 0.30
    return 0.10


def round_tick(value: float) -> float:
    return round(float(value) / TICK) * TICK


def ceil_tick(value: float) -> float:
    return math.ceil(float(value) / TICK) * TICK


def floor_tick(value: float) -> float:
    return math.floor(float(value) / TICK) * TICK


def calc_limit_prices(symbol: str, name: str, prev_close: Optional[float]):
    if not prev_close:
        return None, None
    rate = limit_rate(symbol, name)
    return round_tick(float(prev_close) * (1 + rate)), round_tick(float(prev_close) * (1 - rate))


def session_type(now: datetime) -> str:
    t = now.time()
    if dtime(9, 15) <= t < dtime(9, 25):
        return "PRE_OPEN"
    if dtime(9, 30) <= t < dtime(11, 30) or dtime(13, 0) <= t < dtime(14, 57):
        return "CONTINUOUS"
    if dtime(11, 30) <= t < dtime(13, 0):
        return "LUNCH"
    if dtime(14, 57) <= t < dtime(15, 0):
        return "CLOSING_AUCTION_RISK_ONLY"
    return "CLOSED"


def limit_status(price: Optional[float], limit_up: Optional[float], limit_down: Optional[float]) -> str:
    if price is None:
        return "UNKNOWN"
    if limit_up and price >= limit_up - TICK:
        return "LIMIT_UP_LOCKED"
    if limit_down and price <= limit_down + TICK:
        return "LIMIT_DOWN_LOCKED"
    if limit_up and price >= limit_up * 0.995:
        return "NEAR_UP"
    if limit_down and price <= limit_down * 1.005:
        return "NEAR_DOWN"
    return "NONE"


def context_from_signal(signal: Dict[str, Any], now: datetime) -> TradingRuleContext:
    symbol = str(signal.get("symbol") or "")
    name = str(signal.get("name") or "")
    current = to_float(signal.get("current_price"))
    prev_close = to_float(signal.get("prev_close") or signal.get("previous_close"))
    limit_up = to_float(signal.get("limit_up"))
    limit_down = to_float(signal.get("limit_down"))
    if limit_up is None or limit_down is None:
        limit_up, limit_down = calc_limit_prices(symbol, name, prev_close)
    sess = session_type(now)
    continuous = sess == "CONTINUOUS"
    return TradingRuleContext(
        symbol=symbol,
        exchange=infer_exchange(symbol),
        board_type=infer_board_type(symbol),
        rule_version=rule_version_for(now.date()),
        rule_effective_date=RULE_2026_EFFECTIVE.isoformat(),
        session_type=sess,
        is_continuous_auction=continuous,
        allow_attack_buy=continuous,
        allow_risk_sell_alert=sess in ("CONTINUOUS", "CLOSING_AUCTION_RISK_ONLY"),
        allow_market_order_sim=continuous,
        price_tick=TICK,
        buy_board_lot=100,
        sell_odd_lot_allowed=True,
        limit_up=limit_up,
        limit_down=limit_down,
        limit_status=limit_status(current, limit_up, limit_down),
        valid_buy_price_high=ceil_tick(current * 1.02) if current and continuous else None,
        valid_sell_price_low=floor_tick(current * 0.98) if current and continuous else None,
    )


def to_float(value: Any) -> Optional[float]:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except Exception:
        return None

