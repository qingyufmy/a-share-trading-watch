"""Frozen-contract, closed-bar branch recheck, not an executable backtest."""
import json
import sqlite3
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

BASE = Path.home() / 'Library/Application Support/a-share-trading-watch'
sys.path.insert(0, str(BASE))
from core import intraday_timing_v2 as timing, local_market_data as market, method_entry


def main():
    output = Path(sys.argv[1]) / 'daily_method_recheck.json'
    config = timing.load_config()['daily_method_entry']
    con = sqlite3.connect(f'file:{BASE}/data/runtime/market_data.sqlite?mode=ro', uri=True)
    con.row_factory = sqlite3.Row
    con.execute('BEGIN')
    result = {}
    for day in ['2026-09-09', '2026-09-10']:
        cached = {}
        counts = Counter()
        confirmed = []
        mismatches = []
        reasons = Counter()
        for line in (BASE / f'data/runtime/signal_audit_{day.replace("-", "")}.jsonl').open():
            sample = json.loads(line)
            now = datetime.fromisoformat(sample['timestamp'])
            for row in sample['rows']:
                if row.get('strategy_key') not in {'TREND_520', 'TREND_MA5'} or not row.get('daily_qualified'):
                    continue
                code = row['symbol']
                if code not in cached:
                    cached[code] = [dict(b) for b in con.execute(
                        "SELECT * FROM market_bars WHERE source='tencent' AND timeframe='1m' AND symbol=? AND bar_end>=? AND bar_end<=? ORDER BY bar_end",
                        (code, day+' 09:30:00', day+' 15:00:00'))]
                minute = [b for b in cached[code] if b['bar_end'] <= str(now)]
                bars = timing.closed_bars(market._as_minute_rows(minute), 5, now)
                detail = method_entry.evaluate(row['strategy_contract'], bars, row['price'], now, config)
                counts['evaluations'] += 1
                counts['original_candidates'] += bool(row.get('candidate_pattern'))
                counts['recomputed_candidates'] += bool(detail.get('eligible'))
                item = {'at': str(now), 'symbol': code, 'name': row['name'], 'strategy':row['strategy_key'],
                        'original_candidate':row.get('candidate_pattern'), 'result':detail}
                if detail.get('eligible'):
                    confirmed.append(item)
                if bool(row.get('candidate_pattern')) != bool(detail.get('eligible')):
                    mismatches.append(item)
                reasons.update(detail.get('blockers') or [])
        result[day] = {'counts':counts, 'reasons':reasons, 'confirmations':confirmed, 'mismatches':mismatches}
        print(day,dict(counts),'mismatches',len(mismatches))
    con.close()
    with output.open('x') as stream:
        json.dump({'scope':'Only daily-method branch; archived minute bars may have later fetched_at. Not point-in-time fills or full-engine replay.',
                   'config':config,'days':result}, stream, ensure_ascii=False, indent=2)


if __name__ == '__main__':
    main()
