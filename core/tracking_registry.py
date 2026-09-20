"""User-confirmed tracking coverage, separate from order authorization."""
import json
import re
from pathlib import Path


def instrument_kind(code, name):
    if 'ETF' in name.upper():
        return 'etf'
    if code.startswith(('881', '886')):
        return 'reference_index'
    if code.startswith(('15', '16', '5')):
        return 'reference_fund'
    if re.fullmatch(r'(?:00|30|60|68|83|87|88|92)\d{4}', code):
        return 'equity'
    raise ValueError('Unverified instrument type: ' + code)


def validate(payload):
    if payload.get('version') != 1 or not payload.get('verified_at'):
        raise ValueError('Tracking registry missing version or verification time')
    records = payload.get('records')
    if not isinstance(records, list) or not records:
        raise ValueError('Tracking registry is empty')
    seen = set()
    for row in records:
        code, name = row.get('code', ''), row.get('name', '')
        if not re.fullmatch(r'\d{6}', code) or not name or code in seen:
            raise ValueError('Invalid or duplicate tracking record: ' + code)
        kind = instrument_kind(code, name)
        if kind == 'etf' or row.get('kind') != kind:
            raise ValueError('Excluded or mismatched instrument: ' + code)
        if not isinstance(row.get('groups'), list) or row.get('entry_authorized') is not False:
            raise ValueError('Coverage must not grant entry authorization: ' + code)
        seen.add(code)
    return payload


def read(base_dir):
    path = Path(base_dir) / 'configs' / 'tracking_watchlist.json'
    if not path.exists():
        return {'records': [], 'groups': [], 'references': [], 'errors': []}
    try:
        payload = validate(json.loads(path.read_text(encoding='utf-8')))
    except (OSError, ValueError, TypeError, AttributeError) as exc:
        return {'records': [], 'groups': [], 'references': [],
                'errors': ['已确认观察池读取失败：' + str(exc)]}
    groups = {}
    for row in payload['records']:
        if row['kind'] != 'equity':
            continue
        for name in row['groups'] or ['保留补充观察']:
            groups.setdefault(name, []).append(row['code'])
    return {
        'records': payload['records'],
        'groups': [{'name': name, 'codes': codes, 'count': len(codes),
                    'file': str(path), 'updated_at': payload['verified_at']}
                   for name, codes in groups.items()],
        'references': [row for row in payload['records'] if row['kind'] != 'equity'],
        'errors': [], 'verified_at': payload['verified_at'],
    }
