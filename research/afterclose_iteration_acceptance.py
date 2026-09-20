"""Sep 7 read-only input replay. Never submits orders or sends notifications."""
import copy
import argparse
import hashlib
import importlib.util
import json
import sqlite3
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import after_close_report as base
from core import intraday_timing_v2 as timing, method_entry, observation_strategy_router as router
from core import local_market_data as market

OUT = ROOT / 'output/iteration_20260907_152336'
RUNTIME = Path.home() / 'Library/Application Support/a-share-trading-watch'
DAY = '2026-09-07'


def read(path):
    return json.loads(path.read_text(encoding='utf-8'))


def save(name, value):
    with (OUT / name).open('x', encoding='utf-8') as f:
        json.dump(value, f, ensure_ascii=False, indent=2, default=str)


def main():
    global OUT
    baseline_dir = OUT
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    if args.output:
        OUT = args.output
        OUT.mkdir(parents=True, exist_ok=False)
    frozen_path = ROOT / 'output/acceptance_intraday_20260907_105101/inputs.json'
    frozen = read(frozen_path)
    spec = importlib.util.spec_from_file_location('before_iteration_timing', baseline_dir / 'backup/source/core/intraday_timing_v2.py')
    before_module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = before_module
    spec.loader.exec_module(before_module)
    daily_path = baseline_dir / 'daily_inputs_through_20260904.json'
    if daily_path.exists():
        daily_inputs = read(daily_path)
    else:
        daily_inputs = {}
        base.REPORT_COMPACT_DATE = '20260907'
        for item in frozen['rows']:
            c = item['row']['strategy_contract']
            if c.get('daily_qualified') and c.get('key') in ('TREND_520', 'TREND_MA5'):
                code = item['row']['code']
                rows = sorted([b for b in base.fetch_sohu_daily(code) if b.get('date') and b['date'] <= '2026-09-04'], key=lambda b: b['date'])
                assert rows and rows[-1]['date'] == '2026-09-04', code
                daily_inputs[code] = rows
                print('T-1 verified', code, len(rows), flush=True)
        save(daily_path.name, daily_inputs)
    contracts, comparisons, exit_changes = {}, [], []
    now = datetime.fromisoformat(frozen['asof'])
    cfg = timing.load_config()
    for item in frozen['rows']:
        code = item['row']['code']
        before = before_module.evaluate(**copy.deepcopy(item), now=now, config=frozen['config'])
        updated = copy.deepcopy(item)
        c = updated['row']['strategy_contract']
        if code in daily_inputs:
            computed = router.classify_stock({'quote': updated['row']['quote'], 'daily': daily_inputs[code]})
            c['daily_metrics'] = computed['daily_metrics']
            # Qualification/key remain the original decision-time contract.
            contracts[code] = copy.deepcopy(c)
        after = timing.evaluate(**updated, now=now, config=cfg)
        changed_exit = {k: [before.get(k), after.get(k)] for k in ('position_action', 'position_reason', 'exit_reference')
                        if before.get(k) != after.get(k)}
        if changed_exit:
            exit_changes.append({'symbol': code, 'changes': changed_exit})
        comparisons.append({'symbol': code, 'name': item['row']['name'], 'strategy': c['key'],
                            'before_timing_entry': before['entry_allowed'], 'after_timing_entry': after['entry_allowed'],
                            'before_candidate': before['candidate_entry_pattern'], 'after_candidate': after['candidate_entry_pattern'],
                            'before_blockers': before['blockers'], 'after_blockers': after['blockers'],
                            'method_entry': after.get('method_entry'), 'room_risk': after.get('room_risk')})
    assert not exit_changes, exit_changes
    con = sqlite3.connect(f'file:{RUNTIME}/data/runtime/market_data.sqlite?mode=ro', uri=True)
    con.row_factory = sqlite3.Row
    con.execute('BEGIN')
    raw = {}
    for code in set(contracts) | {'003040'}:
        source = market.PRIMARY_SOURCE
        rows = [dict(r) for r in con.execute(
            'SELECT * FROM market_bars WHERE symbol=? AND timeframe=? AND source=? AND bar_end>=? AND bar_end<=? ORDER BY bar_end',
            (code, '1m', source, DAY+' 09:30:00', DAY+' 15:00:00'))]
        if not rows:
            rows = [dict(r) for r in con.execute(
                'SELECT * FROM market_bars WHERE symbol=? AND timeframe=? AND source=? AND bar_end>=? AND bar_end<=? ORDER BY bar_end',
                (code, '1m', market.FALLBACK_SOURCE, DAY+' 09:30:00', DAY+' 15:00:00'))]
        raw[code] = rows
    con.close()
    audit_path = RUNTIME / 'data/runtime/signal_audit_20260907.jsonl'
    audit = [json.loads(line) for line in audit_path.read_text().splitlines() if line.strip()]
    decisions, leader = [], []
    for sample in audit:
        at = datetime.fromisoformat(sample['timestamp'])
        for original in sample['rows']:
            code = original['symbol']
            if code not in raw:
                continue
            available = [b for b in raw[code] if b['bar_end'] <= str(at)]
            b5 = timing.closed_bars(market._as_minute_rows(available), 5, at)
            if code == '003040' and len(b5) >= 3:
                old_breakout = b5[-1]['close'] >= max(b['high'] for b in b5[-3:-1]) and b5[-1]['close'] > b5[-2]['close']
                ev = timing.recent_breakout_evidence(b5, original['price'], timing._atr(b5, 14) or .05)
                leader.append({'asof': str(at), 'price': original['price'], 'old_latest_breakout': old_breakout,
                               'new_breakout': ev, 'original_blockers': original['blockers']})
            if code in contracts:
                detail = method_entry.evaluate(contracts[code], b5, original['price'], at, cfg.get('daily_method_entry') or {})
                decisions.append({'asof': str(at), 'symbol': code, 'name': original['name'], 'method': contracts[code]['key'],
                                  'price': original['price'], 'original_daily': original['daily_qualified'],
                                  'original_sector_ok': original['sector_ok'], 'original_sector_reason': original['sector_reason'],
                                  'original_candidate': original['candidate_pattern'], 'native_method': detail})
    result = {'frozen_asof': frozen['asof'], 'frozen_sha256': hashlib.sha256(frozen_path.read_bytes()).hexdigest(),
              'frozen_symbols': len(comparisons), 'exit_changes': exit_changes,
              'before_timing_entries': sum(x['before_timing_entry'] for x in comparisons),
              'after_timing_entries': sum(x['after_timing_entry'] for x in comparisons),
              'daily_qualified_methods': len(contracts), 'daily_input_cutoff': '2026-09-04',
              'audit_snapshots': len(audit), 'native_method_checks': len(decisions),
              'native_confirmations': sum(x['native_method']['eligible'] for x in decisions),
              'native_confirmations_by_symbol': dict(Counter(x['symbol'] for x in decisions if x['native_method']['eligible'])),
              'retained_e5_confirmations': [x for x in leader if not x['old_latest_breakout'] and x['new_breakout']['confirmed']],
              'limitations': ['Method confirmations are not formal signals or simulated fills.',
                              'Intraday audit is sampled; stored bars may have arrived later than bar time.',
                              'No historical order book, live sector snapshot reconstruction or cost-adjusted T+1 return is asserted.',
                              'No production database, report, or Feishu message was modified.']}
    save('replay_inputs_minutes.json', raw)
    save('frozen_comparisons.json', comparisons)
    save('native_day_replay.json', decisions)
    save('leader_day_replay.json', leader)
    save('replay_result.json', result)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
