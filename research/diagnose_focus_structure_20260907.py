"""Isolated counterfactual for closed-120m freshness; never changes live policy."""
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
from datetime import datetime

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from core import intraday_timing_v2 as timing, local_market_data as market

OUT = ROOT / 'output/watchlist_replay_20260907_171920'
raw = json.loads((OUT / 'minute_inputs.json').read_text())
frozen = json.loads((ROOT / 'output/acceptance_intraday_20260907_105101/inputs.json').read_text())
templates = {x['row']['code']: x for x in frozen['rows']}
checks = []
for code in ('301511', '301183'):
    history = templates[code]['history_120m']
    for clock in ('11:30:10', '15:00:20'):
        at = datetime.fromisoformat('2026-09-07 ' + clock)
        bars = timing.closed_bars(market._as_minute_rows([b for b in raw[code] if b['bar_end'] <= str(at)]), 120, at)
        assert all(datetime.fromisoformat(b['bar_end']) <= at for b in bars)
        before = timing._full_regime(history, timing.load_config())
        after = timing._full_regime((history + bars)[-240:], timing.load_config())
        checks.append({'symbol': code, 'asof': str(at), 'original_last_120m': history[-1]['bar_end'],
                       'before': before, 'after_appending_completed_bars': after, 'bars_appended': bars,
                       'historical_daily_permission': False,
                       'note': 'Minute-aggregate diagnostic only; unchanged daily eligibility still blocks entry.'})
runtime = Path.home() / 'Library/Application Support/a-share-trading-watch'
with sqlite3.connect(f'file:{runtime}/data/runtime/market_data.sqlite?mode=ro', uri=True) as con:
    dates = con.execute('SELECT symbol,source,MAX(bar_end) FROM market_bars WHERE timeframe=? '
                        'AND symbol IN (?,?) GROUP BY symbol,source', ('60m', '301511', '301183')).fetchall()
files = [x['file'] for x in json.loads((ROOT / 'output/iteration_20260907_152336/deployment.json').read_text())['files']]
digests = {f: hashlib.sha256((ROOT / f).read_bytes()).hexdigest() for f in files}
accepted = json.loads((OUT / 'summary.json').read_text())
assert len(checks) == 4
assert checks[0]['before'][0] == 'BEAR' and checks[0]['after_appending_completed_bars'][0] == 'REPAIR'
assert accepted['technical_checks'] == 8795 and not accepted['timing_entries']
assert all(x['qualified_samples'] == 0 and x['technical_checks'] == 51 for x in accepted['focus'].values())
with (OUT / 'structure_refresh_diagnostic.json').open('x') as handle:
    json.dump({'checks': checks, 'cached_60m_latest': dates, 'source_sha256': digests,
               'validation_assertions_passed': True, 'production_modified': False}, handle, ensure_ascii=False, indent=2)
print(json.dumps({'cached_60m_latest': dates,
                  'transitions': [(x['symbol'], x['asof'], x['before'][0], x['after_appending_completed_bars'][0]) for x in checks],
                  'validation_assertions_passed': True}, ensure_ascii=False, indent=2))
