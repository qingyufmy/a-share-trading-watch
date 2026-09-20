"""Daily-method price anchors with closed five-minute entry confirmation.

The old E identifiers are transport aliases only; no 15m MA20 setup is used.
Numerical buffers are engineering risk overlays, not claims from the book.
"""
from datetime import datetime, timedelta
import math


def number(value):
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (TypeError, ValueError):
        return None


def evaluate(contract, bars5, price, now, config):
    metrics = contract.get('daily_metrics') or {}
    key = contract.get('key')
    detail = {'eligible': False, 'method': key, 'qualification_period': '1d',
              'trigger_period': '5m_closed', 'risk_period': '1d', 'blockers': []}
    try:
        asof = datetime.fromisoformat(str(metrics.get('daily_asof'))).date()
        dated = asof < now.date()
    except (TypeError, ValueError):
        dated = False
    atr = number(metrics.get('daily_atr14'))
    sum4, sum19 = number(metrics.get('close_sum4')), number(metrics.get('close_sum19'))
    if not dated or not atr or atr <= 0 or sum4 is None or sum19 is None:
        detail['blockers'] = ['日线入场依据不完整/未闭合：需要T-1日线ATR及MA5/20计算基数']
        return detail
    if not contract.get('daily_qualified') or len(bars5) < 3:
        detail['blockers'] = ['日线方法资格或三根闭合5分钟确认不足']
        return detail
    lookback = max(3, min(12, int(config.get('touch_lookback_bars', 3))))
    window = bars5[-lookback:]
    try:
        ends = [datetime.fromisoformat(str(b.get('bar_end'))) for b in window]
        closed = all(t.date() == now.date()
                     and t + timedelta(minutes=0 if b.get('timestamp_convention') == 'end' else 1) <= now
                     for t, b in zip(ends, window))
        closed = closed and ends == sorted(set(ends))
    except (TypeError, ValueError):
        closed = False
    if not closed:
        detail['blockers'] = ['5分钟时间戳未闭合、跨日或顺序异常']
        return detail
    current, previous = bars5[-1], bars5[-2]
    anchor5 = (sum4 + float(current['close'])) / 5
    anchor20 = (sum19 + float(current['close'])) / 20
    tolerance = atr * float(config.get('touch_atr', 0.15))
    max_distance = atr * float(config.get('max_anchor_distance_atr', 0.5))
    anchors = [('MA5', anchor5, 'E1_TREND_PULLBACK_RECLAIM')]
    if key == 'TREND_520':
        # Daily qualification authorizes observing a NEW intraday MA20 test;
        # it must not require that same pullback to have happened yesterday.
        anchors = ([('MA20', anchor20, 'E3_MA20_STRUCTURAL_RECLAIM')]
                   if metrics.get('fresh_cross') or metrics.get('ma20_pullback_reclaim') else [])
    touched = None
    for label, anchor, pattern in anchors:
        for bar in reversed(window[:-1]):
            period, seed = (5, sum4) if label == 'MA5' else (20, sum19)
            touch_anchor = (seed + float(bar['close'])) / period
            touch_tolerance = min(tolerance, touch_anchor * float(config.get('max_touch_distance_pct', 0.3)) / 100)
            if float(bar['low']) <= touch_anchor + touch_tolerance and float(bar['high']) >= touch_anchor - touch_tolerance:
                touched = (label, anchor, pattern, bar)
                break
        if touched:
            break
    # Only the next session after the completed daily cross can use this
    # continuation route. An old cross is not a fresh breakout permission.
    next_day_cross = (key == 'TREND_520' and metrics.get('fresh_cross')
                      and metrics.get('cross_days_ago') == 0)
    if not touched and next_day_cross:
        breakout_level = max(float(b['high']) for b in bars5[-3:-1])
        if float(current['close']) > breakout_level and anchor5 >= anchor20:
            touched = ('MA5', anchor5, 'E4_BREAKOUT_RETEST', current)
            detail.update(entry_variant='CROSS_NEXT_DAY_CONFIRM', breakout_level=breakout_level)
    detail.update(daily_asof=str(asof), daily_atr14=atr, anchor_ma5=anchor5, anchor_ma20=anchor20,
                  touch_lookback_bars=lookback, touch_anchor_period='1d_developing_at_touch',
                  confirmation_low_bars=2)
    if not touched:
        detail['blockers'] = [
            '520等待MA20受控回踩，或T-1新金叉的次日闭合确认' if key == 'TREND_520'
            else '趋势5日线等待日线MA5受控回踩；15分钟形态不能替代日线锚点']
        return detail
    label, anchor, pattern, touch = touched
    detail.update(anchor_name=label, anchor=anchor, touch_bar_end=touch.get('bar_end'))
    closed_confirmation = (float(current['close']) >= anchor and float(current['close']) > float(previous['close'])
                           and float(current['low']) >= float(previous['low']))
    advance = float(current['close']) - float(previous['close'])
    minimum_advance = max(0.01, atr * float(config.get('min_confirmation_advance_atr', 0.02)))
    detail.update(confirmation_bar_end=current.get('bar_end'), confirmation_low=float(current['low']),
                  confirmation_advance=advance, minimum_confirmation_advance=minimum_advance,
                  confirmation_volume_ratio=(float(current.get('volume') or 0) / float(previous['volume'])
                                             if previous.get('volume') else None))
    if advance + 1e-9 < minimum_advance or float(current['close']) <= float(current['open']):
        detail['blockers'].append('闭合5分钟转强幅度不足或非阳线，微小跳动不作为确认')
    if price < float(current['low']) - 1e-9:
        detail['blockers'].append('现价已跌破确认5分钟低点，旧回踩确认失效')
    if not closed_confirmation or price < anchor:
        detail['blockers'].append('日线锚点尚未由闭合5分钟收复并抬高低点')
    if price - anchor > max_distance:
        detail['blockers'].append('现价距日线锚点超过0.5日ATR，禁止追价')
    if key == 'TREND_MA5' and int(metrics.get('qualification_count') or 0) < 3:
        detail['blockers'].append('趋势5日线的日线量价证据不足3项，继续观察')
    if key == 'TREND_520' and not (metrics.get('macd', {}).get('bullish') and metrics.get('kdj_confirmed')):
        detail['blockers'].append('520日线MACD/KDJ确认缺失')
    if key == 'TREND_520' and (anchor5 < anchor20 or float(current['close']) < anchor20):
        detail['blockers'].append('520动态MA5/MA20多头结构已失效')
    supports = [float(b['low']) for b in bars5[-3:]] + [anchor]
    if key == 'TREND_520':
        supports.append(anchor20)
    invalidation = min(supports) - atr * float(config.get('stop_buffer_atr', 0.2))
    targets = [number(x) for x in metrics.get('daily_resistance_levels') or []]
    target = min((x for x in targets if x is not None and x > price), default=None)
    detail.update(pattern=pattern, invalidation=invalidation, target=target,
                  eligible=not detail['blockers'])
    return detail
