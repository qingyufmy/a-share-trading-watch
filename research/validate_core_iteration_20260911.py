"""Read-only archived evidence comparison, not a fill or profit backtest."""
import argparse
import copy
import json
from collections import Counter
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from core import method_replay, sector_identity, observation_strategy_router as router
import intraday_report as intra


def review(path):
    counts = Counter()
    room_changes, sector_changes = [], []
    breadth = Counter()
    symbols = set()
    with path.open() as stream:
        for line in stream:
            sample = json.loads(line)
            assert method_replay.digest(sample['rows']) == sample['input_sha256']
            counts['snapshots'] += 1
            b = (sample.get('market_state') or {}).get('breadth') or {}
            if b.get('coverage_ready') and b.get('down_ratio', 0) >= .6 and b.get('up_ratio', 1) <= .3:
                breadth['weak_distribution_nodes'] += 1
                breadth['previous_mixed_nodes'] += b.get('state') == 'mixed'
            boards = [{'f12':b['board_id'],'f14':b['board_name'],'f3':b['pct'],'_coverage':b['coverage']}
                      for b in sample.get('board_snapshot') or []]
            rotations = {b['board_name']:b['rotation'] for b in sample.get('board_snapshot') or []}
            for row in sample['rows']:
                if row.get('method_replay_input'):
                    verification = method_replay.verify(row['method_replay_input'])
                    counts['frozen_'+verification['status']] += 1
                    assert verification['status'] == 'matched', verification
                if not row.get('daily_qualified'):
                    continue
                counts['qualified_nodes'] += 1
                gate = (row.get('structure_freshness') or {}).get('market_gate') or {}
                counts['original_market_veto'] += gate.get('allowed') is False
                counts['global_hard_veto'] += gate.get('stage') == 'CORE_GLOBAL_RISK_BLOCKED'
                c = copy.deepcopy(row['strategy_contract'])
                evidence = (c.get('daily_metrics',{}).get('resonance_identity') or {}).get('evidence') or {}
                if isinstance(evidence,dict) and evidence.get('industry'):
                    # Counterfactual NEW plan using only its archived T-1 metadata.
                    # Never edit or reinterpret the actual signed historical plan.
                    identity = sector_identity.resolve({'quote':evidence},boards)
                    sector_identity.apply(c['daily_metrics'],identity)
                    momentum = intra.sector_momentum_for_candidate(evidence,boards,contract=c,rotations=rotations)
                    rotation = rotations.get(momentum.get('board_name')) or {}
                    ok, reason = router.sector_resonance_gate(c,{'sector_momentum':momentum,'sector_rotation':rotation})
                    counts['replanned_sector_pass'] += ok
                    counts['original_sector_pass'] += bool(row.get('sector_ok'))
                    if bool(row.get('sector_ok')) != ok:
                        symbols.add(row['symbol'])
                        sector_changes.append({'at':sample['timestamp'],'symbol':row['symbol'],'name':row['name'],
                            'before':row['sector_ok'],'after':ok,'old_board':row.get('sector_momentum',{}).get('board_name'),
                            'new_board':momentum.get('board_name'),'reason':reason,'authorized':identity})
                if row.get('candidate_pattern'):
                    counts['technical_candidate_nodes'] += 1
                    room, rr = row.get('room_atr'), row.get('reward_risk')
                    before = room is not None and room >= 1 and rr is not None and rr >= 1.5
                    after = room is not None and room > 0 and rr is not None and rr >= 1.5
                    counts['space_rr_before'] += before
                    counts['space_rr_after'] += after
                    if before != after:
                        room_changes.append({'at':sample['timestamp'],'symbol':row['symbol'],'name':row['name'],
                            'room_atr':room,'rr':rr,'original_blockers':row.get('all_blockers')})
    return {'source':str(path),'counts':counts,'breadth':breadth,'sector_changed_symbols':len(symbols),
            'sector_changes':sector_changes,'space_changes':room_changes,
            'limitations':[
                'Frozen replay verifies only the original daily-method branch, not full engine or fills.',
                'Sector comparison is a new-plan counterfactual using archived metadata and archived live boards.',
                'Historical signed plans and their permissions are not changed.',
                'Historical audit lacks exact VWAP distance and leader closed-bar inputs; no invented replay for these.',
                'All qualified Sept11 nodes retain the original global veto: none can become a formal buy.',
                'No order, fees, slippage, T+1 outcome or profit is inferred from this comparison.']}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    base=Path.home()/'Library/Application Support/a-share-trading-watch'
    result=review(base/'data/runtime/signal_audit_20260911.jsonl')
    with args.output.open('x') as stream:
        json.dump(result,stream,ensure_ascii=False,indent=2)
    print(json.dumps({k:v for k,v in result.items() if k not in {'sector_changes','space_changes'}},ensure_ascii=False,indent=2))
