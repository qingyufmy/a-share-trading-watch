"""Frozen timing inputs for future audits; never sends orders or messages."""
import hashlib
import json
from pathlib import Path


def capture(row, minute_rows, now, history120, history15, config, market, position, expected):
    keys = ('code', 'state', 'quote', 'rt_features', 'strategy_contract', 'defense',
            'pressure', 'repair', 'dynamic', 'sector_momentum', 'sector_rotation')
    payload = {'schema': 'timing_entry_replay_v1', 'now': now.isoformat(),
               'row': {k: row[k] for k in keys if k in row}, 'minute_rows': minute_rows,
               'history_120m': history120 or [], 'history_15m': history15 or [],
               'config': config, 'market_data': market, 'position': position, 'expected': expected}
    try:
        payload['source_hashes'] = {name: hashlib.sha256(Path(__file__).with_name(name).read_bytes()).hexdigest()
                                    for name in ('intraday_timing_v2.py', 'method_entry.py', 'entry_quality.py')}
        raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, allow_nan=False,
                         default=lambda value: value.isoformat()).encode()
        result = json.loads(raw)
        result['sha256'] = hashlib.sha256(raw).hexdigest()
        return result
    except (TypeError, ValueError, AttributeError, OSError):
        return {'schema': 'timing_entry_replay_v1', 'capture_error': 'invalid_input'}
