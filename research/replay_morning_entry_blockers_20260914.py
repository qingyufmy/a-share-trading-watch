"""Offline frozen-AM blocker review; never invoke an engine, order or sender."""
from collections import Counter, defaultdict
from datetime import datetime
import hashlib
import json
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from core import global_risk, method_replay
from research.verify_frozen_method_replay import validate

RUNTIME = Path.home() / 'Library/Application Support/a-share-trading-watch'
MARKET_BLOCK = re.compile(r'^V2市场/板块门控\[')


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def risk_input(row):
    identity = ((row.get('strategy_contract') or {}).get('daily_metrics') or {}).get('resonance_identity') or {}
    industry = (identity.get('evidence') or {}).get('industry')
    metrics = row.get('timing_metrics') or {}
    return {
        'code': row['symbol'], 'industry': industry,
        'quote': {'name': row['name'], 'industry': industry},
        'sector_momentum': row.get('sector_momentum') or {},
        'sector_rotation': row.get('board_rotation') or {},
        'theme_evidence': (row.get('sector_momentum') or {}).get('theme_evidence') or {},
        'rt_features': {'amount_ratio_1m': metrics.get('rvol_1m'), 'amount_ratio_5m': metrics.get('rvol_5m')},
    }


def category(reason):
    for text, label in [('V2市场/板块门控', '国内市场/方向'), ('量能', '量能'),
                        ('VWAP过远', 'VWAP距离'), ('盈亏比', '盈亏比'),
                        ('策略第一关', '板块共振'), ('5分钟', '5分钟执行')]:
        if text in reason:
            return label
    return '其他'


def run(samples):
    permissions, candidates, summaries, candidate_counts = Counter(), [], {}, Counter()
    funnel = Counter()
    for sample in samples:
        now = datetime.fromisoformat(sample['decision_recorded_at'])
        ctx = global_risk.apply_a_share_entry_policy(sample['global_risk'], sample['market_state'], now)
        permissions[ctx['entry_risk_source']] += 1
        assert sample['input_sha256'] == method_replay.digest(sample['rows'])
        for row in sample['rows']:
            funnel['rows'] += 1
            if row.get('daily_qualified'):
                funnel['daily_qualified'] += 1
            if not row.get('candidate_pattern'):
                continue
            adapted = risk_input(row)
            allowed, stage, reason = global_risk.core_attack_gate_status(ctx, adapted, now)
            # Match the narrow production override, never bypass domestic tech,
            # systemic or data gates merely because a sector is strong.
            route_confirmed = any(route.get('eligible') and route.get('sector_confirmed')
                                  for route in (row.get('leader_routes') or {}).values())
            override = stage == 'CORE_SECTOR_ROTATION_BLOCKED' and route_confirmed
            remaining = [b for b in row.get('all_blockers') or [] if not MARKET_BLOCK.match(b)]
            if not allowed and not override:
                remaining.append(f'V2市场/板块门控[{stage}]：{reason}')
            node = {
                'at': sample['timestamp'], 'decision_at': sample['decision_recorded_at'],
                'code': row['symbol'], 'name': row['name'], 'price': row['price'], 'pct': row['pct'],
                'strategy': row['strategy_key'], 'pattern': row['candidate_pattern'],
                'daily_qualified': row['daily_qualified'], 'sector_ok': row['sector_ok'],
                'execution_5m': row['execution_5m'], 'reward_risk': row['reward_risk'],
                'volume_1m': (row.get('timing_metrics') or {}).get('rvol_1m'),
                'volume_5m': (row.get('timing_metrics') or {}).get('rvol_5m'),
                'risk_bucket_from_frozen_identity': global_risk.candidate_risk_bucket(adapted),
                'market_gate': {'allowed': allowed, 'stage': stage, 'reason': reason, 'override': override},
                'permission_source': ctx['entry_risk_source'], 'original_blockers': row['all_blockers'],
                'remaining_blockers': remaining,
            }
            candidates.append(node)
            candidate_counts.update(set(category(b) for b in remaining))
            detail = summaries.setdefault(row['symbol'], {'name': row['name'], 'strategy': row['strategy_key'],
                'nodes': [], 'blocker_counts': Counter(), 'first_price': row['price']})
            detail['nodes'].append(node)
            detail['blocker_counts'].update(set(category(b) for b in remaining))
            if row['daily_qualified'] and row['sector_ok']:
                funnel['daily_sector_candidates'] += 1
            if not remaining and row['daily_qualified'] and row['sector_ok']:
                funnel['unblocked_recorded_candidates'] += 1
    return {'permission_counts': dict(permissions), 'funnel': dict(funnel),
            'candidate_node_count': len(candidates), 'candidate_stock_count': len(summaries),
            'candidate_blocker_counts': dict(candidate_counts), 'stocks': summaries, 'candidates': candidates}


def main():
    out = ROOT / 'output' / ('morning_blockers_20260914_' + datetime.now().strftime('%H%M%S'))
    out.mkdir(exist_ok=False)
    audit = RUNTIME / 'data/runtime/signal_audit_20260914.jsonl'
    lines = audit.read_bytes().splitlines(keepends=True)
    lines = [line for line in lines if datetime.fromisoformat(json.loads(line)['decision_recorded_at']).hour < 12]
    with (out / 'frozen_morning.jsonl').open('xb') as f:
        f.writelines(lines)
    samples = [json.loads(line) for line in lines]
    assert len(samples) == 25
    result = run(samples)
    assert result == run(samples), 'Non-deterministic permission replay'
    verification = validate(out / 'frozen_morning.jsonl')
    assert verification['status'] == 'passed'
    files = ['core/global_risk.py', 'realtime_signal_engine.py', 'core/intraday_timing_v2.py',
             'core/method_entry.py', 'configs/intraday_timing_v2.json']
    hashes = {rel: {'source': digest(ROOT / rel), 'runtime': digest(RUNTIME / rel)} for rel in files}
    assert all(v['source'] == v['runtime'] for v in hashes.values())
    result.update(date='2026-09-14', session='AM', frozen_method_verification=verification,
                  frozen_hash=digest(out / 'frozen_morning.jsonl'), code_hashes=hashes,
                  scope='Recompute domestic permission and candidate scoped gate; verify frozen 520/MA5 branch; retain all other original blockers.',
                  limitations=['5-minute audit snapshots, not all 270 engine ticks.',
                               'Full leader/120m/5m inputs were not frozen; not a complete signal-engine or order replay.',
                               'Risk bucket reconstructed from frozen business identity, selected board and stock name; full original row is unavailable.',
                               'No notifications, orders, account mutations, historical plan edits or live network requests.'])
    with (out / 'result.json').open('x', encoding='utf-8') as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    print(json.dumps({k:result[k] for k in ('permission_counts', 'funnel', 'candidate_node_count',
                                          'candidate_stock_count', 'candidate_blocker_counts')}, ensure_ascii=False, indent=2))
    for node in result['candidates']:
        print(node['at'][11:16], node['name'], node['price'], node['pattern'],
              'RR', round(node['reward_risk'] or 0, 3), '；'.join(node['remaining_blockers']))
    print('OUTPUT', out)


if __name__ == '__main__':
    main()
