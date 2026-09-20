"""Provisional 520 repair evidence, isolated from executable strategy contracts."""
from datetime import datetime, timedelta
from core import method_entry, observation_strategy_router as router


def evaluate(contract, bars5, price, now, row, regime, config):
    try:
        return _evaluate(contract, bars5, price, now, row, regime, config)
    except Exception as exc:
        return {'mode': 'SHADOW_ONLY', 'entry_allowed': False, 'order_action': 'NO_ORDER',
                'shadow_ready': False, 'capture_error': type(exc).__name__,
                'blockers': ['修复影子证据无效，不影响正式策略执行']}


def _evaluate(contract, bars5, price, now, row, regime, config):
    metrics = contract.get('daily_metrics') or {}
    if not metrics.get('repair_watch') and not (
        contract.get('daily_qualified') is False and metrics.get('indicator_seed')
    ):
        return {}
    result = {'mode': 'SHADOW_ONLY', 'entry_allowed': False, 'order_action': 'NO_ORDER',
              'candidate_method': router.TREND_520, 'shadow_ready': False, 'blockers': [],
              'indicator_period': 'developing_daily_at_closed_5m',
              'note': '修复影子验证，不是绿色买入信号，不产生模拟订单'}
    seed = metrics.get('indicator_seed') or {}
    try:
        dated = datetime.fromisoformat(str(metrics['daily_asof'])).date() < now.date()
        ends = [datetime.fromisoformat(str(b['bar_end'])) + timedelta(
            minutes=1 if b.get('timestamp_convention') == 'start' else 0) for b in bars5]
        expected = [now.replace(hour=hour, minute=minute, second=0, microsecond=0) + timedelta(minutes=i)
                    for hour, minute in ((9, 30), (13, 0)) for i in range(5, 121, 5)
                    if now.replace(hour=hour, minute=minute, second=0, microsecond=0) + timedelta(minutes=i) <= now]
        closed = bool(ends and ends == sorted(set(ends)) and all(t.date() == now.date() and t <= now for t in ends)
                      and ends == expected and all(b.get('timestamp_convention') in ('start', 'end') for b in bars5))
        complete_seed = all(method_entry.number(seed.get(k)) is not None for k in ('ema12', 'ema26', 'dea', 'k', 'd', 'avg_volume5'))
        complete_seed = complete_seed and len(seed.get('highs8', [])) == 8 and len(seed.get('lows8', [])) == 8
    except (KeyError, TypeError, ValueError):
        dated = closed = complete_seed = False
    if not dated or not closed or not complete_seed or len(bars5) < 3:
        result['blockers'] = ['修复验证缺少T-1指标种子或完整闭合5分钟数据']
        return result
    if not metrics.get('major_trend_intact') or regime not in ('TREND', 'REPAIR'):
        result['blockers'].append('大周期方向尚未修复，禁止逆长期趋势试仓')
    last = float(bars5[-1]['close'])
    ema12 = seed['ema12'] + 2 / 13 * (last - seed['ema12'])
    ema26 = seed['ema26'] + 2 / 27 * (last - seed['ema26'])
    dif = ema12 - ema26
    dea = seed['dea'] + .2 * (dif - seed['dea'])
    high = max(seed['highs8'] + [float(b['high']) for b in bars5])
    low = min(seed['lows8'] + [float(b['low']) for b in bars5])
    rsv = (last-low) / (high-low) * 100 if high > low else 50
    k = (2 * seed['k'] + rsv) / 3
    d = (2 * seed['d'] + k) / 3
    j = 3 * k - 2 * d
    volume = sum(float(b.get('volume') or 0) for b in bars5)
    volume_ratio = volume / seed['avg_volume5'] if seed['avg_volume5'] > 0 else None
    if volume_ratio is None or volume_ratio < 1:
        result['blockers'].append('当日已闭合成交量未达到过去5日均量，尚未确认放量')
    prospective = {**metrics, 'ma20_pullback_reclaim': True, 'fresh_cross': False,
                   'macd': {'bullish': dif >= dea and dif >= 0}, 'kdj_confirmed': k >= d and j < 100}
    shadow_contract = {**contract, 'key': router.TREND_520, 'daily_qualified': True,
                       'daily_metrics': prospective}
    detail = method_entry.evaluate(shadow_contract, bars5, price, now, config.get('daily_method_entry') or {})
    if detail.get('anchor_ma5', 0) < metrics.get('ma5', 0) or detail.get('anchor_ma20', 0) < metrics.get('ma20', 0):
        result['blockers'].append('动态MA5/MA20尚未共同转向上行')
    sector_ok, sector_reason = router.sector_resonance_gate(shadow_contract, row)
    if not sector_ok:
        result['blockers'].append(sector_reason)
    result['blockers'].extend(detail.get('blockers') or [])
    atr = method_entry.number(metrics.get('daily_atr14')) or 0
    target, invalid = detail.get('target'), detail.get('invalidation')
    room = (target-price) / atr if target is not None and atr > 0 else None
    rr = (target-price) / (price-invalid) if target is not None and invalid is not None and price > invalid else None
    if room is None or rr is None or room < 1 or rr < 1.5:
        result['blockers'].append('修复影子空间/RR不足或缺失')
    vwap = (row.get('rt_features') or {}).get('vwap')
    if not vwap or price < vwap or price-vwap > atr * .5:
        result['blockers'].append('修复影子VWAP承接/距离未通过')
    if now.strftime('%H:%M') >= '14:45':
        result['blockers'].append('超过新增仓截止时间')
    result.update(shadow_ready=not result['blockers'], method_entry=detail, sector_ok=sector_ok,
                  indicators={'dif': dif, 'dea': dea, 'k': k, 'd': d, 'j': j, 'volume_ratio': volume_ratio},
                  room_atr=room, reward_risk=rr)
    return result
