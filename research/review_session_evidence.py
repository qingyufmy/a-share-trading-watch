"""Read-only session funnel review; output only to a new research directory."""
import argparse
from collections import Counter, defaultdict
from datetime import datetime
import hashlib
import json
from pathlib import Path
import sqlite3
from statistics import median
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from research.verify_frozen_method_replay import validate


def records(path):
    with path.open() as stream:
        for line in stream:
            yield json.loads(line)


def review(base, day):
    audit = base / f'data/runtime/signal_audit_{day.replace("-", "")}.jsonl'
    symbols = {}
    snapshots, candidates, sector_nodes = [], [], []
    group = defaultdict(Counter)
    sets = defaultdict(lambda: defaultdict(set))
    for sample in records(audit):
        at = sample['timestamp']
        daily_rows = [r for r in sample['rows'] if r.get('daily_qualified')]
        snapshots.append({'at': at, 'recorded_at': sample.get('decision_recorded_at'),
            'engine_status': sample.get('engine_status'), 'pipeline': sample.get('entry_pipeline'),
            'breadth': (sample.get('market_state') or {}).get('breadth'),
            'market_state': (sample.get('market_state') or {}).get('state'),
            'quality': sample.get('preopen_quality_gate'), 'leader_pool': sample.get('leader_pool'),
            'global_veto_daily_rows': sum(any('CORE_GLOBAL_RISK_BLOCKED' in s for s in r.get('all_blockers', []))
                                          for r in daily_rows), 'daily_rows': len(daily_rows),
            'strong_boards': [b for b in sample.get('board_snapshot', []) if (b.get('pct') or 0) >= .8]})
        for row in sample['rows']:
            key, code = row.get('strategy_key'), row['symbol']
            daily, sector, setup = bool(row.get('daily_qualified')), bool(row.get('sector_ok')), bool(row.get('candidate_pattern'))
            flags = {'watched': True, 'daily': daily, 'sector': sector, 'candidate': setup,
                     'daily_sector': daily and sector, 'daily_sector_candidate': daily and sector and setup}
            for flag, enabled in flags.items():
                if enabled:
                    group[key][flag] += 1
                    sets[key][flag].add(code)
            entry = symbols.setdefault(code, {'name': row['name'], 'strategies': Counter(), 'counts': Counter(),
                'boards': Counter(), 'blockers': Counter(), 'method_blockers': Counter(), 'route_reasons': defaultdict(Counter),
                'first': at, 'first_price': row.get('price'), 'first_pct': row.get('pct'), 'first_contract': row.get('strategy_contract')})
            entry['strategies'][key] += 1
            entry['counts'].update([flag for flag, enabled in flags.items() if enabled])
            entry['boards'][(row.get('sector_momentum') or {}).get('board_name', '')] += 1
            entry.update(last=at, last_price=row.get('price'), last_pct=row.get('pct'), last_contract=row.get('strategy_contract'))
            if daily:
                entry['blockers'].update(row.get('all_blockers') or [])
                entry['method_blockers'].update(((row.get('method_replay_input') or {}).get('expected') or {}).get('blockers') or [])
                for route, data in (row.get('leader_routes') or {}).items():
                    entry['route_reasons'][route][data.get('reason')] += 1
            if setup or daily and sector:
                node = {'at': at, **{k:v for k,v in row.items() if k not in ('method_replay_input',)}}
                if setup:
                    candidates.append(node)
                if daily and sector:
                    sector_nodes.append(node)
    engine_rows = [r for r in records(base/'logs/realtime_signal_engine.log') if str(r.get('ts','')).startswith(day)]
    ticks = [r for r in engine_rows if r.get('event') == 'tick']
    times = [datetime.fromisoformat(r['ts']) for r in ticks]
    gaps = [(b-a).total_seconds() for a,b in zip(times,times[1:]) if (a.hour < 12) == (b.hour < 12)]
    scheduler = []
    for row in records(base/'logs/trading_scheduler.log'):
        if not str(row.get('ts','')).startswith(day) or row.get('event') not in ('child_finished','error','failed'):
            continue
        result = row.get('report_result') or {}
        scheduler.append({k:row.get(k) for k in ('ts','event','mode','attempt','elapsed','returncode','error')})
        scheduler[-1].update(report=result.get('report'), delivery=result.get('feishu_result'))
    def query(db, sql, params=()):
        with sqlite3.connect(f'file:{base}/data/runtime/{db}.sqlite?mode=ro',uri=True) as con:
            con.row_factory = sqlite3.Row
            return [dict(r) for r in con.execute(sql, params)]
    return {'date': day, 'collected_at': str(datetime.now()), 'snapshots': snapshots, 'symbols': symbols,
        'strategies': {k:{'records': group[k], 'unique':{f:len(v) for f,v in s.items()}} for k,s in sets.items()},
        'candidates': candidates, 'sector_nodes': sector_nodes, 'scheduler': scheduler,
        'engine': {'ticks':len(ticks), 'first':times[0].isoformat() if times else None, 'last':times[-1].isoformat() if times else None,
                   'median_cycle_seconds':median(gaps) if gaps else None, 'max_cycle_seconds':max(gaps) if gaps else None,
                   'median_compute_seconds':median(r['elapsed'] for r in ticks) if ticks else None,
                   'pushes':sum(r.get('pushes',0) for r in ticks),'fills':sum(r.get('paper_fills',0) for r in ticks),
                   'events':Counter(r.get('event') for r in engine_rows),
                   'critical_ticks':[{k:r.get(k) for k in ('ts','critical_watch_candidates','critical_watch_feishu_result')} for r in ticks if r.get('critical_watch_candidates')],
                   'errors':[r for r in engine_rows if r.get('event') in ('error','failed')]},
        'orders':query('paper_trading','SELECT created_at,symbol,side,status,qty,fill_price,fees_total FROM paper_orders WHERE trading_date=?',(day,)),
        'accounts':query('paper_trading','SELECT * FROM paper_account_snapshots WHERE trading_date=? ORDER BY snapshot_at DESC LIMIT 1',(day,)),
        'prior_account':query('paper_trading','SELECT * FROM paper_account_snapshots WHERE trading_date<? ORDER BY snapshot_at DESC LIMIT 1',(day,)),
        'events':query('signal_state','SELECT scenario,event_type,COUNT(*) AS n FROM signal_events WHERE trading_date=? GROUP BY scenario,event_type',(day,)),
        'frozen_replay':validate(audit)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--date',required=True)
    parser.add_argument('--base-dir',type=Path,default=Path.home()/'Library/Application Support/a-share-trading-watch')
    args=parser.parse_args()
    datetime.strptime(args.date,'%Y-%m-%d')
    output=ROOT/'output'/('session_review_'+args.date.replace('-','')+'_'+datetime.now().strftime('%H%M%S'))
    output.mkdir(exist_ok=False)
    result=review(args.base_dir,args.date)
    with (output/'evidence.json').open('x') as f:
        json.dump(result,f,ensure_ascii=False,indent=2)
    print(json.dumps({k:result[k] for k in ('strategies','scheduler','engine','orders','accounts','events','frozen_replay')},ensure_ascii=False,indent=2))
    print('OUTPUT',output)


if __name__ == '__main__':
    main()
