"""Shared entry evidence. Thresholds are risk overlays, not win-rate claims."""
from datetime import datetime, timedelta
import math


def number(value):
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (TypeError, ValueError):
        return None


def target_evidence(row, price, closed_close, fallback=None):
    closed_close = number(closed_close)
    metrics = (row.get('strategy_contract') or {}).get('daily_metrics') or {}
    dynamic = row.get('dynamic') or {}
    sources = [('planned_pressure', row.get('pressure')),
               ('execution_pressure', dynamic.get('execution_pressure')),
               ('trend_pressure', dynamic.get('trend_pressure'))]
    sources += [('daily_resistance', x) for x in metrics.get('daily_resistance_levels') or []]
    sources += [('method_target', fallback), ('limit_up', (row.get('quote') or {}).get('limit_up'))]
    candidates, pending = [], []
    for source, raw in sources:
        level = number(raw)
        if level is None or level <= 0:
            continue
        if level > price:
            candidates.append({'source': source, 'price': level})
        elif closed_close is None or closed_close <= level:
            pending.append({'source': source, 'price': level})
    chosen = min(candidates, key=lambda x: x['price']) if candidates else {}
    return {'target': chosen.get('price'), 'source': chosen.get('source'),
            'candidates': candidates, 'unconfirmed_reclaims': pending}


def leader_identity(row, config):
    """Leadership within the observed sector sample, not a full-market rank."""
    quote = row.get('quote') or {}
    rotation = row.get('sector_rotation') or {}
    leader = rotation.get('leader') or {}
    board = row.get('sector_momentum') or {}
    code = str(row.get('code') or quote.get('code') or '')
    leader_code = str(leader.get('code') or '')
    pct, board_pct, leader_pct = map(number, (quote.get('pct'), board.get('board_pct'), leader.get('pct')))
    relative = pct - board_pct if pct is not None and board_pct is not None else None
    gap = leader_pct - pct if pct is not None and leader_pct is not None else None
    cfg = config.get('leader_identity') or {}
    own_leader = bool(code and code == leader_code)
    leading_group = bool(code and leader_code and relative is not None and gap is not None
                         and relative >= float(cfg.get('min_relative_board_pct', 1.0))
                         and gap <= float(cfg.get('max_leader_gap_pct', 2.0)))
    confirmed = bool(rotation.get('sustained') and rotation.get('leader_healthy')
                     and relative is not None and relative >= 0 and (own_leader or leading_group))
    return {'confirmed': confirmed, 'candidate_code': code, 'sector_leader_code': leader_code,
            'scope': 'observed_sector_sample', 'relative_board_pct': relative,
            'leader_gap_pct': gap, 'role': 'SAMPLE_LEADER' if confirmed and own_leader else
            'LEADING_GROUP' if confirmed else 'POPULAR_CANDIDATE',
            'reason': '本票在当日板块样本中领涨/处于领先组' if confirmed else
            '本票领导力未确认：板块领涨健康不能替代本票相对强弱与领先组证据'}


def confirmation_guard(bar, now, price_floor, trigger_level=None):
    try:
        end = datetime.fromisoformat(str(bar.get('bar_end')))
        if bar.get('timestamp_convention') != 'end':
            end += timedelta(minutes=1)
        # A new closed bar invalidates the old token; lunch creates no 13:00 bar.
        next_end = end + timedelta(minutes=5)
        if end.hour == 11 and end.minute == 30:
            next_end = end.replace(hour=13, minute=5)
        if end.hour == 15 and end.minute == 0:
            next_end = end
        floor = number(price_floor)
        trigger = number(trigger_level)
        if floor is None or floor <= 0 or end > now or end.date() != now.date():
            raise ValueError('invalid confirmation')
        return {'required': True, 'confirmed_at': end.isoformat(sep=' '),
                'valid_until': next_end.isoformat(sep=' '), 'price_floor': max(floor, trigger or floor),
                'trigger_level': trigger}
    except (TypeError, ValueError):
        return {'required': True, 'error': '确认K线时间或失效条件缺失'}


def guard_reason(guard, price, now):
    if not guard:
        return '缺少入场确认凭证'
    if guard.get('error'):
        return str(guard['error'])
    try:
        begin = datetime.fromisoformat(guard['confirmed_at'])
        end = datetime.fromisoformat(guard['valid_until'])
        floor = number(guard['price_floor'])
        if floor is None or number(price) is None:
            raise ValueError('invalid price')
        if not begin <= now < end:
            return '确认已跨入下一根闭合5分钟，必须重新评估，不能只刷新价格或有效期'
        if price < floor - 1e-9:
            return f'现价{price:.2f}已破坏确认结构{floor:.2f}，取消旧买点'
    except (KeyError, TypeError, ValueError):
        return '入场确认凭证无效'
    return None


def weak_repair_evidence(contract, bars5, price, vwap, regime, path):
    required = bool(contract.get('key') == 'TREND_MA5' and vwap and price < vwap
                    and (regime == 'REPAIR' or path == 'RALLY_FAILURE'))
    detail = {'required': required, 'confirmed': not required}
    if not required or len(bars5) < 2:
        return detail
    previous, current = bars5[-2:]
    level = float(previous['high'])
    pv, cv = number(previous.get('volume')), number(current.get('volume'))
    detail.update(breakout_level=level, resume_volume_ratio=cv / pv if pv and cv is not None else None)
    detail['confirmed'] = bool(float(current['close']) > level and price >= level
                               and float(current['close']) > float(current['open'])
                               and float(current['low']) >= float(previous['low'])
                               and pv and cv is not None and cv >= pv)
    return detail
