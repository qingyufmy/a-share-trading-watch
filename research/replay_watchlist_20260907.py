"""Read-only sampled replay; no account mutation, order submission or messaging."""
import copy
import argparse
import hashlib
import json
import sqlite3
import sys
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import after_close_report as base
import realtime_signal_engine as engine
from core import intraday_timing_v2 as timing
from core import local_market_data as market
from core import observation_strategy_router as router

DAY = '2026-09-07'
FOCUS = {'301511', '301183'}
RUNTIME = Path.home() / 'Library/Application Support/a-share-trading-watch'
PREVIOUS = ROOT / 'output/iteration_20260907_152336'


def read(path):
    return json.loads(path.read_text())


def save(directory, name, value):
    with (directory / name).open('x') as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2, default=str)


def run():
    parser = argparse.ArgumentParser()
    parser.add_argument('--refresh-structure', action='store_true')
    parser.add_argument('--reclassify-repairs', action='store_true')
    args = parser.parse_args()
    out = ROOT / 'output' / ('watchlist_replay_20260907_' + datetime.now().strftime('%H%M%S'))
    out.mkdir(exist_ok=False)
    frozen = read(ROOT / 'output/acceptance_intraday_20260907_105101/inputs.json')
    templates = {x['row']['code']: x for x in frozen['rows']}
    audit_path = RUNTIME / 'data/runtime/signal_audit_20260907.jsonl'
    audit = [json.loads(line) for line in audit_path.read_text().splitlines() if line.strip()]
    daily = read(PREVIOUS / 'daily_inputs_through_20260904.json')
    base.REPORT_COMPACT_DATE = '20260907'
    focus_classifications = {}
    for code in sorted(FOCUS):
        bars = sorted([b for b in base.fetch_sohu_daily(code) if b['date'] <= '2026-09-04'],
                      key=lambda b: b['date'])
        assert len(bars) >= 60 and bars[-1]['date'] == '2026-09-04', code
        daily[code] = bars
        # Identity fields only: intraday turnover, popularity and price must not enter T-1 classification.
        original_quote = templates[code]['row']['quote']
        quote = {k: original_quote.get(k) for k in ('code', 'name', 'industry', 'concepts')}
        focus_classifications[code] = router.classify_stock({'quote': quote, 'daily': bars})
        print('T-1 classification', code, focus_classifications[code]['key'], flush=True)
    save(out, 'daily_inputs_t1.json', daily)
    save(out, 'focus_classifications.json', focus_classifications)
    all_symbols = {r['symbol'] for sample in audit for r in sample['rows']}
    raw = {}
    with sqlite3.connect(f'file:{RUNTIME}/data/runtime/market_data.sqlite?mode=ro', uri=True) as con:
        con.row_factory = sqlite3.Row
        con.execute('BEGIN')
        for code in sorted(all_symbols):
            for source in (market.PRIMARY_SOURCE, market.FALLBACK_SOURCE):
                bars = [dict(r) for r in con.execute(
                    'SELECT * FROM market_bars WHERE symbol=? AND timeframe=? AND source=? '
                    'AND bar_end>=? AND bar_end<=? ORDER BY bar_end',
                    (code, '1m', source, DAY + ' 09:30:00', DAY + ' 15:00:00'))]
                if bars:
                    raw[code] = bars
                    break
    save(out, 'minute_inputs.json', raw)
    cfg = timing.load_config()
    computed_metrics = {}
    for code, bars in daily.items():
        quote = templates[code]['row']['quote']
        identity = {k: quote.get(k) for k in ('code', 'name', 'industry', 'concepts')}
        computed_metrics[code] = router.classify_stock({'quote': identity, 'daily': bars})['daily_metrics']
    summaries = defaultdict(lambda: {'samples': 0, 'qualified_samples': 0, 'method_confirmations': 0,
                                    'timing_entries': 0, 'technical_checks': 0, 'strategies': Counter(),
                                    'blockers': Counter(), 'missing': Counter()})
    records, focus_timeline = [], []
    for sample in audit:
        at = datetime.fromisoformat(sample['timestamp'])
        for original in sample['rows']:
            code = original['symbol']
            agg = summaries[code]
            agg.update(name=original['name'], last_price=original['price'], last_pct=original['pct'])
            agg['samples'] += 1
            agg['qualified_samples'] += bool(original['daily_qualified'])
            agg['strategies'][original['strategy_key']] += 1
            agg['first_seen'] = agg.get('first_seen', sample['timestamp'])
            if code not in templates or code not in raw:
                agg['missing']['frozen_template_or_minutes'] += 1
                continue
            template = templates[code]
            contract = copy.deepcopy(original.get('strategy_contract') or template['row']['strategy_contract'])
            # Preserve decision-time permission and route. New formulas supply T-1 numeric evidence only.
            contract['key'] = original['strategy_key']
            contract['daily_qualified'] = original['daily_qualified']
            if code in computed_metrics:
                contract['daily_metrics'] = copy.deepcopy(computed_metrics[code])
            if args.reclassify_repairs and code in focus_classifications:
                contract = copy.deepcopy(focus_classifications[code])
            bars = [b for b in raw[code] if b['bar_end'] <= str(at)]
            if not bars:
                agg['missing']['no_available_minutes'] += 1
                continue
            minutes = market._as_minute_rows(bars)
            old_quote = template['row']['quote']
            price = original['price']
            quote = {k: old_quote.get(k) for k in ('code', 'name', 'industry', 'concepts', 'prev_close')}
            quote.update(close=price, pct=original['pct'], open=bars[0]['open'],
                         high=max(b['high'] for b in bars), low=min(b['low'] for b in bars),
                         volume_lot=sum(b['volume'] for b in bars),
                         amount_wan=sum((b['high'] + b['low'] + b['close']) / 3 * b['volume'] / 100 for b in bars))
            board = copy.deepcopy(original.get('board_rotation') or {})
            momentum = copy.deepcopy(original.get('sector_momentum') or {})
            momentum.update(board_name=board.get('board_name'), board_pct=board.get('board_pct'),
                            emotion_ok=bool(board.get('sustained') and board.get('leader_healthy')))
            lock = contract.get('daily_metrics', {}).get('resonance_boards') or []
            lock_status = 'matching_snapshot' if lock and board.get('board_name') in lock else 'unverified'
            if lock and lock_status != 'matching_snapshot':
                momentum['emotion_ok'] = False
                board['sustained'] = False
            # Do not borrow 10:51 dynamic price levels or global context for earlier/later moments.
            row = {'code': code, 'name': original['name'], 'quote': quote, 'strategy_contract': contract,
                   'rt_features': engine.minute_features(minutes, price, at),
                   'sector_momentum': momentum, 'sector_rotation': board}
            h120 = [b for b in template['history_120m'] if str(b['bar_end']) < DAY]
            h15 = [b for b in template['history_15m'] if str(b['bar_end']) < DAY]
            freshness = {}
            if args.refresh_structure:
                h120, freshness = market.merge_completed_120m(h120, bars, at)
            result = timing.evaluate(row, minutes, at, history_120m=h120, history_15m=h15, config=cfg,
                                     market_data={'entry_ready': template['market_data'].get('entry_ready', False)
                                                  and freshness.get('current_session_ready', True),
                                                  'blockers': ['当日闭合120分钟数据缺失'] if freshness.get('current_session_ready') is False else [],
                                                  **freshness})
            method = result.get('method_entry') or {}
            agg['technical_checks'] += 1
            agg['method_confirmations'] += bool(method.get('eligible'))
            agg['timing_entries'] += bool(result['entry_allowed'])
            agg['blockers'].update(result['blockers'])
            record = {'asof': str(at), 'symbol': code, 'name': original['name'], 'price': price,
                      'pct': original['pct'], 'strategy': contract['key'], 'daily_qualified': original['daily_qualified'],
                      'original_sector_ok': original['sector_ok'], 'mainline_snapshot': lock_status,
                      'timing_entry': result['entry_allowed'], 'candidate': result['candidate_entry_pattern'],
                      'regime': result['regime'], 'blockers': result['blockers'], 'method_entry': method,
                      'room_risk': result.get('room_risk'), 'vwap': row['rt_features'].get('vwap'),
                      'volume_1m': row['rt_features']['amount_ratio_1m'], 'volume_5m': row['rt_features']['amount_ratio_5m']}
            record.update(strategy_name=contract.get('name'), repair_shadow=result.get('repair_shadow'), structure_freshness=freshness)
            records.append(record)
            if code in FOCUS:
                focus_timeline.append(record)
        print('replayed', sample['timestamp'], flush=True)
    serial_summary = {code: {**item, 'strategies': dict(item['strategies']), 'blockers': dict(item['blockers']),
                             'missing': dict(item['missing'])} for code, item in summaries.items()}
    result = {'created_at': str(datetime.now()), 'trade_date': DAY, 'audit_sha256': hashlib.sha256(audit_path.read_bytes()).hexdigest(),
              'audit_snapshots': len(audit), 'unique_symbols': len(all_symbols), 'audit_rows': sum(x['samples'] for x in summaries.values()),
              'technical_checks': len(records), 'timing_entries': [r for r in records if r['timing_entry']],
              'method_confirmations': [r for r in records if r['method_entry'].get('eligible')],
              'symbols_without_technical_check': [code for code, s in summaries.items() if not s['technical_checks']],
              'latest_strategy_distribution': dict(Counter(r['strategy_key'] for r in audit[-1]['rows'])),
              'focus': {c: serial_summary[c] for c in sorted(FOCUS)},
              'refresh_structure': args.refresh_structure, 'reclassify_repairs': args.reclassify_repairs,
              'limits': ['Sampled technical replay, not exact tick execution replay.',
                         'Preserves historical strategy qualification; no retroactive permission.',
                         'Uses stored bar-time data, not historical arrival times; minute VWAP/amount are typical-price estimates.',
                         ('Uses T-1 history plus complete current-day 120m sessions.' if args.refresh_structure else 'Uses T-1 structural history only.') + ' Full live market breadth, plan levels, turnover and order book are not reconstructed.',
                         'Timing entries exclude portfolio, mainline completeness, fees, formal contract and simulated account approval.',
                         'No orders, production reports or messages generated; current ledger review deliberately excluded from technical diagnostic.']}
    save(out, 'summary.json', result)
    save(out, 'all_symbols.json', serial_summary)
    save(out, 'sampled_timing_replay.json', records)
    save(out, 'focus_timeline.json', focus_timeline)
    print('OUTPUT', out)
    print(json.dumps({k: v for k, v in result.items() if k not in ('timing_entries', 'method_confirmations', 'focus')}, ensure_ascii=False, indent=2))
    print('TIMING', len(result['timing_entries']), 'METHOD', len(result['method_confirmations']))


if __name__ == '__main__':
    run()
