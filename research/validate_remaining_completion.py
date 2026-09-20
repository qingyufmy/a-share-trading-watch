"""Read-only sensitivity counts; no synthetic orders, pushes or source rewrites."""
import hashlib
import json
import sqlite3
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from core import intraday_timing_v2 as timing, local_market_data as market, method_entry
from core import emotion_leader_pool


def main():
    runtime = Path.home() / 'Library/Application Support/a-share-trading-watch'
    config = timing.load_config()['daily_method_entry']
    variants = {'baseline': config, 'touch_6_bars': {**config, 'touch_lookback_bars': 6},
                'distance_035_atr': {**config, 'max_anchor_distance_atr': .35},
                'distance_065_atr': {**config, 'max_anchor_distance_atr': .65}}
    result = {'scope': 'Counterfactual daily-method sensitivity, not point-in-time or executable backtest',
              'parameters_deployed': False, 'days': {}, 'configs': variants}
    con = sqlite3.connect(f'file:{runtime}/data/runtime/market_data.sqlite?mode=ro', uri=True)
    con.row_factory = sqlite3.Row
    con.execute('BEGIN')
    for day in ('2026-09-09', '2026-09-10'):
        path = runtime / f'data/runtime/signal_audit_{day.replace("-", "")}.jsonl'
        data = path.read_bytes()
        cache, counts, unique, mismatches = {}, {v: Counter() for v in variants}, {v: set() for v in variants}, []
        for line in data.splitlines():
            sample = json.loads(line)
            now = datetime.fromisoformat(sample['timestamp'])
            for row in sample['rows']:
                if row.get('strategy_key') not in {'TREND_520', 'TREND_MA5'} or not row.get('daily_qualified'):
                    continue
                code = row['symbol']
                if code not in cache:
                    cache[code] = [dict(b) for b in con.execute(
                        "SELECT * FROM market_bars WHERE source='tencent' AND timeframe='1m' AND symbol=? AND bar_end>=? AND bar_end<=? ORDER BY bar_end",
                        (code, day + ' 09:30:00', day + ' 15:00:00'))]
                minute = [b for b in cache[code] if b['bar_end'] <= str(now)]
                bars = timing.closed_bars(market._as_minute_rows(minute), 5, now)
                for variant, cfg in variants.items():
                    detail = method_entry.evaluate(row['strategy_contract'], bars, row['price'], now, cfg)
                    counter = counts[variant]
                    counter['evaluations'] += 1
                    eligible = bool(detail.get('eligible'))
                    if eligible:
                        unique[variant].add(code)
                        counter['method_candidates'] += 1
                        quality = (row.get('structure_freshness') or {}).get('preopen_quality_gate') or {}
                        plan = bool(row.get('plan_allowed') and row.get('plan_complete') and quality.get('allowed'))
                        sector = bool(row.get('sector_ok'))
                        volume = bool((row.get('execution_gates') or {}).get('volume_confirmed'))
                        counter['also_sector'] += sector
                        counter['also_sector_and_plan'] += sector and plan
                        counter['also_sector_plan_volume'] += sector and plan and volume
                    if variant == 'baseline' and eligible != bool(row.get('candidate_pattern')):
                        mismatches.append({'symbol': code, 'name': row['name'], 'at': str(now),
                                           'original': bool(row.get('candidate_pattern')), 'recomputed': eligible,
                                           'original_input_available': bool(row.get('method_replay_input')),
                                           'archived_minute_sha256': hashlib.sha256(json.dumps(minute, sort_keys=True).encode()).hexdigest(),
                                           'max_fetched_at': max((str(b.get('fetched_at') or '') for b in minute), default='')})
        result['days'][day] = {'audit_sha256': hashlib.sha256(data).hexdigest(),
                              'variants': {v: {**dict(counts[v]), 'unique_symbols': len(unique[v])} for v in variants},
                              'historical_discrepancies': mismatches}
    con.close()
    pool = emotion_leader_pool.load_latest_snapshot(runtime)
    result['current_pool'] = {k: pool.get(k) for k in ('source_date', 'candidate_count', 'kaipanla')}
    result['limitations'] = ['Only two adjacent sessions; not out-of-sample parameter validation',
                             'Archived minute rows may have been refreshed after original decisions',
                             'Intersections omit other execution gates and are NOT formal signals',
                             'No T+1 executable return estimate; price-limit fills not assumed',
                             'No dated review-tab evidence; intraday-tab source used for next-session crosscheck']
    path = ROOT / 'output' / ('remaining_completion_validation_' + datetime.now().strftime('%Y%m%d_%H%M%S') + '.json')
    with path.open('x') as stream:
        json.dump(result, stream, ensure_ascii=False, indent=2)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    print(path)


if __name__ == '__main__':
    main()
