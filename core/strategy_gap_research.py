"""Executable research hypotheses, frozen at observation time and never orders."""
from datetime import datetime, timedelta
import hashlib
import json
from pathlib import Path
import re
from core import entry_quality

VERSION = 'gap_shadow_v1'


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    allow_nan=False, separators=(',', ':')).encode()).hexdigest()


def end_time(bar):
    if bar.get('timestamp_convention') not in ('start', 'end'):
        raise ValueError('unknown_timestamp_convention')
    return datetime.fromisoformat(bar['bar_end']) + timedelta(
        minutes=1 if bar['timestamp_convention'] == 'start' else 0)


def expected_ends(now, period):
    return [now.replace(hour=h, minute=0, second=0, microsecond=0) + timedelta(minutes=m+i)
            for h, m in ((9, 30), (13, 0)) for i in range(period, 121, period)
            if now.replace(hour=h, minute=0, second=0, microsecond=0) + timedelta(minutes=m+i) <= now]


def valid_tail(bars, period, count, now):
    if len(bars) < count:
        return False
    try:
        tail = bars[-count:]
        if [end_time(b) for b in tail] != expected_ends(now, period)[-count:]:
            return False
        return all(all(entry_quality.number(b.get(k)) is not None for k in
                       ('open', 'high', 'low', 'close', 'volume')) and
                   0 < b['low'] <= min(b['open'], b['close']) <= max(b['open'], b['close']) <= b['high']
                   and b['volume'] >= 0 for b in tail)
    except (KeyError, ValueError, TypeError):
        return False


def quote_fresh(quote, now):
    try:
        stamp = re.sub(r'\D', '', str(quote.get('datetime') or ''))
        at = datetime.strptime(stamp[:14], '%Y%m%d%H%M%S')
        return not quote.get('_stale') and 0 <= (now-at).total_seconds() <= 90
    except ValueError:
        return False


def leader_cohort(levels, quotes, now):
    """Fixed daily-qualified leader universe, including losers and missing quotes."""
    from core.observation_strategy_router import contract_from_level
    codes = sorted(code for code, level in levels.items()
                   if (contract_from_level(level).get('key') == 'LEADER_EMOTION'
                       and contract_from_level(level).get('daily_qualified') is True))
    members = [{'code': code, 'pct': entry_quality.number(quotes[code].get('pct'))}
               for code in codes if code in quotes and quote_fresh(quotes[code], now)
               and entry_quality.number(quotes[code].get('pct')) is not None]
    members.sort(key=lambda r: (-r['pct'], r['code']))
    coverage = len(members)/len(codes) if codes else 0
    positive = sum(r['pct'] > 0 for r in members)/len(members) if members else 0
    return {'asof': now.isoformat(), 'expected': len(codes), 'available': len(members),
            'coverage': coverage, 'positive_fraction': positive,
            'qualified': len(members) >= 5 and coverage >= .9 and positive >= .6,
            'leading_codes': [r['code'] for r in members[:3]], 'members': members,
            'scope': 'fixed_daily_qualified_leader_watchlist_not_market',
            'execution_authorized': False}


def evaluate(payload):
    now = datetime.fromisoformat(payload['now'])
    bars1, bars5 = payload['bars1'], payload['bars5']
    q, contract = payload['quote'], payload['contract']
    metrics, key = contract.get('daily_metrics') or {}, contract.get('key')
    price = entry_quality.number(q.get('close')) or 0
    vwap = entry_quality.number(payload.get('vwap')) or 0
    atr = entry_quality.number(metrics.get('daily_atr14')) or 0
    target = entry_quality.number(payload['target_evidence'].get('target'))
    sector = payload['sector_ok'] is True
    common = {'quote_fresh': quote_fresh(q, now), 'positive_price': price > 0,
              'daily_qualified': contract.get('daily_qualified') is True,
              'major_regime': payload['regime'] in ('TREND', 'REPAIR'),
              'execution_data': payload['market'].get('entry_ready') is True,
              'market_permission': (payload['market'].get('market_gate') or {}).get('allowed') is True,
              'unconfirmed_pressure_clear': not payload['target_evidence'].get('unconfirmed_reclaims'),
              'before_cutoff': now.strftime('%H:%M') < '14:45'}
    def route(checks, invalidation=None):
        rr = ((target-price) / (price-invalidation) if target and invalidation
              and target > price > invalidation else None)
        checks = {**common, **checks, 'rr_at_least_1_5': rr is not None and rr >= 1.5}
        return {'checks': checks, 'blockers': [k for k, v in checks.items() if not v],
                'shadow_ready': all(checks.values()), 'reward_risk': rr,
                'target': target, 'invalidation': invalidation,
                'entry_allowed': False, 'order_action': 'NO_ORDER'}
    early = {'method_is_leader': key == 'LEADER_EMOTION', 'sector_resonance': sector,
             'own_leadership': payload['leadership'].get('confirmed') is True,
             'opening_window': '09:33' <= now.strftime('%H:%M') < '10:00',
             'closed_1m_contiguous': valid_tail(bars1, 1, 3, now),
             'vwap_held': vwap > 0 and price >= vwap,
             'no_chase': 0 < price <= vwap * 1.03 if vwap else False,
             'remaining_limit_room': bool(target and price and (target/price-1)*100 >= 2),
             'three_minute_reclaim': False, 'directional_volume': False}
    invalid = None
    if early['closed_1m_contiguous']:
        a, b, c = bars1[-3:]
        invalid = min(b['low'], c['low'])
        early['three_minute_reclaim'] = bool(c['close'] > max(a['high'], b['high'])
            and c['close'] > c['open'] and c['low'] >= b['low'] >= a['low']
            and min(b['close'], c['close']) >= vwap > 0 and price >= max(a['high'], b['high']))
        early['directional_volume'] = a['volume'] > 0 and b['volume'] > 0 and c['volume'] >= (a['volume']+b['volume'])/2
    routes = {'LEADER_EARLY_3M': route(early, invalid)}
    trend = {'method_is_trend': key in ('TREND_520', 'TREND_MA5'),
             'sector_resonance': sector, 'closed_5m_contiguous': valid_tail(bars5, 5, 6, now),
             'breakout_retest_resume': False, 'directional_volume': False,
             'vwap_held': vwap > 0 and price >= vwap, 'controlled_distance': False}
    invalid = None
    if trend['closed_5m_contiguous'] and atr > 0:
        base, breakout, retest, resume = bars5[-6:-3], bars5[-3], bars5[-2], bars5[-1]
        level = max(b['high'] for b in base)
        invalid = retest['low']
        trend['breakout_retest_resume'] = bool(breakout['close'] > level
            and level - .3*atr <= retest['low'] <= level + .1*atr and retest['close'] >= level
            and resume['close'] > max(retest['high'], resume['open'])
            and resume['low'] >= retest['low'] and price >= retest['high'])
        mean_volume = sum(b['volume'] for b in base)/len(base)
        trend['directional_volume'] = mean_volume > 0 and resume['volume'] >= max(mean_volume, retest['volume'])
        trend['controlled_distance'] = 0 <= price-level <= .5*atr
    routes['TREND_CONTINUATION_RETEST'] = route(trend, invalid)
    # Compare a separate cohort hypothesis without bypassing the live sector gate.
    cohort = payload.get('cohort') or {}
    independent = {k: v for k, v in early.items() if k not in ('sector_resonance', 'own_leadership')}
    independent.update(sector_not_confirmed=not sector,
                       cohort_same_cycle=cohort.get('asof') == payload['now'],
                       cohort_breadth=cohort.get('qualified') is True,
                       own_cohort_leadership=q.get('code') in (cohort.get('leading_codes') or []))
    early_invalid = min(b['low'] for b in bars1[-2:]) if early['closed_1m_contiguous'] else None
    routes['INDEPENDENT_LEADER'] = route(independent, early_invalid)
    return {'version': VERSION, 'mode': 'SHADOW_ONLY', 'entry_allowed': False,
            'order_action': 'NO_ORDER', 'routes': routes,
            'mainline_candidates': metrics.get('mainline_research_candidates') or [],
            'mainline_observations': payload.get('mainline_observations') or [],
            'scope': 'Research shape and risk tests only; not plan authorization, fills or profitability.'}


def capture_evaluate(row, bars1, bars5, now, regime, market):
    try:
        from core.observation_strategy_router import sector_resonance_gate
        contract = row.get('strategy_contract') or {}
        sector_ok, _ = sector_resonance_gate(contract, row)
        price = entry_quality.number((row.get('quote') or {}).get('close')) or 0
        payload = {'schema': VERSION, 'now': now.isoformat(), 'bars1': bars1[-3:], 'bars5': bars5[-6:],
                   'quote': {k: v for k, v in (row.get('quote') or {}).items()
                             if k in ('code', 'datetime', 'close', 'pct', '_stale', 'limit_up')},
                   'contract': {k: contract.get(k) for k in ('key', 'daily_qualified')},
                   'vwap': (row.get('rt_features') or {}).get('vwap'),
                   'regime': regime, 'market': {k: market.get(k) for k in ('entry_ready', 'market_gate')},
                   'sector_ok': sector_ok, 'leadership': entry_quality.leader_identity(row, {}),
                   'cohort': row.get('gap_leader_cohort') or {},
                   'mainline_observations': row.get('gap_mainline_evidence') or [],
                   'target_evidence': entry_quality.target_evidence(row, price,
                       bars1[-1]['close'] if contract.get('key') == 'LEADER_EMOTION' and bars1
                       else bars5[-1]['close'] if bars5 else None)}
        payload['contract']['daily_metrics'] = {k: v for k, v in (contract.get('daily_metrics') or {}).items()
                                               if k in ('daily_atr14', 'daily_asof', 'mainline_research_candidates')}
        # Freeze JSON primitives. A malformed shadow input must never disrupt execution.
        payload = json.loads(json.dumps(payload, allow_nan=False))
        result = evaluate(payload)
        result['replay'] = {'input': payload, 'expected': json.loads(json.dumps(result)),
                            'evaluator_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}
        result['replay']['sha256'] = digest(result['replay'])
        return result
    except Exception as exc:
        return {'version': VERSION, 'mode': 'SHADOW_ONLY', 'entry_allowed': False,
                'order_action': 'NO_ORDER', 'capture_error': type(exc).__name__}


def replay(frozen):
    if frozen.get('sha256') != digest({k: v for k, v in frozen.items() if k != 'sha256'}):
        raise ValueError('research_input_integrity_failed')
    return evaluate(frozen['input'])


def append_opening_snapshot(signals, directory, cache, now):
    """Supplement five-minute audits with bounded closed-minute leader evidence."""
    if not '09:30' <= now.strftime('%H:%M') < '10:00':
        return False
    rows = []
    for signal in signals:
        research = (signal.get('timing_v2') or {}).get('gap_research') or {}
        frozen = research.get('replay') or {}
        if ((frozen.get('input') or {}).get('contract') or {}).get('key') == 'LEADER_EMOTION':
            rows.append({'symbol': signal.get('symbol'), 'gap_research': research})
    if not rows:
        return False
    bucket = now.strftime('%Y-%m-%d %H:%M')
    signature = digest([(r['symbol'], ((r['gap_research']['replay']['input'].get('bars1') or [{}])[-1]).get('bar_end'))
                        for r in rows])
    state = cache.get('gap_opening_audit') or {}
    if state.get('bucket') == bucket and (state.get('signature') == signature or state.get('writes', 0) >= 3):
        return False
    path = Path(directory) / ('gap_opening_' + now.strftime('%Y%m%d') + '.jsonl')
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {'timestamp': now.isoformat(), 'rows': rows, 'input_sha256': digest(rows), 'mode': 'SHADOW_ONLY'}
    with path.open('a', encoding='utf-8') as stream:
        stream.write(json.dumps(payload, ensure_ascii=False, allow_nan=False) + '\n')
    cache['gap_opening_audit'] = {'bucket': bucket, 'signature': signature,
                                'writes': state.get('writes', 0)+1 if state.get('bucket') == bucket else 1}
    return True
