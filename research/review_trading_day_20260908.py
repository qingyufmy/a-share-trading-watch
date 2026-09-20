"""Read-only decision-log audit and closed-bar method replay for September 8."""
import json
import sqlite3
import sys
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from statistics import median

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = Path.home() / 'Library/Application Support/a-share-trading-watch'
sys.path.insert(0, str(RUNTIME))
from core import intraday_timing_v2 as timing, local_market_data as market, method_entry

DAY = '2026-09-08'
OUT = ROOT / 'output/strategy_review_20260908_153000'


def read(path):
    return json.loads(path.read_text())


def lines(path):
    with path.open() as stream:
        for line in stream:
            try:
                yield json.loads(line)
            except ValueError:
                continue


def db(name):
    con = sqlite3.connect(f'file:{RUNTIME}/data/runtime/{name}.sqlite?mode=ro', uri=True)
    con.row_factory = sqlite3.Row
    con.execute('BEGIN')
    return con


def query(con, sql, params=()):
    return [dict(r) for r in con.execute(sql, params)]


def save(name, value):
    with (OUT / name).open('x', encoding='utf-8') as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2, default=str)


def main():
    OUT.mkdir(exist_ok=False)
    snapshot = read(RUNTIME / 'web_dashboard/data/runtime/latest_signals.json')
    assert snapshot['trading_date'] == DAY
    latest = {r['symbol']: r for r in snapshot['signals']}
    plan = read(RUNTIME / 'web_dashboard/data/reports/premarket_20260908.json')
    logs = [r for r in lines(RUNTIME / 'logs/realtime_signal_engine.log') if r.get('ts', '').startswith(DAY)]
    ticks = [r for r in logs if r.get('event') == 'tick']
    clock = [datetime.fromisoformat(t['ts']) for t in ticks]
    gaps = [(b-a).total_seconds() for a, b in zip(clock, clock[1:]) if (a.hour < 12) == (b.hour < 12)]
    summaries = {}
    samples = []
    qualified_observations = []
    contracts = {}
    for sample in lines(RUNTIME / 'data/runtime/signal_audit_20260908.jsonl'):
        at = sample['timestamp']
        groups = defaultdict(lambda: Counter())
        for row in sample['rows']:
            code = row['symbol']
            contract = row.get('strategy_contract') or {}
            contracts[code] = contract
            agg = summaries.setdefault(code, dict(name=row['name'], samples=0, daily=0, sector=0,
                candidate=0, first=at, last=at, strategies=Counter(), sector_reasons=Counter(), blockers=Counter(),
                first_qualified=None, first_sector=None, min_price=row['price'], max_price=row['price']))
            agg['samples'] += 1
            agg['last'] = at
            agg['min_price'] = min(agg['min_price'], row['price'])
            agg['max_price'] = max(agg['max_price'], row['price'])
            agg['strategies'][row['strategy_key']] += 1
            group = groups[row['strategy_key']]
            group['watched'] += 1
            if row.get('daily_qualified'):
                agg['daily'] += 1
                agg['first_qualified'] = agg['first_qualified'] or at
                group['daily'] += 1
                agg['sector_reasons'][row.get('sector_reason')] += 1
                agg['blockers'].update(row.get('all_blockers') or row.get('blockers') or [])
                if row.get('sector_ok'):
                    agg['sector'] += 1
                    agg['first_sector'] = agg['first_sector'] or at
                    group['sector'] += 1
                if row.get('candidate_pattern'):
                    agg['candidate'] += 1
                    group['candidate'] += 1
                qualified_observations.append({'timestamp': at, **row})
        first = sample['rows'][0].get('structure_freshness') or {}
        breadth = first.get('market_breadth') or {}
        samples.append(dict(timestamp=at, engine_status=sample.get('engine_status'),
                            groups=dict(groups), breadth=breadth,
                            market_state=(first.get('a_share_market_regime') or {}).get('state')))
    for code, agg in summaries.items():
        s = latest.get(code) or {}
        c = contracts[code]
        agg.update(close=s.get('current_price'), pct=s.get('pct'), strategy=c.get('key'),
                   qualified=c.get('daily_qualified'), daily_reason=c.get('daily_gate_reason'),
                   planned_boards=(c.get('daily_metrics') or {}).get('resonance_boards'),
                   last_board=(s.get('sector_momentum') or {}).get('board_name'),
                   last_sector_reason=s.get('sector_resonance_reason'))
    paper = db('paper_trading')
    orders = query(paper, 'SELECT * FROM paper_orders WHERE trading_date=?', (DAY,))
    prior = query(paper, 'SELECT * FROM paper_account_snapshots WHERE trading_date<? ORDER BY snapshot_at DESC LIMIT 1', (DAY,))[0]
    end = query(paper, 'SELECT * FROM paper_account_snapshots WHERE trading_date=? ORDER BY snapshot_at DESC LIMIT 1', (DAY,))[0]
    positions = query(paper, 'SELECT * FROM paper_positions')
    events = db('signal_state')
    signal_events = query(events, 'SELECT scenario,event_type,count(*) AS n FROM signal_events WHERE trading_date=? GROUP BY scenario,event_type', (DAY,))
    market_con = db('market_data')
    bars_by_code = {}
    for code in summaries:
        rows = query(market_con, "SELECT * FROM market_bars WHERE symbol=? AND timeframe='1m' AND source='tencent' AND bar_end>=? AND bar_end<=? ORDER BY bar_end", (code, DAY+' 09:30:00', DAY+' 15:00:00'))
        if rows:
            bars_by_code[code] = rows
    cfg = timing.load_config()['daily_method_entry']
    replay = []
    for obs in qualified_observations:
        if obs['strategy_key'] not in {'TREND_520', 'TREND_MA5'}:
            continue
        at = datetime.fromisoformat(obs['timestamp'])
        raw = [r for r in bars_by_code.get(obs['symbol'], []) if r['bar_end'] <= str(at)]
        bars = timing.closed_bars(market._as_minute_rows(raw), 5, at)
        result = method_entry.evaluate(obs['strategy_contract'], bars, obs['price'], at, cfg)
        replay.append(dict(symbol=obs['symbol'], name=obs['name'], asof=str(at), price=obs['price'],
            strategy=obs['strategy_key'], original_sector_ok=obs['sector_ok'],
            original_candidate=obs.get('candidate_pattern'), original_blockers=obs.get('all_blockers'),
            result=result))
    top = sorted([dict(symbol=code, **agg) for code, agg in summaries.items() if agg['pct'] is not None], key=lambda x: -x['pct'])[:25]
    qualified_codes = [code for code, agg in summaries.items() if agg['daily']]
    summary = dict(date=DAY, snapshot_at=snapshot['updated_at'],
        ticks=dict(count=len(ticks), first=ticks[0]['ts'], last=ticks[-1]['ts'],
                   median_compute=median(t['elapsed'] for t in ticks), max_compute=max(t['elapsed'] for t in ticks),
                   median_cycle=median(gaps), max_cycle=max(gaps),
                   pushes=sum(t.get('pushes', 0) for t in ticks), fills=sum(t.get('paper_fills', 0) for t in ticks),
                   critical_candidates=sum(t.get('critical_watch_candidates', 0) for t in ticks)),
        audit_snapshots=len(samples), audit_rows=sum(s['samples'] for s in summaries.values()),
        unique_watched=len(summaries), unique_qualified=len(qualified_codes),
        qualified_strategies=Counter(summaries[c]['strategy'] for c in qualified_codes),
        all_strategies=Counter(s['strategy'] for s in summaries.values()),
        sector_ever=[dict(symbol=c, **summaries[c]) for c in summaries if summaries[c]['sector']],
        candidate_ever=[dict(symbol=c, **summaries[c]) for c in summaries if summaries[c]['candidate']],
        top25=top, health=snapshot['health'], orders=orders, events=signal_events,
        account=dict(prior=prior, end=end, day_pnl=end['total_assets']-prior['total_assets'], positions=positions),
        operational_events=[r for r in logs if r.get('event') != 'tick'],
        scheduler=[r for r in lines(RUNTIME/'logs/trading_scheduler.log') if r.get('ts','').startswith(DAY)],
        method_replay_count=len(replay), method_confirmations=[r for r in replay if r['result'].get('eligible')],
        minute_coverage=len(bars_by_code), plan_meta={k:plan.get(k) for k in ('generated','published_at','counts')})
    save('summary.json', summary)
    save('symbols.json', summaries)
    save('audit_samples.json', samples)
    save('qualified_observations.json', qualified_observations)
    save('method_replay.json', replay)
    save('contracts.json', contracts)
    save('minute_inputs.json', bars_by_code)
    for con in (paper, events, market_con):
        con.close()
    print(json.dumps({k:v for k,v in summary.items() if k in ('ticks','audit_snapshots','audit_rows','unique_watched','unique_qualified','qualified_strategies','all_strategies','method_replay_count','minute_coverage','account')}, ensure_ascii=False, indent=2))
    print('sector ever', [(r['symbol'],r['name'],r['sector']) for r in summary['sector_ever']])
    print('candidate ever', [(r['symbol'],r['name'],r['candidate']) for r in summary['candidate_ever']])
    print('method confirmations',len(summary['method_confirmations']))
    print('OUTPUT', OUT)


if __name__ == '__main__':
    main()
