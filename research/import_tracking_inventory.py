"""Import a checked inventory as coverage only; never remove existing records."""
import argparse
from datetime import datetime
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from core.tracking_registry import instrument_kind, validate


def parse_inventory(path):
    rows = []
    for line in Path(path).read_text(encoding='utf-8').splitlines():
        cells = [c.strip() for c in line.split('|')[1:-1]]
        if len(cells) != 7 or len(cells[0]) != 6 or not cells[0].isdigit():
            continue
        code, name = cells[:2]
        kind = instrument_kind(code, name)
        if kind == 'etf':
            continue
        groups = cells[3].split('、') if cells[3] != '本次目录外' else ['保留补充观察']
        rows.append({'code': code, 'name': name, 'kind': kind, 'groups': groups,
                     'entry_authorized': False})
    if not rows or len({r['code'] for r in rows}) != len(rows):
        raise ValueError('Inventory empty or contains duplicate codes')
    return rows


def merge(existing, incoming, source):
    now = datetime.now().isoformat(' ', timespec='seconds')
    old = {r['code']: r for r in existing.get('records', [])}
    fresh = {r['code']: r for r in incoming}
    # Missing entries remain in coverage until a separate removal is approved.
    combined = {**old, **fresh}
    payload = {'version': 1, 'verified_at': now, 'source': str(source),
               'policy': 'exclude_etf; reference_only_non_equity; additive_no_auto_removal',
               'missing_from_latest_inventory': sorted(set(old)-set(fresh)),
               'records': [combined[c] for c in sorted(combined)]}
    return validate(payload)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--inventory', type=Path, required=True)
    parser.add_argument('--target', type=Path, required=True)
    args = parser.parse_args()
    existing = validate(json.loads(args.target.read_text())) if args.target.exists() else {}
    payload = merge(existing, parse_inventory(args.inventory), args.inventory.resolve())
    args.target.parent.mkdir(parents=True, exist_ok=True)
    if args.target.exists():
        backup = args.target.with_name(args.target.name + '.backup-' + datetime.now().strftime('%Y%m%d%H%M%S%f'))
        with backup.open('xb') as file:
            file.write(args.target.read_bytes())
    with args.target.open('w', encoding='utf-8') as file:
        json.dump(payload, file, ensure_ascii=False, indent=2)
    print(json.dumps({'total': len(payload['records']), 'equity': sum(r['kind']=='equity' for r in payload['records']),
                      'reference_only': sum(r['kind']!='equity' for r in payload['records']),
                      'target': str(args.target)}, ensure_ascii=False))


if __name__ == '__main__':
    main()
