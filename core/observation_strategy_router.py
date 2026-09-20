"""Route observation-pool symbols into executable, named trading methods.

The strategy contract owns the order reason and sizing.  The V2 timing engine
is retained as shared evidence infrastructure for closed bars, multi-period
structure, VWAP, volume, room/risk and time-of-day validation.  It must not
silently choose a method for an observation-pool name.
"""

from __future__ import annotations

import re
from statistics import mean
from typing import Any
from core import sector_identity, closed_liquidity


LEADER = "LEADER_EMOTION"
TREND_520 = "TREND_520"
TREND_MA5 = "TREND_MA5"
OBSERVE = "OBSERVE_UNCLASSIFIED"
CORE_V2 = "CORE_V2"
STRATEGY_CONTRACT_VERSION = "three_method_strategy_v1"

# Execution themes are chosen without live returns; broad tags are not a thesis.
MAINLINE_ALIASES = {
    '液冷服务器': ('液冷',), '数字货币': ('数字货币',), 'CPO概念': ('CPO', '光模块'),
    'PCB': ('PCB', '印制电路板'), '存储芯片': ('存储芯片', 'HBM'),
    '商业航天': ('商业航天',), '培育钻石': ('培育钻石',),
    '创新药': ('创新药',), '光纤概念': ('光纤',), '人形机器人': ('人形机器人',),
}
BROAD_BOARDS = {'深圳特区', '华为概念', '网红经济', '并购重组概念', '融资融券', '沪股通', '深股通'}


def planned_resonance(stock):
    identity = sector_identity.resolve(stock, stock.get('board_catalog'))
    return identity['names'], identity['reason']


def cross_status_text(value):
    return {'verified': '已交叉验证', 'stale': '榜单过期，不能验证',
            'unavailable': '榜单缺失，不能验证', 'not_listed': '完整榜单未上榜',
            'not_observed_partial': '仅有部分榜单，尚未观察到'}.get(value, '榜单状态待核对')

STRATEGY_ENTRY_SCENARIOS = {
    LEADER: "STRATEGY_LEADER_ENTRY",
    TREND_520: "STRATEGY_520_ENTRY",
    TREND_MA5: "STRATEGY_MA5_ENTRY",
}
STRATEGY_ENTRY_SCENARIO_SET = set(STRATEGY_ENTRY_SCENARIOS.values())


def contract_readiness(contract):
    if not contract:
        status, reason = 'CONTRACT_MISSING', '缺少盘前策略合同'
    elif contract.get('key') == OBSERVE:
        status, reason = 'OBSERVATION_ONLY', '合同存在，但仅为待分类/修复观察，无新增仓资格'
    elif contract.get('key') not in STRATEGY_ENTRY_SCENARIOS:
        status, reason = 'LEGACY_OR_UNKNOWN', '历史或未知策略合同不允许新增仓'
    elif contract.get('daily_qualified') is not True:
        status, reason = 'DAILY_NOT_QUALIFIED', '策略合同存在，但日线资格尚未通过'
    elif not contract.get('is_observation_strategy') or not contract.get('allowed_patterns'):
        status, reason = 'CONTRACT_INCOMPLETE', '策略合同缺少执行身份或允许形态'
    else:
        status, reason = 'READY', '日线策略合同已就绪'
    return {'status': status, 'ready': status == 'READY', 'reason': reason,
            'daily_gate_reason': contract.get('daily_gate_reason') if contract else None}


FORMAL_ENTRY_SIGNALS = {
    LEADER: "龙头战法正式买入：板块共振后，仅E5B开盘强承接、E5A开盘弱转强或E5二次转强确认后首笔试仓",
    TREND_520: "520战法正式买入：锁定主线共振，日线金叉/MA20回踩资格，日线锚点经5分钟闭合收复后试仓",
    TREND_MA5: "趋势5日线正式买入：锁定主线共振，日线MA5受控回踩经5分钟闭合收复后试仓",
    OBSERVE: "待分类观察：不产生新增仓买入信号",
    CORE_V2: "历史合同已停用：重新分类前只观察，不产生新增仓信号",
}


STRATEGY_META = {
    LEADER: {
        "name": "龙头战法",
        "style": "情绪票",
        "allowed_patterns": [
            "V2_E5B_LEADER_OPENING_HOLD",
            "V2_E5A_LEADER_OPENING_REVERSAL",
            "V2_E5_LEADER_SECOND_LEG",
        ],
        "entry_rule": "先确认当日板块持续、领涨健康及本票在板块样本中的领导力；人气候选身份不能代替本票领涨/领先组证据。可用E5B开盘承接、E5A触板回落后弱转强、E5首波回踩后二次转强。闭合确认后破坏结构即取消；未突破压力不得跳过，120m、VWAP、量能与Room/RR仍需通过。",
        "exit_rule": "不加速追价；高位放量滞涨、跌破短周期结构或V2退出时优先减压。",
    },
    TREND_520: {
        "name": "520战法",
        "style": "趋势票",
        "allowed_patterns": [
            "V2_E1_TREND_PULLBACK_RECLAIM",
            "V2_E3_MA20_STRUCTURAL_RECLAIM",
            "V2_E4_BREAKOUT_RETEST",
        ],
        "entry_rule": "先确认盘前锁定主线的当日共振；日线新近MA5/MA20金叉或MA20回踩收复，量能、MACD、KDJ合格；用日线MA5/MA20锚点及闭合5m收复入场，不再以15m MA20替代。120m不得BEAR、Room/RR、VWAP、量能和仓位风控仍有效。",
        "exit_rule": "MA5下穿MA20，或收盘跌破MA20且反抽无量失败时停止进攻并按V2结构处理。",
    },
    TREND_MA5: {
        "name": "趋势5日线法则",
        "style": "趋势票",
        "allowed_patterns": ["V2_E1_TREND_PULLBACK_RECLAIM", "V2_E4_BREAKOUT_RETEST"],
        "entry_rule": "先确认盘前锁定主线的当日共振；日线MA5>MA10>=MA20且量价证据至少3项只授予方法资格。MA5受控回踩后由独立闭合5m阳线、有效转强及抬低确认，现价不得跌破确认结构。强趋势可用日线锚点替代VWAP收复；修复/反弹失败且低于VWAP时须突破前高、阳线抬低及方向性量能确认。未突破压力、120m、Room/RR、仓位风控仍有效。",
        "exit_rule": "有效跌破MA5且无法收复、放量滞涨或V2结构退出时减仓；连续两至三日未走强需复核。",
    },
    OBSERVE: {
        "name": "待分类观察",
        "style": "未分类",
        "allowed_patterns": [],
        "entry_rule": "缺少情绪龙头或日线趋势方法的完整证据，只做覆盖和复核，不生成新增仓信号。",
        "exit_rule": "不适用。",
    },
    CORE_V2: {
        "name": "历史合同（已停用）",
        "style": "未分类",
        "allowed_patterns": [],
        "entry_rule": "历史开仓合同已停用；必须重新取得龙头、520或趋势5日线资格。",
        "exit_rule": "已有仓位继续执行结构止损、止盈与风险退出。",
    },
}


def _f(value: Any, default: float = 0.0) -> float:
    try:
        value = float(value)
        return value if value == value else default
    except (TypeError, ValueError):
        return default


def _ma(values: list[float], period: int, end: int | None = None) -> float | None:
    sample = values[:end] if end is not None else values
    if len(sample) < period:
        return None
    return mean(sample[-period:])


def _context_text(stock: dict[str, Any]) -> str:
    parts: list[str] = []
    for key in ("news", "notice"):
        for item in (stock.get("ths") or {}).get(key) or []:
            if isinstance(item, dict):
                parts.extend((str(item.get("title") or ""), str(item.get("abstract") or "")))
    parts.extend(str(item) for item in (stock.get("manual_leader_evidence") or []) if item)
    parts.extend(str(item) for item in ((stock.get("emotion_leader_candidate") or {}).get("reasons") or []) if item)
    return " ".join(parts)


def _leader_context_evidence(context_text: str) -> dict[str, bool]:
    """Keep institutional, ordinary-seat and popularity evidence auditable."""
    text = str(context_text or "")
    buy_action = r"(?:净买入?|买入|抢筹)"
    institution = r"(?:\d+家机构|机构(?:专用|席位)?)"
    ordinary = r"普通席位"
    institutional_net_buy = bool(
        re.search(rf"{institution}.{{0,20}}{buy_action}", text)
        or re.search(rf"{buy_action}.{{0,20}}{institution}", text)
    )
    ordinary_seat_net_buy = bool(
        re.search(rf"{ordinary}.{{0,20}}{buy_action}", text)
        or re.search(rf"{buy_action}.{{0,20}}{ordinary}", text)
    )
    return {
        "institutional_net_buy": institutional_net_buy,
        "ordinary_seat_net_buy": ordinary_seat_net_buy,
        "popularity_context": any(token in text for token in ("人气榜", "东财人气", "开盘啦")),
        "leadership_label": any(token in text for token in ("龙头", "核心标的")),
    }


def _float_market_cap_yi(quote: dict[str, Any]) -> float | None:
    """Estimate float market value from price, volume lots and turnover rate."""
    explicit = closed_liquidity.positive(quote.get("float_market_cap_yi")) or 0
    if explicit > 0:
        return explicit
    close = closed_liquidity.positive(quote.get("close")) or 0
    volume_lot = closed_liquidity.positive(quote.get("volume_lot")) or 0
    turnover = closed_liquidity.positive(quote.get("turnover")) or 0
    if close <= 0 or volume_lot <= 0 or turnover <= 0:
        return None
    return close * volume_lot / (turnover * 10000.0)


def _ema_series(values: list[float], period: int) -> list[float]:
    """Return a deterministic EMA series for daily MACD qualification."""
    if not values:
        return []
    alpha = 2.0 / (period + 1.0)
    result = [float(values[0])]
    for value in values[1:]:
        result.append(alpha * float(value) + (1.0 - alpha) * result[-1])
    return result


def _macd_metrics(closes: list[float]) -> dict[str, float | bool | None]:
    if len(closes) < 35:
        return {"dif": None, "dea": None, "hist": None, "hist_prev": None, "bullish": False, "improving": False}
    ema12 = _ema_series(closes, 12)
    ema26 = _ema_series(closes, 26)
    dif_series = [fast - slow for fast, slow in zip(ema12, ema26)]
    dea_series = _ema_series(dif_series, 9)
    hist_series = [dif - dea for dif, dea in zip(dif_series, dea_series)]
    dif, dea, hist = dif_series[-1], dea_series[-1], hist_series[-1]
    hist_prev = hist_series[-2] if len(hist_series) >= 2 else None
    return {
        "dif": round(dif, 5),
        "dea": round(dea, 5),
        "hist": round(hist, 5),
        "hist_prev": round(hist_prev, 5) if hist_prev is not None else None,
        "bullish": bool(dif >= dea and dif >= 0),
        "improving": bool(hist_prev is not None and hist >= hist_prev),
    }


def _kdj_metrics(highs: list[float], lows: list[float], closes: list[float], period: int = 9) -> dict[str, float | bool | None]:
    """Calculate a daily KDJ series from OHLC bars for strategy confirmation.

    KDJ is deliberately a daily qualifier here.  It improves the quality of a
    5/20 setup but cannot create an order by itself; the V2 closed-bar path
    remains the lower-timeframe execution trigger.
    """
    if len(highs) != len(lows) or len(lows) != len(closes) or len(closes) < period + 1:
        return {
            "k": None,
            "d": None,
            "j": None,
            "k_cross_up": False,
            "bullish": False,
            "not_overheated": False,
        }

    k_value = d_value = 50.0
    k_series: list[float] = []
    d_series: list[float] = []
    j_series: list[float] = []
    for index, close in enumerate(closes):
        if index < period - 1:
            rsv = 50.0
        else:
            window_high = max(highs[index - period + 1:index + 1])
            window_low = min(lows[index - period + 1:index + 1])
            rsv = 50.0 if window_high <= window_low else (close - window_low) * 100.0 / (window_high - window_low)
        k_value = (2.0 * k_value + rsv) / 3.0
        d_value = (2.0 * d_value + k_value) / 3.0
        k_series.append(k_value)
        d_series.append(d_value)
        j_series.append(3.0 * k_value - 2.0 * d_value)

    k_value, d_value, j_value = k_series[-1], d_series[-1], j_series[-1]
    prior_k, prior_d = k_series[-2], d_series[-2]
    return {
        "k": round(k_value, 2),
        "d": round(d_value, 2),
        "j": round(j_value, 2),
        "k_cross_up": bool(prior_k <= prior_d and k_value > d_value),
        "bullish": bool(k_value >= d_value),
        # A high KDJ does not invalidate a strong new MA cross automatically,
        # but an extreme J value is not a controlled first-entry location.
        "not_overheated": bool(j_value < 100.0),
    }


def _bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "yes", "y", "通过", "合格"}


def _pct_limit(code: str) -> float:
    return 19.5 if str(code).startswith("3") or str(code).startswith("68") else 9.7


def _recent_520_events(closes: list[float], lows: list[float], lookback: int = 5) -> dict[str, Any]:
    """Find recent MA5/MA20 events without requiring them on T-1 exactly."""
    if len(closes) < 21 or len(lows) != len(closes):
        return {
            "recent_cross": False,
            "cross_days_ago": None,
            "recent_ma20_reclaim": False,
            "reclaim_days_ago": None,
        }

    cross_index = None
    reclaim_index = None
    start = max(20, len(closes) - max(1, lookback) - 1)
    for index in range(start, len(closes)):
        ma5 = _ma(closes, 5, index + 1)
        ma20 = _ma(closes, 20, index + 1)
        prior_ma5 = _ma(closes, 5, index)
        prior_ma20 = _ma(closes, 20, index)
        if not all(value is not None for value in (ma5, ma20, prior_ma5, prior_ma20)):
            continue
        if prior_ma5 <= prior_ma20 and ma5 > ma20:
            cross_index = index
        if (
            lows[index] <= ma20 * 1.015
            and closes[index] >= ma20
            and ma5 > ma20
            and ma5 >= prior_ma5
        ):
            reclaim_index = index

    def days_ago(index: int | None) -> int | None:
        return len(closes) - 1 - index if index is not None else None

    cross_days_ago = days_ago(cross_index)
    reclaim_days_ago = days_ago(reclaim_index)
    return {
        "recent_cross": bool(cross_days_ago is not None and cross_days_ago <= lookback),
        "cross_days_ago": cross_days_ago,
        "recent_ma20_reclaim": bool(reclaim_days_ago is not None and reclaim_days_ago <= lookback),
        "reclaim_days_ago": reclaim_days_ago,
    }


def _contract(key: str, reason: str, metrics: dict[str, Any], is_observation: bool) -> dict[str, Any]:
    meta = STRATEGY_META[key]
    repair = bool(metrics.get('repair_watch'))
    gate_reason = str(metrics.get('gate_reason') or reason)
    if not metrics.get('qualified') and metrics.get('missing_evidence'):
        missing = '；龙头证据缺项（未核验，不等于指标不达标）：' + '、'.join(metrics['missing_evidence'])
        reason += missing
        gate_reason += missing
    return {
        "key": key,
        "name": '趋势修复观察' if repair else meta["name"],
        "style": '趋势票' if repair else meta["style"],
        "formal_entry_signal": '趋势修复仅影子验证，不产生新增仓信号' if repair else formal_entry_signal(key),
        "allowed_patterns": list(meta["allowed_patterns"]),
        "entry_rule": metrics.get('repair_rule') if repair else meta["entry_rule"],
        "exit_rule": meta["exit_rule"],
        "reason": reason,
        "is_observation_strategy": bool(is_observation),
        "version": STRATEGY_CONTRACT_VERSION,
        "daily_metrics": metrics,
        "daily_qualified": bool(metrics.get("qualified")),
        "daily_gate_reason": gate_reason,
    }


def formal_entry_signal(contract_or_key: dict[str, Any] | str | None) -> str:
    """Return the one plain-language event that may create a new order."""
    if isinstance(contract_or_key, dict):
        key = str(contract_or_key.get("key") or OBSERVE)
        if key == LEADER:
            allowed = {str(item) for item in contract_or_key.get("allowed_patterns") or []}
            routes = []
            if "V2_E5B_LEADER_OPENING_HOLD" in allowed:
                routes.append("E5B开盘强承接")
            if "V2_E5A_LEADER_OPENING_REVERSAL" in allowed:
                routes.append("E5A开盘弱转强")
            if "V2_E5_LEADER_SECOND_LEG" in allowed:
                routes.append("E5二次转强")
            if routes:
                return f"龙头战法正式买入：板块共振后，仅{'、'.join(routes)}确认后首笔试仓"
    else:
        key = str(contract_or_key or OBSERVE)
    return FORMAL_ENTRY_SIGNALS.get(key, FORMAL_ENTRY_SIGNALS[OBSERVE])


def daily_evidence_text(contract: dict[str, Any] | None) -> str:
    """Compact, human-readable daily qualification for plan/report handoff."""
    contract = contract or {}
    metrics = contract.get("daily_metrics") or {}
    if not contract.get("is_observation_strategy"):
        return "历史合同未重新分类，禁止新增仓"
    if not contract.get("daily_qualified"):
        return str(contract.get("daily_gate_reason") or "日线策略资格未通过")
    key = str(contract.get("key") or OBSERVE)
    if key == LEADER:
        return (
            f"日线候选通过：近4日涨停{metrics.get('recent_limit_days', 0)}次，"
            f"额比{metrics.get('amount_ratio_5d', 0):.2f}，换手{metrics.get('turnover', 0):.2f}%"
            f"；开盘啦：{cross_status_text(metrics.get('emotion_pool_cross_status'))}"
        )
    if key == TREND_520:
        route = "近5日5-20金叉" if metrics.get("fresh_cross") else "近5日MA20回踩收复"
        return (
            f"日线资格通过：{route}；MA5/20 {metrics.get('ma5', 0):.2f}/{metrics.get('ma20', 0):.2f}，"
            f"额比{metrics.get('amount_ratio_5d', 0):.2f}，MACD多头，"
            f"K/D/J {metrics.get('kdj', {}).get('k', 0):.1f}/"
            f"{metrics.get('kdj', {}).get('d', 0):.1f}/{metrics.get('kdj', {}).get('j', 0):.1f}"
        )
    if key == TREND_MA5:
        distance = metrics.get("ma5_distance_pct")
        location = f"距MA5 {distance:+.2f}%" if isinstance(distance, (int, float)) else "MA5位置待核"
        return (
            f"方法资格通过：MA5/10/20 {metrics.get('ma5', 0):.2f}/{metrics.get('ma10', 0):.2f}/{metrics.get('ma20', 0):.2f}，"
            f"{location}；量价准备度{metrics.get('qualification_count', 0)}/5，入场需至少3项及日线MA5锚点的闭合5分钟收复"
        )
    return str(contract.get("daily_gate_reason") or "未取得可交易策略资格")


def _ma5_qualification(closes, lows, amounts, pct, amount_ratio, macd):
    ma5, ma10, ma20, ma60 = (_ma(closes, n) for n in (5, 10, 20, 60))
    prior_ma5, prior_ma10, prior_ma20 = (_ma(closes, n, -1) for n in (5, 10, 20))
    close = closes[-1]
    touch_indices = []
    for index in range(max(4, len(lows) - 10), len(lows)):
        line = _ma(closes, 5, index + 1)
        if line and lows[index] <= line * 1.002:
            touch_indices.append(index)
    latest_touch = touch_indices[-1] if touch_indices else None
    first_pullback = bool(latest_touch is not None and latest_touch >= len(lows) - 4 and len(touch_indices) == 1)
    touch_amount = amounts[latest_touch] if latest_touch is not None and latest_touch < len(amounts) else 0.0
    before_touch = amounts[max(0, (latest_touch or 0) - 5):(latest_touch or 0)]
    pullback_shrunk = bool(before_touch and touch_amount <= mean(before_touch))
    medium_trend_ok = bool(ma60 is None or close >= ma60)
    ma_alignment = bool(ma5 and ma10 and ma20 and prior_ma5 and prior_ma10 and prior_ma20
                        and ma5 > ma10 >= ma20 and close >= ma20 and medium_trend_ok
                        and ma5 > prior_ma5 and ma20 >= prior_ma20)
    rebound_day = bool(close >= ma5 and pct > 0 and amount_ratio >= 1.0)
    macd_recovering = bool(macd.get('bullish') or macd.get('improving'))
    count = sum((ma_alignment, first_pullback, pullback_shrunk, rebound_day, macd_recovering))
    return dict(ma_alignment=ma_alignment, medium_trend_ok=medium_trend_ok,
                first_pullback=first_pullback, pullback_shrunk=pullback_shrunk,
                rebound_day=rebound_day, macd_recovering=macd_recovering,
                qualification_count=count, ma5_distance_pct=round((close / ma5 - 1) * 100, 3),
                ma5_method_qualified=bool(ma_alignment and close >= ma5 * 0.97),
                setup_ready=bool(first_pullback and count >= 3))


def classify_stock(stock: dict[str, Any]) -> dict[str, Any]:
    """Return the only strategy path eligible for a planned observation name.

    A涨停或均线多头 alone is insufficient.  "龙头" remains a candidate until
    V2 later proves sector persistence and a controlled second leg.  Likewise
    a trend path needs explicit MA evidence rather than a vague trend label.
    """
    quote = stock.get("quote") or {}
    daily = list(stock.get("daily") or [])
    tech = stock.get("tech") or {}
    # Every executable watchlist name must obtain one named method contract.
    # Pool membership controls reporting provenance, not permission to bypass
    # the strategy router through the legacy CORE_V2 lane.
    is_observation = True

    ohlc = [
        item for item in daily
        if _f(item.get("close")) > 0 and _f(item.get("low")) > 0 and _f(item.get("high")) > 0
    ]
    closes = [_f(item.get("close")) for item in ohlc]
    lows = [_f(item.get("low")) for item in ohlc]
    highs = [_f(item.get("high")) for item in ohlc]
    amounts = [_f(item.get("amount_wan")) for item in ohlc]
    # Strategy qualification is a daily-bar decision.  Premarket and auction
    # quotes are partial by definition and must never replace the latest
    # completed daily close, amount or percentage change.
    latest_daily = ohlc[-1] if ohlc else {}
    close = _f(latest_daily.get("close"))
    code = str(quote.get("code") or "")
    pct = _f(latest_daily.get("pct"))
    if not close or len(closes) < 35:
        return _contract(
            OBSERVE,
            "日线样本不足35根，无法核验MA5/10/20、MACD、KDJ与回踩量能，不把缺数据当成策略资格。",
            {"qualified": False, "gate_reason": "日线样本不足35根"},
            True,
        )

    ma5 = _ma(closes, 5)
    ma10 = _ma(closes, 10)
    ma20 = _ma(closes, 20)
    ma60 = _ma(closes, 60)
    prior_ma5 = _ma(closes, 5, -1)
    prior_ma20 = _ma(closes, 20, -1)
    ma5_two_days_ago = _ma(closes, 5, -2)
    prior_ma10 = _ma(closes, 10, -1)
    prior_amounts = [value for value in amounts[-6:-1] if value > 0]
    avg_amount5 = mean(prior_amounts) if prior_amounts else 0.0
    completed_amount = amounts[-1] if amounts else 0.0
    amount_ratio = completed_amount / avg_amount5 if avg_amount5 else 0.0
    latest_low = lows[-1] if lows else close
    prior_low = lows[-2] if len(lows) >= 2 else latest_low
    recent_limit_days = sum(1 for item in daily[-4:] if _f(item.get("pct")) >= _pct_limit(code))
    concepts = " ".join(str(quote.get(key) or "") for key in ("industry", "concepts", "concept"))
    context_text = _context_text(stock)
    macd = _macd_metrics(closes)
    kdj = _kdj_metrics(highs, lows, closes)
    completed_quote = quote if closed_liquidity.matching_close(latest_daily, quote) else {}
    turnover = closed_liquidity.positive(latest_daily.get("turnover")) or closed_liquidity.positive(completed_quote.get("turnover")) or 0
    amount_yi = completed_amount / 10000.0
    float_market_cap_yi = _float_market_cap_yi(latest_daily) or _float_market_cap_yi(completed_quote)
    external_leader = stock.get("emotion_leader_candidate") or {}
    resonance_boards, resonance_source = planned_resonance(stock)
    metrics = {
        "close": round(close, 3),
        "pct": round(pct, 3),
        "ma5": round(ma5, 3) if ma5 else None,
        "ma10": round(ma10, 3) if ma10 else None,
        "ma20": round(ma20, 3) if ma20 else None,
        "ma60": round(ma60, 3) if ma60 else None,
        "amount_ratio_5d": round(amount_ratio, 3),
        "amount_ratio_source": "last_completed_daily_bar",
        "daily_asof": latest_daily.get("date"),
        "resonance_policy": 'locked_mainline_v1',
        "resonance_boards": resonance_boards,
        "resonance_source": resonance_source,
        "entry_basis_version": 2,
        "close_sum4": sum(closes[-4:]),
        "close_sum19": sum(closes[-19:]),
        "daily_atr14": mean(max(highs[i] - lows[i], abs(highs[i] - closes[i-1]), abs(lows[i] - closes[i-1]))
                            for i in range(len(closes) - 14, len(closes))),
        "daily_resistance_levels": sorted(set(
            [highs[i] for i in range(max(2, len(highs)-60), len(highs)-2)
             if highs[i] >= max(highs[i-2:i] + highs[i+1:i+3])]
            + [max(highs[-20:])])),
        "turnover": round(turnover, 3),
        "liquidity_evidence": {
            "asof": latest_daily.get('date'),
            "turnover_status": 'available' if turnover > 0 else 'missing',
            "turnover_source": latest_daily.get('turnover_source') or ('completed_daily' if _f(latest_daily.get('turnover')) else 'matching_close' if completed_quote else None),
            "float_cap_source": latest_daily.get('float_market_cap_yi_source') or ('completed_daily_or_matching_close' if float_market_cap_yi else None),
        },
        "amount_yi": round(amount_yi, 3),
        "float_market_cap_yi": round(float_market_cap_yi, 3) if float_market_cap_yi is not None else None,
        "recent_limit_days": recent_limit_days,
        "macd": macd,
        "kdj": kdj,
        "emotion_pool_rank": external_leader.get("rank"),
        "emotion_pool_score": external_leader.get("score"),
        "emotion_pool_cross_verified": bool(external_leader.get("cross_verified")),
        "emotion_pool_cross_status": external_leader.get("cross_status") or ('verified' if external_leader.get('cross_verified') else 'unavailable'),
        "emotion_pool_source_date": external_leader.get("source_date"),
        "leader_profile": external_leader.get("leader_profile"),
        "prior_day_touched_limit": bool(external_leader.get("touched_limit")),
        "prior_day_closed_limit": bool(external_leader.get("closed_limit")),
        "indicator_seed": {
            "ema12": _ema_series(closes, 12)[-1], "ema26": _ema_series(closes, 26)[-1],
            "dea": _ema_series([a-b for a, b in zip(_ema_series(closes, 12), _ema_series(closes, 26))], 9)[-1],
            "k": kdj['k'], "d": kdj['d'], "highs8": highs[-8:], "lows8": lows[-8:],
            "avg_volume5": mean([_f(b.get('volume_lot')) for b in ohlc[-5:]]),
        },
    }
    sector_identity.apply(metrics, sector_identity.resolve(stock, stock.get('board_catalog')))
    metrics['mainline_research_candidates'] = sector_identity.research_candidates(stock, stock.get('board_catalog'))

    # A board-type candidate is allowed to use only E5 later.  It is never
    # called a leader from a daily close alone; intraday sector leadership is
    # deliberately delegated to the existing E5 verification.
    external_qualified = bool(external_leader.get("qualified"))
    emotional_close = bool(
        pct >= _pct_limit(code)
        or str(tech.get("state") or "").startswith("强情绪涨停")
        or external_qualified
    )
    context_evidence = _leader_context_evidence(context_text)
    leadership_context = any(context_evidence.values())
    leader_evidence = {
        "amount_ratio_ge_1": amount_ratio >= 1.0,
        "turnover_ge_8": turnover >= 8.0,
        "amount_ge_8yi": amount_yi >= 8.0,
        "float_cap_le_500yi": float_market_cap_yi is not None and float_market_cap_yi <= 500.0,
        "theme_identified": bool(concepts.strip()),
        "leadership_context": leadership_context or external_qualified,
        "popularity_pool_qualified": external_qualified,
    }
    leader_evidence_score = sum(bool(value) for value in leader_evidence.values())
    leader_qualified = bool(
        emotional_close
        and (recent_limit_days >= 1 or external_qualified)
        and leader_evidence_score >= 3
        and (amount_ratio >= 1.0 or turnover >= 8.0 or leadership_context or external_qualified)
    )
    metrics.update({
        "leader_evidence": leader_evidence,
        "leader_evidence_score": leader_evidence_score,
        "leader_context_evidence": context_evidence,
        "missing_evidence": [label for label, missing in (
            ('T-1换手率', turnover <= 0), ('流通市值', float_market_cap_yi is None),
            ('人气榜候选证据', not external_leader),
        ) if missing],
    })
    if leader_qualified and not stock.get("disable_leader_routing"):
        metrics.update({
            "qualified": True,
            "gate_reason": (
                f"东方财富人气母池/日线强度筛选通过，龙头辨识度证据 {leader_evidence_score}/7，"
                "仅取得龙头候选资格。"
            ),
        })
        cap_text = f"、流通市值约 {float_market_cap_yi:.0f}亿" if float_market_cap_yi is not None else ""
        opening_reversal = str(metrics.get("leader_profile") or "") == "FAILED_LIMIT_REVERSAL"
        timing_text = (
            "E5二次转强或受限的E5A开盘弱转强"
            if opening_reversal
            else "E5B开盘强承接或E5二次转强"
        )
        leader_reason = (
            f"东财人气第 {external_leader.get('rank') or '-'}、筛选 {external_leader.get('score') or '-'}分、"
            f"涨幅 {pct:.2f}%、近4日涨停 {recent_limit_days} 次、5日额比 {amount_ratio:.2f}、"
            f"换手 {turnover:.2f}%、成交 {amount_yi:.1f}亿{cap_text}；"
            f"开盘啦：{cross_status_text(metrics.get('emotion_pool_cross_status'))}；"
            f"盘前仅列为情绪龙头候选，盘中必须先完成实时板块共振，再由{timing_text}证明买点。"
        )
        contract = _contract(
            LEADER,
            leader_reason,
            metrics,
            True,
        )
        contract["allowed_patterns"] = (
            ["V2_E5A_LEADER_OPENING_REVERSAL", "V2_E5_LEADER_SECOND_LEG"]
            if opening_reversal
            else ["V2_E5B_LEADER_OPENING_HOLD", "V2_E5_LEADER_SECOND_LEG"]
        )
        contract["formal_entry_signal"] = formal_entry_signal(contract)
        return contract

    events_520 = _recent_520_events(closes, lows, lookback=5)
    fresh_cross = bool(events_520["recent_cross"])
    ma20_pullback_reclaim = bool(events_520["recent_ma20_reclaim"])
    no_chase_extension = bool(ma20 and close <= ma20 * 1.08)
    kdj_confirmed = bool(
        kdj.get("bullish")
        and kdj.get("not_overheated")
    )
    checks_520 = {
        'recent_event': fresh_cross or ma20_pullback_reclaim,
        'ma5_ge_ma20': bool(ma5 and ma20 and ma5 >= ma20),
        'close_ge_ma20': bool(ma20 and close >= ma20),
        'volume_ready': amount_ratio >= 1.0,
        'macd_ready': bool(macd.get('bullish')),
        'kdj_ready': kdj_confirmed,
        'distance_ready': no_chase_extension,
    }
    metrics.update(fresh_cross=fresh_cross, ma20_pullback_reclaim=ma20_pullback_reclaim,
                   cross_days_ago=events_520['cross_days_ago'], reclaim_days_ago=events_520['reclaim_days_ago'],
                   checks_520=checks_520)
    method_520 = bool(
        ma5 and ma20
        and ma5 >= ma20
        and close >= ma20
        and (fresh_cross or ma20_pullback_reclaim)
    )
    setup_520_ready = bool(
        method_520
        and amount_ratio >= 1.0
        and bool(macd.get("bullish"))
        and kdj_confirmed
        and no_chase_extension
    )
    # Evaluate both daily methods before selecting ONE premarket contract.
    # Intraday execution never switches to the alternative to evade a veto.
    ma5_checks = _ma5_qualification(closes, lows, amounts, pct, amount_ratio, macd)
    ma5_ready = bool(ma5_checks['ma5_method_qualified'] and ma5_checks['qualification_count'] >= 3)
    prefer_ma5 = bool(method_520 and ma5_ready and (
        not setup_520_ready or (not ma20_pullback_reclaim and events_520['cross_days_ago'] != 0)))
    metrics.update(method_candidates={
        TREND_520: {'method_available': method_520, 'daily_qualified': setup_520_ready,
                    'next_day_cross': fresh_cross and events_520['cross_days_ago'] == 0,
                    'intraday_ma20_test': method_520},
        TREND_MA5: {'method_available': ma5_checks['ma5_method_qualified'],
                    'daily_qualified': ma5_checks['ma5_method_qualified'], 'entry_evidence_ready': ma5_ready,
                    'qualification_count': ma5_checks['qualification_count']},
    }, selection_policy='independent_daily_methods_v2',
       selection_reason=('MA5独立量价资格通过，优先选择趋势回踩；520非金叉次日或增强确认不足'
                         if prefer_ma5 else '按独立日线资格选择主策略，盘中不自动切换方法'))
    if method_520 and not prefer_ma5:
        metrics.update({
            "qualified": setup_520_ready,
            "fresh_cross": fresh_cross,
            "ma20_pullback_reclaim": ma20_pullback_reclaim,
            "cross_days_ago": events_520["cross_days_ago"],
            "reclaim_days_ago": events_520["reclaim_days_ago"],
            "kdj_confirmed": kdj_confirmed,
            "setup_ready": setup_520_ready,
            "gate_reason": (
                "520方法与当日确认均通过：近5日金叉/MA20回踩、量能、MACD、KDJ及非追高位置完整。"
                if setup_520_ready
                else "已归类520观察，但当日入场授权未通过：仍须同时满足额比≥1、MACD多头、KDJ多头不过热及距MA20≤8%。"
            ),
        })
        return _contract(
            TREND_520,
            f"日线MA5/MA20 {ma5:.2f}/{ma20:.2f}，"
            f"{'近5日出现5-20金叉' if fresh_cross else '近5日出现MA20回踩收复'}；"
            f"额比 {amount_ratio:.2f}、MACD{'通过' if macd.get('bullish') else '待确认'}、"
            f"K/D/J {kdj.get('k'):.1f}/{kdj.get('d'):.1f}/{kdj.get('j'):.1f}。"
            f"{'允许等待日线锚点回踩或金叉次日确认' if setup_520_ready else '今日只观察，不授权开仓'}。",
            metrics,
            True,
        )

    metrics.update(ma5_checks)
    qualification_count = ma5_checks['qualification_count']
    ma5_distance_pct = ma5_checks['ma5_distance_pct']
    ma5_method_qualified = ma5_checks['ma5_method_qualified']
    setup_ready = ma5_checks['setup_ready']
    medium_trend_ok = ma5_checks['medium_trend_ok']
    if ma5_method_qualified:
        metrics.update({
            "qualified": True,
            "gate_reason": "趋势5日线方法资格通过；入场仍需日线量价至少3项、MA5锚点回踩及闭合5分钟收复。",
        })
        return _contract(
            TREND_MA5,
            f"MA5/10/20多头且MA5、MA20上行，收盘距MA5 {ma5_distance_pct:+.2f}%；"
            f"当前回踩准备度 {qualification_count}/5，{'已出现日线回踩线索' if setup_ready else '尚未出现完整日线回踩线索'}。"
            "分类用于锁定方法，真正下单需日线MA5锚点与闭合5分钟确认。"
            + (metrics['selection_reason'] + '。' if prefer_ma5 else ''),
            metrics,
            True,
        )

    if emotional_close:
        evidence_labels = {
            "amount_ratio_ge_1": "5日额比≥1.0",
            "turnover_ge_8": "换手≥8%",
            "amount_ge_8yi": "成交额≥8亿",
            "float_cap_le_500yi": "流通市值≤500亿",
            "theme_identified": "题材归属",
            "leadership_context": "明确龙头/人气/净买入证据",
            "popularity_pool_qualified": "东方财富情绪母池筛选",
        }
        missing = [label for key, label in evidence_labels.items() if not leader_evidence[key]]
        gate_reason = (
            f"情绪龙头候选证据 {leader_evidence_score}/7，"
            f"尚缺：{'、'.join(missing) if missing else '无'}；未取得新增仓资格。"
        )
    else:
        failed = []
        if not checks_520['recent_event']:
            failed.append('无近5日金叉/MA20收复事件')
        if not checks_520['ma5_ge_ma20']:
            failed.append(f'MA5 {ma5:.2f}<MA20 {ma20:.2f}')
        if not checks_520['close_ge_ma20']:
            failed.append(f'收盘{close:.2f}<MA20 {ma20:.2f}')
        if not checks_520['volume_ready']:
            failed.append(f'成交额比{amount_ratio:.2f}<1')
        if not checks_520['macd_ready']:
            failed.append(f'MACD未通过：DIF {macd["dif"]:.3f}/DEA {macd["dea"]:.3f}，需DIF≥DEA且≥0')
        if not checks_520['kdj_ready']:
            failed.append(f'KDJ未通过：K/D/J {kdj["k"]:.1f}/{kdj["d"]:.1f}/{kdj["j"]:.1f}')
        if not checks_520['distance_ready']:
            failed.append('距MA20超过8%')
        event_text = (f'存在近5日520事件（金叉距今{events_520["cross_days_ago"]}日、收复距今{events_520["reclaim_days_ago"]}日）'
                      if checks_520['recent_event'] else '520事件未形成')
        gate_reason = event_text + '；未通过：' + '；'.join(failed) + '；趋势5日线排列/位置未通过。'
    repair_watch = bool(not emotional_close and (fresh_cross or ma20_pullback_reclaim or medium_trend_ok)
                        and (close < ma5 or close < ma20))
    if repair_watch:
        metrics.update(repair_watch=True, candidate_method=TREND_520,
                       observation_phase='REPAIR_SHADOW', major_trend_intact=bool(ma60 and close >= ma60),
                       repair_rule='520双线收复影子验证；日线方向、MACD/KDJ、量能与锁定主线仍须验证，不授权新增仓')
        gate_reason = '趋势修复观察（非买入资格）：' + gate_reason
    return _contract(
        OBSERVE,
        gate_reason,
        {**metrics, "qualified": False, "gate_reason": gate_reason},
        True,
    )


def sector_resonance_gate(contract: dict[str, Any] | None, row: dict[str, Any] | None) -> tuple[bool, str]:
    """First execution stage: require live sector resonance for watchlist methods."""
    contract = contract or {}
    if not contract.get("is_observation_strategy"):
        return False, "历史合同未重新分类：板块共振不得授权新增仓"
    if str(contract.get("key") or OBSERVE) == OBSERVE:
        return False, '趋势修复影子观察：无新增仓授权' if (contract.get('daily_metrics') or {}).get('repair_watch') else "待分类观察：未取得可交易策略资格"
    row = row or {}
    momentum = row.get("sector_momentum") or {}
    rotation = row.get("sector_rotation") or {}
    board_name = str(momentum.get("board_name") or momentum.get("sector_name") or row.get("industry") or "所属板块")
    board_pct = _f(momentum.get("board_pct"), -999.0)
    metrics = contract.get('daily_metrics') or {}
    if board_name in BROAD_BOARDS:
        return False, f'策略第一关：{board_name}仅为宽泛关联，不能代替交易主线'
    if metrics.get('resonance_policy') == sector_identity.POLICY:
        identity = metrics.get('resonance_identity') or {}
        if not sector_identity.ready(metrics):
            return False, '策略第一关：计划主线不可执行：' + str(identity.get('reason') or '身份未验证')
        if (str(momentum.get('board_id') or '') not in metrics.get('resonance_board_ids', [])
                or board_name not in metrics.get('resonance_boards', [])):
            return False, '策略第一关：当日行情未匹配盘前板块代码，禁止切换主线'
    if metrics.get('resonance_policy') == 'locked_mainline_v1':
        planned = metrics.get('resonance_boards') or []
        if not planned or board_name not in planned:
            return False, '策略第一关：盘前锁定主线未确认，禁止切换至其他强板块：' + '、'.join(planned)
    if momentum.get("emotion_ok") is not True or board_pct < 0.8:
        return False, f"策略第一关：{board_name} 未达到板块强势阈值0.80%"
    if not rotation.get("sustained"):
        return False, f"策略第一关：{board_name} 未完成连续刷新共振"
    strategy_key = str(contract.get("key") or "")
    if strategy_key == LEADER and not rotation.get("leader_healthy"):
        return False, f"策略第一关：{board_name} 领涨股换手/承接未确认"
    if strategy_key in {TREND_520, TREND_MA5} and not rotation.get("leader_healthy") and board_pct < 1.5:
        return False, f"策略第一关：{board_name} 强度不足1.50%且领涨承接未确认"
    confirmation = "领涨承接" if rotation.get("leader_healthy") else "连续强板块代理"
    return True, f"板块共振通过：{board_name} {board_pct:.2f}% + 连续刷新 + {confirmation}"


def daily_qualification_gate(contract: dict[str, Any] | None) -> tuple[bool, str]:
    """Second execution stage: fail closed when the premarket daily proof is absent."""
    contract = contract or {}
    if not contract.get("is_observation_strategy"):
        return False, "历史合同未重新分类：缺少最新日线策略资格"
    if not _bool(contract.get("daily_qualified")):
        return False, str(contract.get("daily_gate_reason") or "策略第二关：盘前日线策略资格未通过")
    return True, str(contract.get("daily_gate_reason") or "日线策略资格通过")


def execution_gate(contract: dict[str, Any] | None, pattern: str | None) -> tuple[bool, str]:
    """Check that shared timing evidence belongs to the named method."""
    contract = contract or {}
    if not contract.get("is_observation_strategy"):
        return False, "历史合同未重新分类：旧时机路径已停用"
    key = str(contract.get("key") or OBSERVE)
    allowed = [str(item) for item in contract.get("allowed_patterns") or []]
    if not allowed:
        return False, f"{contract.get('name') or '待分类观察'}：未取得可交易策略资格"
    if pattern not in allowed:
        return False, f"{contract.get('name') or key}的证据路径仅允许{'、'.join(allowed)}，当前{pattern or '未形成时机证据'}不匹配"
    return True, f"{contract.get('name') or key}的时机证据{pattern}匹配"


def entry_scenario(contract: dict[str, Any] | None, pattern: str | None) -> str:
    """Return the user-facing order scenario; V2 patterns stay as evidence."""
    contract = contract or {}
    if not contract.get("is_observation_strategy"):
        return ""
    return STRATEGY_ENTRY_SCENARIOS.get(str(contract.get("key") or ""), "")


def is_strategy_entry(scenario: str | None) -> bool:
    return str(scenario or "") in STRATEGY_ENTRY_SCENARIO_SET


def contract_from_level(level: dict[str, Any] | None) -> dict[str, Any]:
    """Rehydrate the compact premarket table fields for the realtime engine."""
    level = level or {}
    key = str(level.get("strategy_key") or "").strip()
    if not key or key == CORE_V2:
        key = OBSERVE
    if key not in STRATEGY_META:
        key = OBSERVE
    meta = STRATEGY_META[key]
    patterns = [str(item) for item in level.get("strategy_allowed_patterns") or [] if str(item).strip()]
    return {
        "key": key,
        "name": str(level.get("strategy_name") or meta["name"]),
        "style": str(level.get("strategy_style") or meta["style"]),
        "formal_entry_signal": str(level.get("strategy_formal_entry_signal") or formal_entry_signal(key)),
        "allowed_patterns": patterns if key != OBSERVE else [],
        "entry_rule": str(level.get("strategy_entry_rule") or meta["entry_rule"]),
        "exit_rule": str(level.get("strategy_exit_rule") or meta["exit_rule"]),
        "reason": str(level.get("strategy_reason") or "盘前策略字段缺失，按保守路径处理。"),
        "is_observation_strategy": True,
        "version": STRATEGY_CONTRACT_VERSION,
        "daily_metrics": {
            **dict(level.get("strategy_daily_metrics") or {}),
            "leader_profile": level.get("strategy_leader_profile"),
            "emotion_pool_cross_verified": _bool(level.get("strategy_leader_cross_verified")),
            "prior_day_touched_limit": _bool(level.get("strategy_prior_day_touched_limit")),
            "prior_day_closed_limit": _bool(level.get("strategy_prior_day_closed_limit")),
        },
        "daily_qualified": _bool(level.get("strategy_daily_qualified")),
        "daily_gate_reason": str(level.get("strategy_daily_evidence") or "盘前日线策略资格字段缺失，按未通过处理。"),
    }
