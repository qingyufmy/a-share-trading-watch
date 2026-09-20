"""Read-only row replay, including rejected and losing members of the fixed pool."""
from datetime import datetime
from core import method_entry, method_replay, strategy_gap_research as gaps


def inspect_row(row):
    result = {'method': {'status': 'missing_input'}, 'shadow': {'status': 'missing_input'}}
    frozen = row.get('method_replay_input') or {}
    if frozen:
        try:
            if frozen.get('sha256') != method_replay.digest({k: v for k, v in frozen.items() if k != 'sha256'}):
                raise ValueError('integrity_failed')
            if frozen.get('schema') != method_replay.SCHEMA:
                raise ValueError('unsupported_schema')
            now = datetime.fromisoformat(frozen['now'])
            if any(gaps.end_time(b) > now or gaps.end_time(b).date() != now.date() for b in frozen['bars5']):
                raise ValueError('future_or_wrong_day_bar')
            actual = method_entry.evaluate(frozen['contract'], frozen['bars5'], frozen['price'], now, frozen['config'])
            same_version = frozen.get('evaluator_sha256') == method_replay.EVALUATOR_SHA256
            result['method'] = {'status': 'same_version' if same_version else 'current_code_on_original_input',
                                'recorded_eligible': frozen['expected'].get('eligible'),
                                'current_eligible': actual.get('eligible'),
                                'blockers': actual.get('blockers') or [],
                                'scope': 'daily_method_branch_only_not_full_entry'}
            if same_version:
                result['method']['exact_match'] = actual == frozen['expected']
            # A partial reconstruction exposes shape only. Unknown live gates stay false.
            shell = {'strategy_contract': frozen['contract'],
                     'quote': {'code': row.get('symbol'), 'close': frozen['price']}}
            shape = gaps.capture_evaluate(shell, [], frozen['bars5'], now, row.get('regime'), {})
            route = (shape.get('routes') or {}).get('TREND_CONTINUATION_RETEST') or {}
            result['method']['continuation_shape'] = (route.get('checks') or {}).get('breakout_retest_resume', False)
            result['method']['continuation_scope'] = 'shape_only_live_quote_vwap_and_permissions_not_reconstructed'
        except (KeyError, ValueError, TypeError, OverflowError) as exc:
            result['method'] = {'status': 'invalid_input', 'error': str(exc)}
    research = row.get('gap_research') or {}
    frozen_shadow = research.get('replay') or {}
    if frozen_shadow:
        try:
            actual = gaps.replay(frozen_shadow)
            result['shadow'] = {'status': 'replayed', 'exact_match': actual == frozen_shadow.get('expected'),
                                'ready_routes': [k for k, v in actual['routes'].items() if v.get('shadow_ready')],
                                'entry_allowed': False, 'order_action': 'NO_ORDER'}
        except (KeyError, ValueError, TypeError) as exc:
            result['shadow'] = {'status': 'invalid_input', 'error': str(exc)}
    elif research.get('capture_error'):
        result['shadow'] = {'status': 'capture_error', 'error': research['capture_error']}
    return result
