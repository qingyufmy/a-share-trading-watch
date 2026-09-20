"""Frozen-input comparison. Research only; never calls orders or messaging."""
import argparse
import copy
import hashlib
import importlib.util
import json
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from core import intraday_timing_v2 as timing, observation_strategy_router as router, local_market_data as market
import realtime_signal_engine as engine

RUNTIME = Path.home() / 'Library/Application Support/a-share-trading-watch'
FROZEN = ROOT / 'output/watchlist_replay_20260907_173811'
DAY = '2026-09-07'


def read(path):
    return json.loads(path.read_text())


def save(out, name, value):
    with (out / name).open('x', encoding='utf-8') as f:
        json.dump(value, f, ensure_ascii=False, indent=2, default=str)


def load_old(out, name):
    spec = importlib.util.spec_from_file_location('baseline_' + name, out / 'backup/source/core' / (name + '.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def variants(cfg):
    result = {}
    for name in ('daily_atr', 'touch_30m', 'late_1455', 'volume_5m', 'leader_gap', 'leader_window'):
        c = copy.deepcopy(cfg)
        if name == 'daily_atr':
            for key in ('TREND_MA5', 'TREND_520'):
                c['method_execution'][key]['vwap_distance_period'] = '1d'
        elif name == 'touch_30m':
            c['daily_method_entry']['touch_lookback_bars'] = 6
        elif name == 'late_1455':
            c['time']['no_new_entry_after'] = '14:55'
        elif name == 'volume_5m':
            c['execution'].update(min_amount_ratio_1m=1e6, min_amount_ratio_1m_floor=0)
        elif name == 'leader_gap':
            c['leader_opening_hold']['min_open_gap_pct'] = -2
        elif name == 'leader_window':
            c['leader_opening_hold']['end'] = '10:15'
        result[name] = c
    return result


def compact(result):
    return {'entry': result['entry_allowed'], 'candidate': result['candidate_entry_pattern'],
            'method_confirmed': bool(result.get('method_entry', {}).get('eligible')),
            'blockers': result['blockers'], 'room_risk': result.get('room_risk'),
            'position_action': result['position_action'], 'position_reason': result['position_reason'],
            'exit_reference': result['exit_reference']}


def main(out):
    old = load_old(out, 'intraday_timing_v2')
    old.method_entry = load_old(out, 'method_entry')
    old_router = load_old(out, 'observation_strategy_router')
    frozen = read(ROOT / 'output/acceptance_intraday_20260907_105101/inputs.json')
    templates = {x['row']['code']: x for x in frozen['rows']}
    audit_path = RUNTIME / 'data/runtime/signal_audit_20260907.jsonl'
    audit_bytes = audit_path.read_bytes()
    audit = [json.loads(line) for line in audit_bytes.splitlines() if line.strip()]
    daily = read(FROZEN / 'daily_inputs_t1.json')
    minutes_raw = read(FROZEN / 'minute_inputs.json')
    old_contracts, new_contracts = {}, {}
    for code, daily_bars in daily.items():
        assert daily_bars[-1]['date'] == '2026-09-04'
        identity = {k: templates[code]['row']['quote'].get(k) for k in ('code', 'name', 'industry', 'concepts')}
        stock = {'quote': identity, 'daily': daily_bars}
        old_contracts[code] = old_router.classify_stock(stock)
        new_contracts[code] = router.classify_stock(stock)
    save(out, 'classification_comparison.json', {
        code: {'old': old_contracts[code], 'new': new_contracts[code]} for code in daily})
    cfg = timing.load_config()
    alternatives = variants(cfg)
    counters = {name: Counter() for name in ('baseline', 'fixed_same_plan', 'new_premarket_plan', *alternatives)}
    changed, hypothetical, missing = [], [], Counter()
    method_events = {key: [] for key in counters}
    exit_mismatches = []
    with (out / 'comparison.jsonl').open('x', encoding='utf-8') as log:
        for snapshot in audit:
            now = datetime.fromisoformat(snapshot['timestamp'])
            for original in snapshot['rows']:
                code = original['symbol']
                if code not in templates or code not in minutes_raw:
                    missing[code] += 1
                    continue
                template = templates[code]
                c = copy.deepcopy(template['row']['strategy_contract'])
                c.update(key=original['strategy_key'], daily_qualified=original['daily_qualified'])
                if code in old_contracts:
                    c['daily_metrics'] = copy.deepcopy(old_contracts[code]['daily_metrics'])
                if code in {'301183', '301511'}:
                    c = copy.deepcopy(old_contracts[code])
                raw = [b for b in minutes_raw[code] if b['bar_end'] <= str(now)]
                if not raw:
                    missing[code] += 1
                    continue
                minute_rows = market._as_minute_rows(raw)
                price = original['price']
                quote = {k: template['row']['quote'].get(k) for k in ('code', 'name', 'industry', 'concepts', 'prev_close')}
                quote.update(close=price, pct=original['pct'], open=raw[0]['open'],
                             high=max(b['high'] for b in raw), low=min(b['low'] for b in raw),
                             volume_lot=sum(b['volume'] for b in raw),
                             amount_wan=sum((b['high']+b['low']+b['close'])/3*b['volume']/100 for b in raw))
                board = copy.deepcopy(original.get('board_rotation') or {})
                momentum = dict(board_name=board.get('board_name'), board_pct=board.get('board_pct'),
                                emotion_ok=bool(board.get('sustained') and board.get('leader_healthy')))
                lock = c.get('daily_metrics', {}).get('resonance_boards') or []
                if lock and board.get('board_name') not in lock:
                    momentum['emotion_ok'] = False
                    board['sustained'] = False
                row = {'code': code, 'name': original['name'], 'quote': quote, 'strategy_contract': c,
                       'rt_features': engine.minute_features(minute_rows, price, now),
                       'sector_momentum': momentum, 'sector_rotation': board}
                h120 = [b for b in template['history_120m'] if str(b['bar_end']) < DAY]
                h120, quality = market.merge_completed_120m(h120, raw, now)
                h15 = [b for b in template['history_15m'] if str(b['bar_end']) < DAY]
                kwargs = {'now': now, 'history_120m': h120, 'history_15m': h15,
                          'market_data': {'entry_ready': template['market_data'].get('entry_ready', False)
                                          and quality['current_session_ready'],
                                          'blockers': [] if quality['current_session_ready'] else ['当日闭合120分钟数据缺失']},
                          'position': {'sellable': 100}}
                baseline = old.evaluate(row, minute_rows, config=cfg, **kwargs)
                fixed = timing.evaluate(row, minute_rows, config=cfg, **kwargs)
                results = {'baseline': baseline, 'fixed_same_plan': fixed}
                if any(baseline[k] != fixed[k] for k in ('position_action', 'position_reason', 'exit_reference')):
                    exit_mismatches.append({'symbol': code, 'asof': str(now)})
                selected = c
                if code in new_contracts and c['key'] in {'TREND_520', 'TREND_MA5'}:
                    selected = copy.deepcopy(new_contracts[code])
                research_row = dict(row, strategy_contract=selected)
                if selected != c:
                    results['new_premarket_plan'] = timing.evaluate(research_row, minute_rows, config=cfg, **kwargs)
                else:
                    results['new_premarket_plan'] = fixed
                for name, variant in alternatives.items():
                    is_leader = selected['key'] == 'LEADER_EMOTION'
                    eligible_variant = (is_leader if name.startswith('leader_') else
                                        selected['key'] in {'TREND_520', 'TREND_MA5'})
                    if eligible_variant and selected.get('daily_qualified'):
                        results[name] = timing.evaluate(research_row, minute_rows, config=variant, **kwargs)
                info = {'symbol': code, 'name': original['name'], 'asof': str(now), 'price': price,
                        'old_strategy': c['key'], 'research_strategy': selected['key'],
                        'original_sector_ok': original['sector_ok'],
                        'sector_gate': router.sector_resonance_gate(selected, research_row)}
                for name, result in results.items():
                    counters[name]['checks'] += 1
                    counters[name]['method_confirmations'] += bool(result.get('method_entry', {}).get('eligible'))
                    counters[name]['technical_entries'] += bool(result['entry_allowed'])
                    counters[name]['technical_and_sector'] += bool(result['entry_allowed'] and info['sector_gate'][0])
                    if result.get('method_entry', {}).get('eligible'):
                        method_events[name].append({**info, **compact(result)})
                    if result['entry_allowed']:
                        hypothetical.append({**info, 'variant': name, **compact(result), 'formal_order': False})
                if (compact(baseline) != compact(fixed) and
                        (baseline['entry_allowed'] != fixed['entry_allowed'] or
                         baseline['candidate_entry_pattern'] != fixed['candidate_entry_pattern'])):
                    changed.append({**info, 'old': compact(baseline), 'new': compact(fixed)})
                log.write(json.dumps({**info, 'results': {k: compact(v) for k, v in results.items()}}, ensure_ascii=False)+'\n')
            print('Compared', snapshot['timestamp'], flush=True)
    result = {'trade_date': DAY, 'audit_sha256': hashlib.sha256(audit_bytes).hexdigest(),
              'counts': {k: dict(v) for k, v in counters.items()}, 'missing_symbols': dict(missing),
              'classification_changes': {c: [old_contracts[c]['key'], new_contracts[c]['key']]
                                         for c in daily if old_contracts[c]['key'] != new_contracts[c]['key']},
              'exit_mismatches': exit_mismatches, 'signal_changes': changed,
              'hypothetical_entries': hypothetical,
              'limitations': ['Sampled technical evidence, not historical executable fills.',
                              'T-1 classification re-evaluated only for the 13 frozen daily datasets.',
                              'Variants are one-factor offline hypotheses, not promoted signals.',
                              'Sector snapshots incomplete; pressure levels/orderbook/arrival latency not reconstructed.',
                              'Neither zero nor positive technical entries proves profit or complete missed-opportunity coverage.',
                              'No production plan, account, order or notification writes.']}
    save(out, 'replay_summary.json', result)
    save(out, 'method_events.json', method_events)
    assert not exit_mismatches, exit_mismatches
    print(json.dumps({k: result[k] for k in ('counts', 'classification_changes', 'exit_mismatches')}, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, required=True)
    main(parser.parse_args().output.resolve())
