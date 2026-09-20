"""Read-only two-session evidence audit; writes only a new research directory."""
import json
import sqlite3
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from statistics import median

BASE = Path.home() / 'Library/Application Support/a-share-trading-watch'
OUT = Path(__file__).resolve().parents[1] / 'output' / ('review_20260909_10_' + datetime.now().strftime('%H%M%S'))


def records(path):
    with path.open() as stream:
        for number, line in enumerate(stream, 1):
            try:
                yield json.loads(line)
            except ValueError:
                errors.append({'file': str(path), 'line': number})


def query(database, sql, params=()):
    with sqlite3.connect(f'file:{BASE}/data/runtime/{database}.sqlite?mode=ro', uri=True) as con:
        con.row_factory = sqlite3.Row
        return [dict(r) for r in con.execute(sql, params)]


def main():
    OUT.mkdir(exist_ok=False)
    scheduler = list(records(BASE / 'logs/trading_scheduler.log'))
    engine = list(records(BASE / 'logs/realtime_signal_engine.log'))
    result = {}
    for day in ['2026-09-09', '2026-09-10']:
        compact = day.replace('-', '')
        symbols = {}
        snapshots = []
        opportunities = []
        groups = defaultdict(Counter)
        group_sets = defaultdict(lambda: defaultdict(set))
        for sample in records(BASE / f'data/runtime/signal_audit_{compact}.jsonl'):
            rows = sample.get('rows', [])
            if not rows:
                continue
            at = sample['timestamp']
            first = rows[0].get('structure_freshness') or {}
            snapshots.append({'at': at, 'status': sample.get('engine_status'), 'count': len(rows),
                'pipeline': sample.get('entry_pipeline'), 'market': sample.get('market_state'),
                'breadth': first.get('market_breadth'), 'preopen': first.get('preopen_quality_gate'),
                'board_coverage': (sample.get('board_snapshot') or [{}])[0].get('coverage')})
            for row in rows:
                code = row['symbol']; key = row.get('strategy_key', 'UNKNOWN')
                c = row.get('strategy_contract') or {}
                blockers = row.get('all_blockers') or row.get('blockers') or []
                z = symbols.setdefault(code, {'name': row['name'], 'samples': 0, 'strategies': Counter(),
                    'daily': 0, 'sector': 0, 'candidate': 0, 'daily_sector': 0, 'daily_sector_candidate': 0,
                    'blockers': Counter(), 'sector_reasons': Counter(), 'route_reasons': defaultdict(Counter),
                    'first': at, 'first_price': row.get('price'), 'first_pct': row.get('pct'),
                    'first_contract': c, 'first_qualified': None, 'first_candidate': None})
                z['samples'] += 1; z['strategies'][key] += 1
                z.update(last=at, last_price=row.get('price'), last_pct=row.get('pct'), last_contract=c)
                group_sets[key]['watched'].add(code)
                flags = {'daily': bool(row.get('daily_qualified')), 'sector': bool(row.get('sector_ok')),
                         'candidate': bool(row.get('candidate_pattern'))}
                flags['daily_sector'] = flags['daily'] and flags['sector']
                flags['daily_sector_candidate'] = flags['daily_sector'] and flags['candidate']
                for k, v in flags.items():
                    if v:
                        z[k] += 1; groups[key][k] += 1; group_sets[key][k].add(code)
                if flags['daily']:
                    z['first_qualified'] = z['first_qualified'] or at
                    z['blockers'].update(blockers); z['sector_reasons'][row.get('sector_reason')] += 1
                    for route, data in (row.get('leader_routes') or {}).items():
                        z['route_reasons'][route][data.get('reason')] += 1
                if flags['candidate']:
                    z['first_candidate'] = z['first_candidate'] or at
                    opportunities.append({'at': at, **{k: row.get(k) for k in ['symbol', 'name', 'price', 'pct',
                        'strategy_key', 'daily_qualified', 'sector_ok', 'candidate_pattern', 'scenario', 'room_atr',
                        'reward_risk', 'plan_allowed', 'plan_complete']}, 'blockers': blockers,
                        'preopen': (row.get('structure_freshness') or {}).get('preopen_quality_gate')})
        logs = [r for r in engine if r.get('ts', '').startswith(day)]
        ticks = [r for r in logs if r.get('event') == 'tick']
        times = [datetime.fromisoformat(r['ts']) for r in ticks]
        gaps = [{'from': str(a), 'to': str(b), 'seconds': (b-a).total_seconds()} for a, b in zip(times, times[1:])
                if (a.hour < 12) == (b.hour < 12)]
        costs = [r['elapsed'] for r in ticks if isinstance(r.get('elapsed'), (float, int))]
        sched = []
        for r in scheduler:
            if not r.get('ts', '').startswith(day):
                continue
            item = {k:r[k] for k in ['ts','event','mode','resolved_mode','returncode','error','reason'] if k in r}
            rr = r.get('report_result') or {}
            if not rr:
                try:
                    rr = json.loads(r.get('stdout', ''))
                except ValueError:
                    rr = {}
            item['result'] = {k:rr[k] for k in ['report','feishu_result','status'] if k in rr}
            if r.get('stderr'):
                item['error_tail'] = r['stderr'][-700:]
            sched.append(item)
        orders = query('paper_trading', 'SELECT * FROM paper_orders WHERE trading_date=? ORDER BY created_at', (day,))
        for o in orders:
            payload = json.loads(o.pop('signal_payload_json') or '{}')
            o['strategy'] = payload.get('strategy_contract', {}).get('key')
            for k in ['discipline_check_json','strategy_rationale_json']:
                o[k] = json.loads(o[k]) if o.get(k) else None
        accounts = query('paper_trading', 'SELECT * FROM paper_account_snapshots WHERE trading_date=? ORDER BY snapshot_at', (day,))
        prior = query('paper_trading', 'SELECT * FROM paper_account_snapshots WHERE trading_date<? ORDER BY snapshot_at DESC LIMIT 1', (day,))
        positions = query('paper_trading', 'SELECT * FROM paper_position_snapshots WHERE snapshot_at=(SELECT MAX(snapshot_at) FROM paper_position_snapshots WHERE trading_date=?)', (day,))
        qcpath = BASE / f'data/runtime/preopen_quality/preopen_quality_{compact}.json'
        qc = json.loads(qcpath.read_text()) if qcpath.exists() else {}
        events = query('signal_state', 'SELECT scenario,event_type,COUNT(*) AS n FROM signal_events WHERE trading_date=? GROUP BY scenario,event_type', (day,))
        pushes = query('signal_state', 'SELECT * FROM signal_events WHERE trading_date=? AND event_type LIKE ? ORDER BY created_at', (day, '%push%'))
        for p in pushes:
            p.pop('payload_json', None)
        r = dict(symbols=symbols, snapshots=snapshots, opportunities=opportunities,
            strategies={k:{'observations':groups[k], 'unique':{f:len(v) for f,v in s.items()}} for k,s in group_sets.items()},
            ticks={'count':len(ticks),'first':ticks[0]['ts'] if ticks else None,'last':ticks[-1]['ts'] if ticks else None,
                'median_seconds':median(costs) if costs else None,'max_seconds':max(costs) if costs else None,
                'median_cycle':median(g['seconds'] for g in gaps) if gaps else None,
                'gaps_over_180s':[g for g in gaps if g['seconds']>180],
                'pushes':sum(t.get('pushes',0) for t in ticks),'paper_fills':sum(t.get('paper_fills',0) for t in ticks)},
            engine_events=[r for r in logs if r.get('event') not in ['tick','idle','preopen_monitoring']],
            scheduler=sched, orders=orders, accounts={'prior':prior, 'first':accounts[:1], 'last':accounts[-1:], 'count':len(accounts)},
            positions=positions, signal_events=events, push_events=pushes, quality=qc)
        result[day] = r
        print(day,json.dumps({k:r[k] for k in ['strategies','ticks','accounts','positions','orders','signal_events']},ensure_ascii=False,default=list))
    with (OUT / 'audit.json').open('x') as stream:
        json.dump({'collected_at':str(datetime.now()),'days':result,'parse_errors':errors}, stream, ensure_ascii=False, indent=2)
    print('OUTPUT', OUT)


errors = []
if __name__ == '__main__':
    main()
