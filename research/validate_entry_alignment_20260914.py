"""Frozen morning counterfactual for sector divergence, MA5 and RR alignment."""
from collections import Counter
from datetime import datetime
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from core import global_risk, method_entry, intraday_timing_v2 as timing, signal_contract
from research.replay_morning_entry_blockers_20260914 import risk_input, MARKET_BLOCK
from research.verify_frozen_method_replay import validate


def run():
    frozen = ROOT / 'output/morning_blockers_20260914_120249/frozen_morning.jsonl'
    verification = validate(frozen)
    assert verification['status'] == 'passed'
    config = timing.load_config()
    output, counts = [], Counter()
    for sample in map(json.loads, frozen.read_text().splitlines()):
        now = datetime.fromisoformat(sample['decision_recorded_at'])
        context = global_risk.apply_a_share_entry_policy(sample['global_risk'], sample['market_state'], now)
        for row in sample['rows']:
            if not row.get('candidate_pattern'):
                continue
            adapted = risk_input(row)
            adapted['strategy_contract'] = row['strategy_contract']
            allowed, stage, reason = global_risk.core_attack_gate_status(context, adapted, now)
            remaining = [b for b in row['all_blockers'] if not MARKET_BLOCK.match(b)]
            frozen_method = row.get('method_replay_input') or {}
            method = {}
            anchor_ready = False
            if frozen_method:
                method = method_entry.evaluate(frozen_method['contract'], frozen_method['bars5'],
                                               frozen_method['price'], datetime.fromisoformat(frozen_method['now']),
                                               frozen_method['config'])
                anchor_ready = timing.daily_anchor_execution_ready(row['strategy_contract'], method, config)
                if anchor_ready:
                    remaining = [b for b in remaining if b != '5分钟已收盘VWAP收复未完成']
            route_confirmed = any(r.get('eligible') and r.get('sector_confirmed') for r in row['leader_routes'].values())
            if not allowed and not (stage == 'CORE_SECTOR_ROTATION_BLOCKED' and route_confirmed):
                remaining.append(f'V2市场/板块门控[{stage}]：{reason}')
            target, stop = method.get('target'), method.get('invalidation')
            if not method:
                route = next((r for r in row['leader_routes'].values() if r.get('eligible')), {})
                target, stop = route.get('target'), route.get('invalidation')
            cap = timing.risk_reward_price_ceiling(target, stop, 1.5)
            node = {'at': sample['timestamp'], 'code': row['symbol'], 'name': row['name'],
                    'strategy': row['strategy_key'], 'pattern': row['candidate_pattern'], 'price': row['price'],
                    'rr': row['reward_risk'], 'target': target, 'invalidation': stop, 'gross_rr_max_entry': cap,
                    'sector_divergence_allowed': stage == 'CORE_STRATEGY_SECTOR_ALLOWED',
                    'ma5_anchor_execution': anchor_ready, 'remaining_blockers': remaining}
            counts['candidate_nodes'] += 1
            if stage == 'CORE_STRATEGY_SECTOR_ALLOWED':
                counts['independent_sector_permission'] += 1
            if not remaining:
                counts['recorded_technical_chain_passed'] += 1
                # Cost contract stress only. Actual rating, account and lot-size
                # checks are not claimed to be reconstructed by these inputs.
                atr5 = row['timing_metrics']['atr5']
                proposal = {'trading_date': '2026-09-14', 'symbol': row['symbol'], 'name': row['name'],
                            'scenario': 'STRATEGY_MA5_ENTRY', 'current_price': row['price'],
                            'trigger_price': row['price'], 'invalid_price': stop, 'nearest_resistance': target,
                            'execution_band_low': row['price']-atr5*.1, 'execution_band_high': row['price']+atr5*.1,
                            'atr5m': atr5, 'amount_ratio_1m': row['timing_metrics']['rvol_1m'],
                            'updated_at': str(now.replace(microsecond=0))}
                c = signal_contract.from_signal(proposal)
                node['cost_proxy_only'] = {'execution_upper': c.exec_high, 'net_rr': c.net_reward_risk,
                                           'rr_price_ceiling': c.rr_price_ceiling,
                                           'assumptions': 'Missing original spread/ATR1 use contract defaults; no grade/plan/account authorization supplied; not an executable signal.'}
            output.append(node)
    return {'counts': dict(counts), 'candidates': output, 'frozen_method_verification': verification,
            'scope': 'Frozen leaf-rule counterfactual, not complete engine/fill/profit replay',
            'no_runtime_orders_or_pushes': True}


if __name__ == '__main__':
    result = run()
    assert result == run()
    out = Path(sys.argv[1])
    with out.open('x') as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    print(json.dumps(result['counts'], ensure_ascii=False))
    for row in result['candidates']:
        print(row['at'][11:16], row['name'], row['price'], 'RR', row['rr'], 'RR价格上限', row['gross_rr_max_entry'],
              '；'.join(row['remaining_blockers']) or json.dumps(row.get('cost_proxy_only'), ensure_ascii=False))
