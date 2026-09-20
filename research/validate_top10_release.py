"""Scoped, after-close release of top-ten audit support and isolated research."""
import argparse
from datetime import datetime, time
import difflib
import json
from pathlib import Path
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import install_launch_agent as install
from research.validate_entry_evidence_release import sha, save
from research.deploy_strategy_optimization import account_snapshot
from core import closed_liquidity

FILES = ('core/closed_liquidity.py', 'core/strategy_gap_research.py',
         'core/observation_strategy_router.py', 'core/sector_identity.py',
         'core/repair_observation.py', 'core/intraday_timing_v2.py',
         'premarket_report.py', 'realtime_signal_engine.py')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--deploy', action='store_true')
    parser.add_argument('--scope', choices=('top10', 'shadow-notifications'), default='top10')
    args = parser.parse_args()
    files = FILES if args.scope == 'top10' else (
        'core/shadow_research_reporting.py', 'realtime_signal_engine.py', 'after_close_report.py')
    runtime = install.RUNTIME_DIR
    out = ROOT / 'output' / (args.scope + '_release_' + datetime.now().strftime('%Y%m%d_%H%M%S_%f'))
    out.mkdir()
    print(str(out), flush=True)
    baseline = install.release_manifest(runtime)
    account = account_snapshot()
    historical = {str(p.relative_to(runtime)): sha(p) for pattern in
                  ('data/runtime/*20260915*', 'data/runtime/*20260914*', 'data/runtime/quote_cache.json')
                  for p in runtime.glob(pattern) if p.is_file()}
    changes = [{'path': name, 'before': sha(runtime/name), 'after': sha(ROOT/name)} for name in files]
    save(out, 'baseline.json', {'manifest': baseline, 'account': account, 'historical': historical, 'changes': changes})
    for name in (*files, 'release_manifest.json'):
        if (runtime/name).exists():
            target = out/'backup'/name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(runtime/name, target)
    stage = out/'stage'; stage.mkdir()
    for name in install.RUNTIME_FILES:
        shutil.copy2(runtime/name, stage/name)
    for folder, pattern in (('core', '*.py'), ('configs', '*.json')):
        (stage/folder).mkdir()
        for p in (runtime/folder).glob(pattern):
            shutil.copy2(p, stage/folder/p.name)
    for name in files:
        shutil.copy2(ROOT/name, stage/name)
    with (out/'production.patch').open('x') as f:
        for c in changes:
            f.write(''.join(difflib.unified_diff(
                (out/'backup'/c['path']).read_text().splitlines(True) if c['before'] else [],
                (stage/c['path']).read_text().splitlines(True), fromfile='before/'+c['path'], tofile='after/'+c['path'])))
    def test(name, cmd):
        r = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, timeout=300)
        save(out, name, {'returncode': r.returncode, 'output': r.stdout+r.stderr})
        assert r.returncode == 0, r.stdout+r.stderr
    test('source_tests.json', [str(ROOT/'.venv_quant/bin/python'), '-m', 'unittest', 'discover', '-s', 'tests', '-q'])
    test('staged_native_tests.json', [str(install.PYTHON_APP_EXECUTABLE), str(ROOT/'research/run_strategy_suite.py'), '--runtime', str(stage)])
    assert account_snapshot() == account
    assert all(sha(runtime/n) == h for n, h in historical.items())
    if not args.deploy:
        return
    assert datetime.now().time() >= time(15, 15), 'After close only'
    assert subprocess.run(['pgrep', '-fl', 'realtime_signal_engine.py'], capture_output=True).returncode == 1, 'Engine running'
    assert all(sha(runtime/n) == h for n, h in baseline['files'].items()), 'Runtime drift'
    assert all(sha(ROOT/c['path']) == c['after'] and sha(runtime/c['path']) == c['before'] for c in changes)
    assert sha(runtime/'release_manifest.json') == sha(out/'backup/release_manifest.json')
    for c in changes:
        shutil.copy2(stage/c['path'], runtime/c['path'])
    test('installed_native_tests.json', [str(install.PYTHON_APP_EXECUTABLE), str(ROOT/'research/run_strategy_suite.py'), '--runtime', str(runtime)])
    assert account_snapshot() == account
    assert all(sha(runtime/n) == h for n, h in historical.items())
    assert all(sha(runtime/c['path']) == c['after'] for c in changes)
    assert all(sha(runtime/n) == h for n, h in baseline['files'].items() if n not in files)
    old_manifest = json.loads((out/'backup/release_manifest.json').read_text())
    manifest = install.release_manifest(runtime)
    for k, v in old_manifest.items():
        if k not in ('release_id', 'generated_at', 'files', 'strategy_version'):
            manifest[k] = v
    manifest[args.scope.replace('-', '_') + '_release'] = out.name
    (runtime/'release_manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2))
    # New reference records only; no historical plan, order, signal or quote overwrite.
    quotes = json.loads((runtime/'data/runtime/quote_cache.json').read_text())
    archive = closed_liquidity.archive_completed_quotes(runtime, quotes, datetime.now())
    save(out, 'acceptance.json', {'release_id': manifest['release_id'], 'deployed_at': str(datetime.now()),
        'files': changes, 'account_unchanged': account_snapshot() == account,
        'historical_files_unchanged': all(sha(runtime/n) == h for n, h in historical.items()),
        'other_runtime_files_unchanged': True, 'closing_liquidity_archive': archive,
        'activation': 'next_scheduled_start', 'manual_orders': 0, 'manual_pushes': 0,
        'new_formal_entry_routes': 0, 'research_only': True})
    print('Deployment verified: ' + manifest['release_id'], flush=True)


if __name__ == '__main__':
    main()
