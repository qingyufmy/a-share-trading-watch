"""Research-only notifications and forward observations, isolated from orders."""
from contextlib import closing
from datetime import datetime
import json
from pathlib import Path
import re
import sqlite3

from core import entry_quality, strategy_gap_research as gaps

ROUTES = {
    'LEADER_EARLY_3M': '龙头早盘3分钟确认',
    'TREND_CONTINUATION_RETEST': '趋势突破回踩再启动',
    'INDEPENDENT_LEADER': '独立情绪龙头',
    'REPAIR_520': '520日内修复',
}
BASES = {
    'LEADER_EARLY_3M': '连续3根闭合1分钟收复、VWAP承接、方向量能、板块共振及研究空间检查通过',
    'TREND_CONTINUATION_RETEST': '闭合5分钟突破、回踩、再启动，VWAP、方向量能、板块及研究空间检查通过',
    'INDEPENDENT_LEADER': '原板块未确认；固定情绪候选群体广度、个股领先及早盘闭合形态通过（替代板块的研究假设）',
    'REPAIR_520': '日内发展中均线、MACD、KDJ、已闭合量能、板块及修复形态通过（不等于T-1日线资格）',
}
COOLDOWN = 30
TTL = 120


def db_path(base):
    return Path(base) / 'data/runtime/shadow_research.sqlite3'


def connect(base):
    path = db_path(base)
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path, timeout=2)
    con.row_factory = sqlite3.Row
    con.executescript('''
        CREATE TABLE IF NOT EXISTS events (
            id TEXT PRIMARY KEY, day TEXT, code TEXT, name TEXT, route TEXT,
            version TEXT, at TEXT, quote_at TEXT, price REAL, target REAL,
            invalidation REAL, evidence TEXT, sent_at TEXT, attempted_at TEXT,
            attempts INTEGER DEFAULT 0, delivery_error TEXT);
        CREATE TABLE IF NOT EXISTS samples (
            event_id TEXT, minute TEXT, quote_at TEXT, price REAL, high REAL, low REAL,
            PRIMARY KEY(event_id, minute));
        CREATE TABLE IF NOT EXISTS cycles (
            day TEXT PRIMARY KEY, first_at TEXT, last_at TEXT, cycles INTEGER,
            evaluated INTEGER, capture_errors INTEGER);
    ''')
    return con


def quote_time(q):
    try:
        return datetime.strptime(re.sub(r'\D', '', str(q.get('datetime') or ''))[:14], '%Y%m%d%H%M%S')
    except ValueError:
        return None


def session(now):
    return '09:30' <= now.strftime('%H:%M') <= '11:30' or '13:00' <= now.strftime('%H:%M') <= '15:00'


def safe_shadow(result):
    return (result.get('entry_allowed') is False and result.get('order_action') == 'NO_ORDER'
            and not result.get('capture_error'))


def candidates(signals, quotes, now):
    for signal in signals:
        code = str(signal.get('symbol') or '')
        q = quotes.get(code) or {}
        price = entry_quality.number(q.get('close'))
        if (q.get('code') != code or not price or price <= 0 or not gaps.quote_fresh(q, now)
                or not session(quote_time(q))):
            continue
        timing = signal.get('timing_v2') or {}
        research = timing.get('gap_research') or signal.get('gap_research') or {}
        frozen = research.get('replay') or {}
        routes = []
        if (safe_shadow(research) and research.get('mode') == 'SHADOW_ONLY' and frozen
                and any(r.get('shadow_ready') is True for r in (research.get('routes') or {}).values())):
            try:
                observed = datetime.fromisoformat(frozen['input']['now'])
                valid = (0 <= (now-observed).total_seconds() <= 30
                         and frozen['input']['quote'] == {k: v for k, v in q.items()
                             if k in ('code', 'datetime', 'close', 'pct', '_stale', 'limit_up')})
                if valid and gaps.replay(frozen) == frozen['expected']:
                    for key, result in frozen['expected']['routes'].items():
                        if key in ROUTES and safe_shadow(result) and result.get('shadow_ready') is True:
                            routes.append((key, result, frozen, frozen['evaluator_sha256']))
            except (KeyError, TypeError, ValueError):
                pass
        repair = timing.get('repair_shadow') or signal.get('repair_shadow') or {}
        if (safe_shadow(repair) and repair.get('mode') == 'SHADOW_ONLY'
                and repair.get('shadow_ready') is True and not repair.get('blockers')):
            import hashlib
            from core import repair_observation
            version = hashlib.sha256(Path(repair_observation.__file__).read_bytes()).hexdigest()
            detail = repair.get('method_entry') or {}
            routes.append(('REPAIR_520', {**repair, 'target': detail.get('target'),
                           'invalidation': detail.get('invalidation')},
                           {'repair': repair, 'quote': q, 'contract': signal.get('strategy_contract'),
                            'at': now.isoformat(), 'evaluator_sha256': version}, version))
        for key, result, evidence, version in routes:
            target = entry_quality.number(result.get('target'))
            invalid = entry_quality.number(result.get('invalidation'))
            if target is None or invalid is None or not target > price > invalid > 0:
                continue
            yield {'id': gaps.digest([now.date().isoformat(), code, key, version]),
                   'day': now.date().isoformat(), 'code': code, 'name': signal.get('name') or code,
                   'route': key, 'version': version, 'at': now.isoformat(),
                   'quote_at': quote_time(q).isoformat(), 'price': price,
                   'target': target, 'invalidation': invalid,
                   'evidence': json.dumps(evidence, ensure_ascii=False, allow_nan=False)}


def record_tick(base, signals, quotes, now):
    """One first trigger per stock/method/version/day; sampled prices never fill orders."""
    if not session(now):
        return {'status': 'outside_session', 'new': 0}
    with closing(connect(base)) as con, con:
        evaluated = errors = 0
        for s in signals:
            r = (s.get('timing_v2') or {}).get('gap_research') or s.get('gap_research') or {}
            evaluated += bool(r.get('replay'))
            errors += bool(r.get('capture_error'))
        con.execute('''INSERT INTO cycles VALUES (?, ?, ?, 1, ?, ?)
            ON CONFLICT(day) DO UPDATE SET last_at=excluded.last_at, cycles=cycles+1,
            evaluated=evaluated+excluded.evaluated, capture_errors=capture_errors+excluded.capture_errors''',
            (now.date().isoformat(), now.isoformat(), now.isoformat(), evaluated, errors))
        added = 0
        if now.strftime('%H:%M') < '14:45':
            for item in candidates(signals, quotes, now):
                added += con.execute('''INSERT OR IGNORE INTO events
                    (id,day,code,name,route,version,at,quote_at,price,target,invalidation,evidence)
                    VALUES (:id,:day,:code,:name,:route,:version,:at,:quote_at,:price,:target,:invalidation,:evidence)''', item).rowcount
        for event in con.execute('SELECT * FROM events WHERE day=?', (now.date().isoformat(),)).fetchall():
            q = quotes.get(event['code']) or {}
            price, at = entry_quality.number(q.get('close')), quote_time(q)
            if (q.get('code') != event['code'] or not price or price <= 0 or not gaps.quote_fresh(q, now)
                    or not session(at) or at < datetime.fromisoformat(event['quote_at'])):
                continue
            con.execute('''INSERT INTO samples VALUES (?,?,?,?,?,?)
                ON CONFLICT(event_id,minute) DO UPDATE SET high=MAX(high,excluded.high), low=MIN(low,excluded.low)''',
                (event['id'], at.strftime('%Y-%m-%d %H:%M'), at.isoformat(), price, price, price))
        return {'status': 'ok', 'new': added, 'evaluated': evaluated, 'capture_errors': errors}


def card(events, now):
    def line(e):
        rr = (e['target']-e['price'])/(e['price']-e['invalidation'])
        return (f"**{e['name']}({e['code']})｜{ROUTES[e['route']]}**\n"
                f"- 首次触发 {e['at'][11:19]}｜观察基准价 {e['price']:.2f}\n"
                f"- 研究失效位 {e['invalidation']:.2f}｜研究目标 {e['target']:.2f}｜RR {rr:.2f}\n"
                f"- 研究依据：{BASES[e['route']]}\n"
                f"- 编号 {e['id'][:10]}｜版本 {e['version'][:8]}")
    return {'msg_type': 'interactive', 'card': {
        'config': {'wide_screen_mode': True},
        'header': {'template': 'purple', 'title': {'tag': 'plain_text',
            'content': f'A股影子研究信号｜禁止下单｜{now:%H:%M:%S}'}},
        'elements': [{'tag': 'div', 'text': {'tag': 'lark_md', 'content': content}} for content in (
            '仅影子研究条件通过，未取得正式策略交易授权；不是绿色买入信号，不创建模拟订单。',
            '\n\n'.join(line(e) for e in events),
            '同股同方法同版本每日首次提醒；后续表现于盘后汇总。基准价不是成交价，'
            '日内涨跌只是观察统计，未计费用滑点，不代表T+1可实现收益。')]
    }}


def notify(base, now, sender):
    """Bounded retries, broker acknowledgement required, expired events never backfilled."""
    if not session(now) or now.strftime('%H:%M') >= '14:45' or not db_path(base).exists():
        return {'status': 'outside_window_or_no_ledger', 'sent': 0}
    with closing(connect(base)) as con, con:
        last = con.execute('SELECT MAX(attempted_at) FROM events WHERE day=?', (now.date().isoformat(),)).fetchone()[0]
        if last and (now-datetime.fromisoformat(last)).total_seconds() < COOLDOWN:
            return {'status': 'cooldown', 'sent': 0}
        selected = [dict(r) for r in con.execute('SELECT * FROM events WHERE day=? AND sent_at IS NULL AND attempts<3 ORDER BY at,id',
                    (now.date().isoformat(),))
                    if 0 <= (now-datetime.fromisoformat(r['at'])).total_seconds() <= TTL][:8]
        if not selected:
            return {'status': 'no_fresh_unsent', 'sent': 0}
        for e in selected:
            con.execute('UPDATE events SET attempted_at=?, attempts=attempts+1 WHERE id=?', (now.isoformat(), e['id']))
    try:
        response = sender(card(selected, now))
        response = json.loads(response) if isinstance(response, str) else response
        acknowledged = isinstance(response, dict) and response.get('code', response.get('StatusCode')) in (0, '0')
        error = None if acknowledged else 'feishu_rejected_or_missing_ack'
    except Exception as exc:
        acknowledged, error = False, type(exc).__name__
    with closing(connect(base)) as con, con:
        for e in selected:
            con.execute('UPDATE events SET sent_at=?, delivery_error=? WHERE id=?',
                        (now.isoformat() if acknowledged else None, error, e['id']))
    return {'status': 'sent' if acknowledged else 'delivery_failed',
            'sent': len(selected) if acknowledged else 0, 'error': error}


def review(base, day, quotes, now):
    result = {'day': day, 'mode': 'SHADOW_ONLY', 'order_action': 'NO_ORDER',
              'events': [], 'status': 'not_recorded', 'groups': []}
    path = db_path(base)
    if not path.exists():
        return result
    with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)) as con:
        con.row_factory = sqlite3.Row
        cycle = con.execute('SELECT * FROM cycles WHERE day=?', (day,)).fetchone()
        result['coverage'] = dict(cycle) if cycle else {}
        result['status'] = 'recorded' if cycle else 'not_recorded'
        for raw in con.execute('SELECT * FROM events WHERE day=? AND at<=? ORDER BY at,code,route', (day, now.isoformat())):
            e = {k: raw[k] for k in raw.keys() if k != 'evidence'}
            # A minute's extrema can include later ticks; only use completed sample minutes at an as-of review.
            samples = [dict(r) for r in con.execute('SELECT * FROM samples WHERE event_id=? AND minute<? ORDER BY minute',
                       (e['id'], now.strftime('%Y-%m-%d %H:%M')))]
            q = quotes.get(e['code']) or {}
            at, close = quote_time(q), entry_quality.number(q.get('close'))
            valid_close = (q.get('code') == e['code'] and not q.get('_stale') and close is not None and close > 0
                           and at is not None and at.date().isoformat() == day and '15:00' <= at.strftime('%H:%M') <= '15:30'
                           and at <= now and datetime.fromisoformat(e['at']) <= at)
            e['close'] = close if valid_close else None
            e['close_return_pct'] = (close/e['price']-1)*100 if valid_close else None
            highs = [e['price']] + [s['high'] for s in samples] + ([close] if valid_close else [])
            lows = [e['price']] + [s['low'] for s in samples] + ([close] if valid_close else [])
            e.update(sampled_mfe_pct=(max(highs)/e['price']-1)*100,
                     sampled_mae_pct=(min(lows)/e['price']-1)*100,
                     target_observed=max(highs) >= e['target'], invalidation_observed=min(lows) <= e['invalidation'],
                     sample_minutes=len(samples), last_sample_at=samples[-1]['quote_at'] if samples else None)
            result['events'].append(e)
    for route in ROUTES:
        events = [e for e in result['events'] if e['route'] == route]
        if events:
            returns = [e['close_return_pct'] for e in events if e['close_return_pct'] is not None]
            result['groups'].append({'route': route, 'count': len(events), 'priced': len(returns),
                'positive': sum(v > 0 for v in returns),
                'mean_close_return_pct': sum(returns)/len(returns) if returns else None})
    return result


def text_lines(result, limit=None):
    if result.get('status') == 'error':
        return ['影子研究台账读取失败，不能判定为零信号；正式账户统计不受影响。']
    if result.get('status') != 'recorded':
        return ['当日未采集影子通知台账，不等同于无研究机会；历史回放不补发盘中提醒。']
    events = result['events']
    coverage = result.get('coverage') or {}
    lines = [f"研究事件 {len(events)} 条、{len({e['code'] for e in events})} 只股票；飞书已确认 {sum(bool(e['sent_at']) for e in events)} 条。",
             f"采集区间 {coverage.get('first_at', '')[11:19]} 至 {coverage.get('last_at', '')[11:19]}；"
             f"有效评估 {coverage.get('evaluated', 0)} 次，采集错误 {coverage.get('capture_errors', 0)} 次。",
             '口径：首次触发价至收盘的观察涨跌，不是成交收益；不计费用滑点、不计入模拟账户，不代表T+1可实现收益。',
             '最大涨跌幅仅来自触发后实时采样，可能遗漏极值；目标/失效位触及不等于成交或可卖。']
    fmt = lambda v: '缺失' if v is None else f'{v:+.2f}%'
    for group in result.get('groups', []):
        lines.append(f"{ROUTES[group['route']]}：{group['count']} 条，有效收盘 {group['priced']} 条，"
                     f"正向 {group['positive']} 条，等权平均观察涨跌 {fmt(group['mean_close_return_pct'])}（非组合收益）。")
    for e in events if limit is None else events[:limit]:
        touch = ' / '.join(label for key, label in (('target_observed', '目标已观察触及'),
                          ('invalidation_observed', '失效位已观察触及')) if e[key]) or '未观察到目标/失效位触及'
        delivery = '已提醒' if e['sent_at'] else ('发送失败/未确认' if e['attempts'] else '未发送/已过时')
        lines.append(f"{e['name']}({e['code']})｜{ROUTES[e['route']]}｜{e['at'][11:19]} 基准 {e['price']:.2f}｜"
                     f"收盘观察 {fmt(e['close_return_pct'])}｜采样最大涨/跌 {fmt(e['sampled_mfe_pct'])}/{fmt(e['sampled_mae_pct'])}｜"
                     f"{touch}｜{delivery}｜采样 {e['sample_minutes']} 分钟。")
    if limit is not None and len(events) > limit:
        lines.append(f'其余 {len(events)-limit} 条详见盘后复盘完整表。')
    return lines


def safe_review(base, day, quotes, now):
    try:
        return review(base, day, quotes, now)
    except Exception as exc:
        return {'status': 'error', 'error': type(exc).__name__, 'events': [], 'mode': 'SHADOW_ONLY'}
