"""Read-only frozen-input regression and original intraday audit, never orders/pushes."""
import copy
import argparse
import importlib.util
import json
import sqlite3
import sys
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import after_close_report as base
import intraday_report as intra
import realtime_signal_engine as engine
from core import intraday_timing_v2 as timing
from core import local_market_data as market


def read(path):
    return json.loads(path.read_text(encoding='utf-8'))


def old_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--baseline-dir', type=Path, required=True,
                        help='Immutable pre-change code backup, not the now-updated runtime')
    parser.add_argument('--replay-inputs', type=Path)
    args = parser.parse_args()
    baseline = old_module('baseline_timing_20260907', args.baseline_dir / 'core/intraday_timing_v2.py')
    if args.replay_inputs:
        frozen = read(args.replay_inputs)
        differences = []
        keys = ('entry_allowed', 'entry_pattern', 'candidate_entry_pattern',
                'position_action', 'position_reason', 'exit_reference', 'action')
        for item in frozen['rows']:
            options = {'now': datetime.fromisoformat(frozen['asof']), 'config': frozen['config']}
            before = baseline.evaluate(**copy.deepcopy(item), **options)
            after = timing.evaluate(**copy.deepcopy(item), **options)
            changed = {k: [before.get(k), after.get(k)] for k in keys if before.get(k) != after.get(k)}
            if changed:
                differences.append({'symbol': item['row']['code'], 'changes': changed})
        print(json.dumps({'replayed_symbols': len(frozen['rows']), 'decision_differences': differences}))
        raise SystemExit(bool(differences))
    runtime = Path.home() / 'Library/Application Support/a-share-trading-watch'
    now = datetime.now()
    out = ROOT / 'output' / ('acceptance_intraday_' + now.strftime('%Y%m%d_%H%M%S'))
    out.mkdir(parents=True, exist_ok=False)
    old_intra = old_module('baseline_intra_20260907', args.baseline_dir / 'intraday_report.py')
    snapshot = read(runtime / 'web_dashboard/data/runtime/latest_signals.json')
    quotes = read(runtime / 'data/runtime/quote_cache.json')
    profiles = read(runtime / 'data/runtime/market_profiles.json')['profiles']
    boards, _ = base.fetch_boards()
    config = timing.load_config()
    con = sqlite3.connect(f'file:{runtime}/data/runtime/market_data.sqlite?mode=ro', uri=True)
    con.row_factory = sqlite3.Row
    con.execute('BEGIN')

    def bars(_, code, timeframe, source):
        return [dict(r) for r in con.execute(
            'SELECT source,bar_end,open,high,low,close,volume,amount,fetched_at '
            'FROM market_bars WHERE symbol=? AND timeframe=? AND source=? AND bar_end<=? ORDER BY bar_end',
            (code, timeframe, source, now.strftime('%Y-%m-%d %H:%M:%S')))]

    positions = {p['symbol']: p for p in snapshot['paper_positions']}
    inputs, differences, sectors = [], [], []
    for s in snapshot['signals']:
        if s.get('strategy_family') != 'THREE_METHOD' or s['symbol'] not in quotes:
            continue
        code = s['symbol']
        q = {**profiles.get(code, {}), **quotes[code]}
        with patch.object(market, 'load_bars', side_effect=bars):
            h120, _ = market.history_120m(runtime, code, now)
            h15, _ = market.history_15m(runtime, code, now)
        minute = [b for b in bars(runtime, code, '1m', market.PRIMARY_SOURCE)
                  if b['bar_end'][:10] == now.strftime('%Y-%m-%d')]
        if not minute:
            minute = [b for b in bars(runtime, code, '1m', market.FALLBACK_SOURCE)
                      if b['bar_end'][:10] == now.strftime('%Y-%m-%d')]
        minute = market._as_minute_rows(minute)
        row = {'code': code, 'name': s['name'], 'quote': q,
               'defense': s.get('defense_price'), 'repair': s.get('repair_price'),
               'pressure': s.get('pressure_price'), 'state': s.get('row_state'),
               'rt_features': engine.minute_features(minute, q['close'], now),
               'strategy_contract': s.get('strategy_contract'),
               'sector_momentum': s.get('sector_momentum'), 'sector_rotation': s.get('sector_rotation')}
        md = (s.get('timing_v2', {}).get('data_quality', {}).get('market_data') or {})
        item = dict(row=row, minute_rows=minute, history_120m=h120, history_15m=h15,
                    market_data=md, position=positions.get(code))
        inputs.append(item)
        before = baseline.evaluate(**copy.deepcopy(item), now=now, config=config)
        after = timing.evaluate(**copy.deepcopy(item), now=now, config=config)
        decision_keys = ('entry_allowed', 'entry_pattern', 'candidate_entry_pattern',
                         'position_action', 'position_reason', 'exit_reference', 'action')
        changed = {k: [before.get(k), after.get(k)] for k in decision_keys
                   if before.get(k) != after.get(k)}
        if changed:
            differences.append({'symbol': code, 'changes': changed})
        old_sector = old_intra.sector_momentum_for_candidate(q, boards[:100])
        new_sector = intra.sector_momentum_for_candidate(q, boards)
        if old_sector != new_sector:
            sectors.append({'symbol': code, 'name': s['name'],
                            'daily_qualified': s.get('strategy_daily_qualified'),
                            'before': old_sector, 'after': new_sector,
                            'remaining_timing_blockers': after['blockers']})
    con.close()
    audit_path = runtime / 'data/runtime' / ('signal_audit_' + now.strftime('%Y%m%d') + '.jsonl')
    audit = [json.loads(line) for line in audit_path.read_text().splitlines() if line.strip()]
    orders_con = sqlite3.connect(f'file:{runtime}/data/runtime/paper_trading.sqlite?mode=ro', uri=True)
    orders_con.row_factory = sqlite3.Row
    orders = [dict(r) for r in orders_con.execute(
        'SELECT created_at,symbol,name,scenario,side,status FROM paper_orders WHERE trading_date=?',
        (now.strftime('%Y-%m-%d'),))]
    orders_con.close()
    summary = {'asof': now.isoformat(), 'baseline_code': str(args.baseline_dir), 'data_runtime': str(runtime),
               'tested_symbols': len(inputs), 'decision_differences_same_input': differences,
               'board_coverage': boards[0].get('_coverage') if boards else {},
               'sector_mapping_comparisons': sectors,
               'original_live_pipeline': engine.entry_pipeline_summary(snapshot['signals']),
               'original_live_orders': orders, 'original_audit_snapshots': len(audit),
               'limitations': ['Frozen comparison is not an earlier-session sector replay.',
                               'Current board quotes must not be used as historical evidence.',
                               'No orders, reports, or Feishu messages were generated.']}
    for name, data in [('inputs.json', {'asof': now.isoformat(), 'config': config, 'rows': inputs,
                                       'boards': boards}), ('result.json', summary),
                       ('original_audit.json', audit)]:
        with (out / name).open('x', encoding='utf-8') as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2)
    print(json.dumps({'output': str(out), 'symbols': len(inputs), 'differences': differences,
                      'board_coverage': summary['board_coverage'], 'orders': orders,
                      'pipeline': summary['original_live_pipeline']['passed']}, ensure_ascii=False, indent=2))
    if differences:
        raise SystemExit('Decision regression requires review before deployment')


if __name__ == '__main__':
    main()
