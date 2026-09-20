"""Import observed UI evidence; dated immutable archives keep T-1 evidence intact."""
import argparse
import hashlib
import json
import re
from datetime import datetime, timedelta
from pathlib import Path

LABELS = {'intraday': '盘中人气榜', 'review': '复盘人气榜'}
SOURCE = 'kaipanla_local_ui_v1'


def checksum(value):
    return hashlib.sha256(json.dumps({k: v for k, v in value.items() if k != 'sha256'},
                                    ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def parse_capture(text, list_type, captured_at):
    label = LABELS[list_type]
    if f'榜单：{label}' not in text and not re.search(r'\(selected\).*' + label, text):
        raise ValueError('Selected list type is not evidenced')
    rows = {}
    for line in text.splitlines():
        line = re.sub(r'^\s*\d+\s+文本\s+(?:Description: )?', '', line)
        match = re.match(r'\s*(\d+),\s*([^,]+),\s*(\d{6}),\s*(.*?)([+-]?\d+(?:\.\d+)?)%', line)
        if not match:
            continue
        rank, name, code, theme, pct = match.groups()
        row = {'rank': int(rank), 'name': name.strip(), 'code': code,
               'theme': theme.strip(' ,'), 'pct': float(pct), 'list_type': label}
        if int(rank) < 1 or int(rank) > 1000:
            raise ValueError('Invalid rank')
        if rank in rows and rows[rank] != row:
            raise ValueError('Rank changed during capture; retry a stable snapshot')
        rows[rank] = row
    ordered = sorted(rows.values(), key=lambda r: r['rank'])
    if not ordered or len({r['code'] for r in ordered}) != len(ordered):
        raise ValueError('Empty or conflicting stock rows')
    ranks = [r['rank'] for r in ordered]
    complete = '已加载完所有数据' in text and ranks == list(range(1, len(ordered) + 1))
    source_time = None
    # A review tab without its own date cannot borrow the intraday tab's banner.
    dates = re.findall(r'最后更新时间为\s*(\d{2}-\d{2})\s+(\d{2}:\d{2})', text)
    if len(set(dates)) > 1:
        raise ValueError('Snapshot dates changed during capture')
    if dates:
        month_day, clock = dates[0]
        year = captured_at.year - (1 if captured_at.month == 1 and month_day.startswith('12-') else 0)
        source_time = datetime.strptime(f'{year}-{month_day} {clock}', '%Y-%m-%d %H:%M')
        if source_time > captured_at or captured_at - source_time > timedelta(days=7):
            raise ValueError('Source date is future or older than seven days')
    result = {'version': SOURCE, 'source': SOURCE, 'list_type': list_type,
              'source_date': source_time.strftime('%Y-%m-%d') if source_time else None,
              'source_time': str(source_time) if source_time else None,
              'date_verified': source_time is not None,
              'date_basis': 'visible_update_banner' if source_time else 'unverified_no_visible_date',
              'fetched_at': str(captured_at), 'complete': complete, 'count': len(ordered),
              'coverage_scope': 'app_displayed_list', 'rows': ordered,
              'evidence': text}
    result['sha256'] = checksum(result)
    return result


def archive(base_dir, snapshot):
    if checksum(snapshot) != snapshot.get('sha256'):
        raise ValueError('Snapshot checksum mismatch')
    date = snapshot.get('source_date') or 'undated'
    if date != 'undated':
        datetime.strptime(date, '%Y-%m-%d')
    kind = snapshot['list_type']
    if kind not in LABELS:
        raise ValueError('Invalid list type')
    directory = Path(base_dir) / 'data/runtime/kaipanla_popularity' / date
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{kind}_{snapshot['sha256']}.json"
    if not path.exists():
        with path.open('x', encoding='utf-8') as stream:
            json.dump(snapshot, stream, ensure_ascii=False, indent=2)
    return path


def load(base_dir, source_date=None, now=None):
    now = now or datetime.now()
    directory = Path(base_dir) / 'data/runtime/kaipanla_popularity'
    if source_date:
        try:
            datetime.strptime(source_date, '%Y-%m-%d')
        except (TypeError, ValueError):
            return {}
        paths = (directory / source_date).glob('*.json')
    else:
        paths = directory.glob('*/*.json')
    valid = []
    for path in paths:
        try:
            value = json.loads(path.read_text())
            observed = datetime.fromisoformat(value['fetched_at'])
            dated = datetime.fromisoformat(value['source_time'])
            if (value.get('version') != SOURCE or checksum(value) != value.get('sha256')
                    or not value.get('date_verified') or not value.get('rows')
                    or value['source_date'] != dated.strftime('%Y-%m-%d')
                    or dated > observed or observed > now
                    or (source_date and value['source_date'] != source_date)):
                continue
            valid.append(value)
        except (OSError, ValueError, KeyError, TypeError):
            continue
    # The timestamp, not the tab's name, determines the newest usable evidence.
    return max(valid, key=lambda v: (v['source_time'], bool(v.get('complete')), v['fetched_at']), default={})


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--input', type=Path, required=True)
    parser.add_argument('--base-dir', type=Path, required=True)
    parser.add_argument('--list-type', choices=LABELS, required=True)
    parser.add_argument('--captured-at', required=True)
    args = parser.parse_args()
    captured = datetime.fromisoformat(args.captured_at)
    if captured > datetime.now():
        raise ValueError('Future capture time')
    value = parse_capture(args.input.read_text(), args.list_type, captured)
    path = archive(args.base_dir, value)
    print(json.dumps({'path': str(path), 'count': value['count'], 'complete': value['complete'],
                      'date_verified': value['date_verified'], 'source_date': value['source_date']}, ensure_ascii=False))
