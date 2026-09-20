"""Enrich missing daily liquidity only from matching, completed-session quotes."""
import math
import re
import hashlib
import json
from datetime import datetime
from pathlib import Path


def positive(value):
    try:
        value = float(value)
        return value if math.isfinite(value) and value > 0 else None
    except (TypeError, ValueError):
        return None


def matching_close(bar, quote):
    stamp = re.sub(r'\D', '', str(quote.get('datetime') or ''))
    close, quoted = positive(bar.get('close')), positive(quote.get('close'))
    return bool(len(stamp) >= 12 and stamp[:8] == str(bar.get('date', '')).replace('-', '')
                and (not bar.get('code') or bar.get('code') == quote.get('code'))
                and '1500' <= stamp[8:12] <= '1530' and not quote.get('_stale')
                and close and quoted and abs(close - quoted) <= max(.011, close * .0001))


def enrich(daily, quote, cached_quote):
    if not daily:
        return daily
    last = dict(daily[-1])
    for source, candidate in (('same_day_closing_quote', quote), ('verified_closing_cache', cached_quote)):
        if quote.get('code') and candidate and candidate.get('code') != quote['code']:
            continue
        if not matching_close(last, candidate or {}):
            continue
        for field in ('turnover', 'float_market_cap_yi'):
            if not positive(last.get(field)) and positive(candidate.get(field)):
                last[field] = float(candidate[field])
                last[field + '_source'] = source
        if positive(last.get('turnover')) and not positive(last.get('float_market_cap_yi')):
            volume = positive(last.get('volume_lot'))
            if volume:
                last['float_market_cap_yi'] = float(last['close']) * volume / (last['turnover'] * 10000)
                last['float_market_cap_yi_source'] = 'completed_daily_lots_and_turnover_estimate'
    return list(daily[:-1]) + [last]


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                    allow_nan=False).encode()).hexdigest()


def archive_completed_quotes(base, quotes, now):
    """One immutable verified closing-liquidity record per symbol/day."""
    saved, existing = 0, 0
    for code, quote in quotes.items():
        if not re.fullmatch(r'\d{6}', code) or quote.get('code') != code or not positive(quote.get('turnover')):
            continue
        bar = {'code': code, 'date': now.strftime('%Y-%m-%d'), 'close': quote.get('close')}
        if not matching_close(bar, quote):
            continue
        stamp = re.sub(r'\D', '', str(quote.get('datetime') or ''))
        try:
            if datetime.strptime(stamp[:14], '%Y%m%d%H%M%S') > now:
                continue
        except ValueError:
            continue
        value = {k: quote[k] for k in ('code', 'datetime', 'close', 'turnover', 'float_market_cap_yi')
                 if k in quote and (k in ('code', 'datetime') or positive(quote[k]))}
        path = Path(base) / 'data/reference/closing_liquidity' / bar['date'] / (code + '.json')
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with path.open('x', encoding='utf-8') as stream:
                json.dump({'quote': value, 'sha256': _digest(value), 'captured_at': now.isoformat()}, stream,
                          ensure_ascii=False, allow_nan=False)
            saved += 1
        except FileExistsError:
            existing += 1
    return {'saved': saved, 'existing': existing}


def load_completed_quote(base, code, day):
    if not re.fullmatch(r'\d{6}', str(code)) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}', str(day)):
        return {}
    try:
        path = Path(base) / 'data/reference/closing_liquidity' / day / (code + '.json')
        data = json.loads(path.read_text(encoding='utf-8'))
        quote = data['quote']
        if data['sha256'] != _digest(quote) or not matching_close({'code': code, 'date': day, 'close': quote['close']}, quote):
            return {}
        return quote
    except (OSError, ValueError, KeyError, TypeError):
        return {}
