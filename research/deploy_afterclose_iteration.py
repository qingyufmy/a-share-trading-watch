"""Scoped, backup-checked deployment. No scheduler, order or message execution."""
import argparse
import hashlib
import json
import shutil
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import install_launch_agent
import paper_trading

RUNTIME = Path.home() / 'Library/Application Support/a-share-trading-watch'
OUT = ROOT / 'output/iteration_20260907_152336'
FILES = ['paper_trading.py', 'intraday_report.py', 'realtime_signal_engine.py', 'premarket_report.py',
         'core/intraday_timing_v2.py', 'core/observation_strategy_router.py', 'core/method_entry.py',
         'core/opportunity_rating.py', 'core/strategy_discipline.py', 'core/signal_tracking.py',
         'configs/intraday_timing_v2.json']


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def save(name, value):
    with (OUT / name).open('x', encoding='utf-8') as f:
        json.dump(value, f, ensure_ascii=False, indent=2, default=str)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    changes = []
    for rel in FILES:
        source, target = ROOT / rel, RUNTIME / rel
        backup = OUT / 'backup/runtime' / rel
        if target.exists():
            assert backup.exists() and sha(target) == sha(backup), 'Runtime changed since backup: ' + rel
        else:
            assert rel == 'core/method_entry.py', 'Unexpected missing runtime file: ' + rel
        changes.append({'file': rel, 'before': sha(target) if target.exists() else None, 'after': sha(source)})
    if not args.apply:
        print(json.dumps(changes, indent=2)); return
    assert not (OUT / 'deployment.json').exists(), 'Do not apply a release twice'
    # SQLite backup takes a consistent read-only snapshot, including any WAL.
    shadow = OUT / 'shadow_account'
    paths = paper_trading.runtime_paths(shadow)
    original = RUNTIME / 'data/runtime/paper_trading.sqlite'
    assert not paths['db'].exists()
    source = sqlite3.connect(f'file:{original}?mode=ro', uri=True)
    destination = sqlite3.connect(paths['db'])
    source.backup(destination)
    source.close(); destination.close()
    con = paper_trading.init_db(shadow)
    try:
        quality = paper_trading.ledger_quality(con)
        holdings = [dict(zip(('symbol','quantity','sellable'), row)) for row in con.execute(
            'SELECT symbol,quantity,sellable FROM paper_positions ORDER BY symbol')]
        orders = con.execute('SELECT count(*) FROM paper_orders').fetchone()[0]
        check = con.execute('PRAGMA integrity_check').fetchone()[0]
    finally:
        con.close()
    assert check == 'ok'
    save('shadow_ledger_check.json', {'quality': quality, 'preserved_holdings': holdings,
                                     'preserved_order_count': orders, 'integrity': check,
                                     'production_ledger_modified': False})
    manifest_path = RUNTIME / 'release_manifest.json'
    if manifest_path.exists():
        shutil.copy2(manifest_path, OUT / 'backup/runtime/release_manifest.json')
    for rel in FILES:
        shutil.copy2(ROOT / rel, RUNTIME / rel)
    assert all(sha(ROOT / rel) == sha(RUNTIME / rel) for rel in FILES)
    manifest = install_launch_agent.release_manifest(RUNTIME)
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8')
    save('deployment.json', {'deployed_at': str(datetime.now()), 'release_id': manifest['release_id'],
                             'files': changes, 'source_runtime_equal': True,
                             'scheduler_or_feishu_invoked': False, 'production_ledger_modified': False})
    print(json.dumps({'release_id': manifest['release_id'], 'files': len(FILES), 'shadow_ledger_quality': quality}, ensure_ascii=False))


if __name__ == '__main__':
    main()
