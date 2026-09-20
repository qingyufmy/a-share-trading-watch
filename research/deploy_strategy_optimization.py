"""Deploy only audited fixes, preserving plans, schedules, accounts and config."""
import argparse
import difflib
import hashlib
import json
import shutil
import sqlite3
import subprocess
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import install_launch_agent

RUNTIME = Path.home() / 'Library/Application Support/a-share-trading-watch'
FILES = ['core/method_entry.py', 'core/observation_strategy_router.py',
         'core/intraday_timing_v2.py', 'realtime_signal_engine.py']
FIXTURES = ['tests/test_strategy_optimization.py',
            'output/watchlist_replay_20260907_173811/daily_inputs_t1.json']


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def save(out, name, value):
    path = out / name
    if path.exists():
        path = path.with_name(path.stem + '_' + datetime.now().strftime('%H%M%S%f') + path.suffix)
    with path.open('x', encoding='utf-8') as f:
        json.dump(value, f, ensure_ascii=False, indent=2, default=str)


def account_snapshot():
    path = RUNTIME / 'data/runtime/paper_trading.sqlite'
    with sqlite3.connect(f'file:{path}?mode=ro', uri=True) as con:
        con.execute('BEGIN')
        rows = {name: con.execute('SELECT * FROM ' + name + ' ORDER BY rowid').fetchall()
                for name in ('paper_positions', 'paper_orders', 'paper_account_snapshots',
                             'paper_position_snapshots', 'paper_buy_lots', 'paper_intent_marks',
                             'paper_order_marks', 'paper_reconciliations', 'paper_seed_registry')}
        rows['integrity'] = con.execute('PRAGMA integrity_check').fetchone()[0]
        return {'sha256': hashlib.sha256(json.dumps(rows, default=str).encode()).hexdigest(),
                'order_count': len(rows['paper_orders']), 'position_rows': len(rows['paper_positions']),
                'integrity': rows['integrity']}


def test(out, name, executable, cwd):
    command = ([str(executable), str(ROOT / 'research/run_strategy_suite.py'), '--runtime', str(RUNTIME)]
               if cwd == RUNTIME else [str(executable), '-m', 'unittest', 'discover', '-s', 'tests', '-q'])
    result = subprocess.run(command,
                            cwd=cwd, text=True, capture_output=True, timeout=180)
    save(out, name, {'exit_code': result.returncode, 'output': result.stdout + result.stderr,
                     'executable': str(executable), 'cwd': str(cwd)})
    assert result.returncode == 0, result.stdout + result.stderr


def main(out, apply, finalize=False):
    baseline = json.loads((out / 'baseline_hashes.json').read_text())
    replay = json.loads((out / 'replay_summary.json').read_text())
    assert not replay['exit_mismatches']
    assert replay['counts']['baseline']['checks'] == replay['counts']['fixed_same_plan']['checks'] == 8795
    assert replay['counts']['fixed_same_plan']['technical_entries'] == 0
    if finalize:
        preview = json.loads((out / 'deployment_preview.json').read_text())
        assert not (out / 'deployment.json').exists()
        assert sha(RUNTIME / 'release_manifest.json') == baseline['runtime/release_manifest.json']
        assert all(sha(ROOT / x['path']) == sha(RUNTIME / x['path']) == x['after'] for x in preview['files'])
        test(out, 'final_runtime_tests_verified.json', install_launch_agent.PYTHON_APP_EXECUTABLE, RUNTIME)
        assert account_snapshot() == preview['original_account']
        manifest = install_launch_agent.release_manifest(RUNTIME)
        (RUNTIME / 'release_manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8')
        save(out, 'deployment.json', {'deployed_at': str(datetime.now()), 'release_id': manifest['release_id'],
                                     'files': preview['files'], 'account_unchanged': True, 'source_runtime_equal': True,
                                     'config_changed': False, 'historical_plan_changed': False,
                                     'scheduled_job_or_order_or_notification_invoked': False,
                                     'test_discovery_failure_resolved': True})
        print(json.dumps({'release_id': manifest['release_id'], 'account_unchanged': True})); return
    test(out, 'final_source_tests.json', ROOT / '.venv_quant/bin/python', ROOT)
    test(out, 'native_source_tests.json', install_launch_agent.PYTHON_APP_EXECUTABLE, ROOT)
    original_account = account_snapshot()
    assert original_account['integrity'] == 'ok'
    changes = []
    for rel in FILES:
        assert sha(RUNTIME / rel) == baseline['runtime/' + rel], 'Concurrent runtime change: ' + rel
        changes.append({'path': rel, 'before': sha(RUNTIME / rel), 'after': sha(ROOT / rel)})
    manifest_path = RUNTIME / 'release_manifest.json'
    assert sha(manifest_path) == baseline['runtime/release_manifest.json']
    patch_text = ''.join(''.join(difflib.unified_diff(
        (out / 'backup/source' / rel).read_text().splitlines(keepends=True),
        (ROOT / rel).read_text().splitlines(keepends=True), fromfile='before/' + rel, tofile='after/' + rel))
        for rel in FILES)
    with (out / 'review.patch').open('x') as f:
        f.write(patch_text)
    save(out, 'deployment_preview.json', {'files': changes, 'original_account': original_account,
                                         'config_changes': [], 'plan_changes': [], 'scheduler_changes': []})
    if not apply:
        print('Validated preview only'); return
    assert not (out / 'deployment.json').exists()
    for rel in FIXTURES:
        target = RUNTIME / rel
        assert not target.exists() or sha(target) == sha(ROOT / rel), 'Existing fixture differs: ' + rel
    for rel in FILES + FIXTURES:
        target = RUNTIME / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / rel, target)
    executable = install_launch_agent.PYTHON_APP_EXECUTABLE
    try:
        test(out, 'final_runtime_tests.json', executable, RUNTIME)
    except Exception:
        save(out, 'deployment_failed.json', {'files': changes, 'backup': str(out / 'backup/runtime'),
                                            'reason': 'Runtime tests failed; release manifest not advanced.'})
        raise
    assert all(sha(ROOT / rel) == sha(RUNTIME / rel) for rel in FILES)
    assert account_snapshot() == original_account, 'Account changed during deployment; inspect before proceeding'
    manifest = install_launch_agent.release_manifest(RUNTIME)
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8')
    save(out, 'deployment.json', {'deployed_at': str(datetime.now()), 'release_id': manifest['release_id'],
                                 'files': changes, 'account_unchanged': True, 'source_runtime_equal': True,
                                 'config_changed': False, 'historical_plan_changed': False,
                                 'scheduled_job_or_order_or_notification_invoked': False})
    print(json.dumps({'release_id': manifest['release_id'], 'account': original_account}, ensure_ascii=False))


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--finalize', action='store_true')
    args = parser.parse_args()
    main(args.output.resolve(), args.apply, args.finalize)
