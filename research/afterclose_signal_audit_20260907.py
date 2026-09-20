"""Read-only Sep 7 audit; production databases use mode=ro, probes use memory."""
import hashlib
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
import paper_trading as paper
from core import intraday_timing_v2 as timing
from core import local_market_data as market
from core import signal_tracking as tracking

DAY = '2026-09-07'


def read_json(path):
    return json.loads(path.read_text(encoding='utf-8'))


def json_lines(path):
    rows = []
    for line in path.read_text(encoding='utf-8').splitlines():
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


def connect(name):
    con = sqlite3.connect(f'file:{RUNTIME}/data/runtime/{name}.sqlite?mode=ro', uri=True)
    con.row_factory = sqlite3.Row
    con.execute('BEGIN')
    return con


def query(con, sql, params=()):
    return [dict(row) for row in con.execute(sql, params)]


def main():
    snapshot_path = RUNTIME / 'web_dashboard/data/runtime/latest_signals.json'
    snapshot = read_json(snapshot_path)
    assert snapshot['trading_date'] == DAY
    audit = json_lines(RUNTIME / 'data/runtime/signal_audit_20260907.jsonl')
    logs = [x for x in json_lines(RUNTIME / 'logs/realtime_signal_engine.log')
            if x.get('ts', '').startswith(DAY)]
    ticks = [x for x in logs if x.get('event') == 'tick']
    scheduler = [x for x in json_lines(RUNTIME / 'logs/trading_scheduler.log')
                 if x.get('ts', '').startswith(DAY) and x.get('event') == 'success']
    closing = {s['symbol']: s for s in snapshot['signals']}
    candidates = defaultdict(list)
    for sample in audit:
        for row in sample['rows']:
            if row.get('daily_qualified') and row.get('sector_ok') and row.get('candidate_pattern'):
                candidates[row['symbol']].append({'timestamp': sample['timestamp'], **row})
    candidate_summary = []
    for code, rows in candidates.items():
        first, last = rows[0], rows[-1]
        close = closing[code]['current_price']
        candidate_summary.append(dict(symbol=code, name=first['name'], strategy=first['strategy_key'],
            first_time=first['timestamp'], last_time=last['timestamp'], snapshots=len(rows),
            first_price=first['price'], close=close,
            close_markout_pct=round((close / first['price'] - 1) * 100, 3),
            first_blockers=first['blockers'], all_snapshots=rows))
    candidate_summary.sort(key=lambda x: x['first_time'])

    con = connect('paper_trading')
    orders = query(con, 'SELECT * FROM paper_orders WHERE trading_date=? ORDER BY created_at', (DAY,))
    previous = query(con, 'SELECT * FROM paper_account_snapshots WHERE trading_date<? ORDER BY snapshot_at DESC LIMIT 1', (DAY,))[0]
    prior_positions = query(con, 'SELECT * FROM paper_position_snapshots WHERE snapshot_at=?', (previous['snapshot_at'],))
    current = query(con, 'SELECT * FROM paper_account_snapshots WHERE trading_date=? ORDER BY snapshot_at DESC LIMIT 1', (DAY,))[0]
    positions = query(con, 'SELECT * FROM paper_position_snapshots WHERE snapshot_at=?', (current['snapshot_at'],))
    expected = {x['symbol']: x['quantity'] for x in prior_positions}
    for order in orders:
        if order['status'] == 'FILLED':
            code = order['symbol']
            expected[code] = expected.get(code, 0) + order['qty'] * (1 if order['side'] == 'BUY' else -1)
    expected_market_value = sum(qty * closing[code]['current_price'] for code, qty in expected.items())
    expected_assets = current['cash'] + expected_market_value
    reconciliation = dict(previous=previous, previous_positions=prior_positions, reported=current,
        reported_positions=positions, expected_quantities_without_external_flow=expected,
        expected_market_value=expected_market_value, expected_assets=expected_assets,
        expected_day_pnl=round(expected_assets - previous['total_assets'], 2),
        reported_day_pnl=round(current['total_assets'] - previous['total_assets'], 2),
        excess_assets=round(current['total_assets'] - expected_assets, 2),
        assumption='No external cash or stock transfer; excludes unmodeled commissions and taxes.')

    # Reproduce on an empty in-memory schema, never on the production connection.
    memory = sqlite3.connect(':memory:')
    schema = con.execute("SELECT sql FROM sqlite_master WHERE name='paper_positions'").fetchone()[0]
    memory.execute(schema)
    now = datetime.fromisoformat(DAY + ' 09:45:32')
    seed = {'000831': {'quantity': 100, 'sellable': 100, 'cost': 56.73}}
    paper.seed_positions(memory, seed, now=now)
    before = paper.get_position(memory, '000831')['quantity']
    paper.update_position_after_fill(memory, {'symbol': '000831'}, 'SELL', 100, 55.86, now)
    after_sell = paper.get_position(memory, '000831')['quantity']
    paper.seed_positions(memory, seed, now=now)
    after_reseed = paper.get_position(memory, '000831')['quantity']
    assert (before, after_sell, after_reseed) == (100, 0, 100)
    reseed_probe = dict(before=before, after_sell=after_sell, after_reseed=after_reseed,
                        bug_reproduced=True, database=':memory:')
    missed_probe = tracking.state_for({'timing_v2': {'trigger_missed': True}},
        {'hard_vetoed': True, 'hard_veto': [{'code': 'RR', 'reason': 'RR below threshold'}]})
    assert missed_probe == 'TRIGGERED_BUT_MISSED'
    state = connect('signal_state')
    events = query(state, 'SELECT scenario,event_type,count(*) AS count FROM signal_events WHERE trading_date=? GROUP BY scenario,event_type', (DAY,))
    missed = query(state, "SELECT symbol,new_state,price,hard_veto_json,gaps_json,created_at FROM signal_track_events WHERE trading_date=? AND new_state='TRIGGERED_BUT_MISSED' AND old_state!=new_state", (DAY,))
    market_con = connect('market_data')
    minute = query(market_con, "SELECT * FROM market_bars WHERE symbol='003040' AND timeframe='1m' AND source='tencent' AND bar_end>=? AND bar_end<=? ORDER BY bar_end", (DAY + ' 09:30:00', DAY + ' 15:00:00'))
    breakout_replay = []
    for hhmm in ['11:00', '11:05', '11:10', '11:15', '11:20', '13:30', '13:35', '13:40', '13:45', '13:50']:
        asof = datetime.fromisoformat(DAY + ' ' + hhmm + ':00')
        available = [b for b in minute if b['bar_end'] <= asof.strftime('%Y-%m-%d %H:%M:%S')]
        bars = timing.closed_bars(market._as_minute_rows(available), 5, asof)
        if len(bars) < 3:
            continue
        prior_high = max(b['high'] for b in bars[-3:-1])
        breakout_replay.append(dict(asof=str(asof), close=bars[-1]['close'], prior_two_high=prior_high,
            breakout=bars[-1]['close'] >= prior_high and bars[-1]['close'] > bars[-2]['close']))
    top = sorted(snapshot['signals'], key=lambda s: s.get('pct') or 0, reverse=True)[:20]
    top_rows = [dict(symbol=s['symbol'], name=s['name'], pct=s['pct'], strategy=s.get('strategy_key'),
                    daily=s.get('strategy_daily_qualified'), evidence=s.get('strategy_daily_evidence')) for s in top]
    group_counts = Counter(s.get('strategy_key') for s in snapshot['signals'] if s.get('strategy_daily_qualified'))
    qualified = [s for s in snapshot['signals'] if s.get('strategy_daily_qualified')]
    sector_rows = [dict(symbol=s['symbol'], name=s['name'], reason=s.get('sector_resonance_reason'))
                   for s in qualified if s.get('sector_resonance_ok')]
    tick_times = [datetime.fromisoformat(t['ts']) for t in ticks]
    gaps = [(b - a).total_seconds() for a, b in zip(tick_times, tick_times[1:]) if a.hour < 12 and b.hour < 12 or a.hour >= 13 and b.hour >= 13]
    result = dict(date=DAY, captured_at=str(datetime.now()), snapshot_at=snapshot['updated_at'],
        source_sha256=hashlib.sha256(snapshot_path.read_bytes()).hexdigest(),
        health=snapshot['health'], ticks=dict(count=len(ticks), first=ticks[0]['ts'], last=ticks[-1]['ts'],
            median_compute_seconds=median(t['elapsed'] for t in ticks), max_compute_seconds=max(t['elapsed'] for t in ticks),
            median_cycle_seconds=median(gaps), max_session_cycle_seconds=max(gaps),
            pushes=sum(t.get('pushes', 0) for t in ticks), fills=sum(t.get('paper_fills', 0) for t in ticks),
            yellow_candidates=sum(t.get('critical_watch_candidates', 0) for t in ticks)),
        errors=[x for x in logs if 'error' in x.get('event', '')],
        trade_push_ticks=[t for t in ticks if t.get('paper_fills')], scheduler=scheduler,
        strategy_counts=group_counts, qualifying_sector_reasons=sector_rows,
        audit_snapshots=len(audit), audit_buckets=[a['bucket'] for a in audit],
        candidates=candidate_summary, top20=top_rows,
        top20_daily_qualified=sum(bool(s['daily']) for s in top_rows),
        orders=orders, events=events, missed_events=missed, reconciliation=reconciliation,
        probes=dict(reseed=reseed_probe, missed_state_with_other_veto=missed_probe),
        leader_5m_replay=breakout_replay,
        replay_limitations=['Snapshot audit is not a tick-exact historical strategy backtest.',
            'Minute replay uses locally stored bars and does not prove historical arrival time.',
            'Close mark-outs are observation diagnostics, not executable T+1 profits or win rates.'])
    out = ROOT / 'output' / ('afterclose_audit_20260907_' + datetime.now().strftime('%H%M%S'))
    out.mkdir(parents=True, exist_ok=False)
    with (out / 'evidence.json').open('x', encoding='utf-8') as handle:
        json.dump(result, handle, ensure_ascii=False, indent=2)
    print(json.dumps(dict(output=str(out), ticks=result['ticks'], strategy_counts=group_counts,
        candidates=[{k: v for k, v in c.items() if k != 'all_snapshots'} for c in candidate_summary],
        top20_daily_qualified=result['top20_daily_qualified'], reconciliation=reconciliation,
        probes=result['probes'], breakout_replay=breakout_replay,
        missed_symbols=sorted({r['symbol'] for r in missed})), ensure_ascii=False, indent=2))
    for connection in (memory, con, state, market_con):
        connection.close()


if __name__ == '__main__':
    main()
