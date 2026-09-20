"""Preserve scoped deployment inputs and verify that research leaves the ledger alone."""
import hashlib
import json
import shutil
import argparse
import subprocess
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = Path.home() / 'Library/Application Support/a-share-trading-watch'
OUT = ROOT / 'output/mainline_iteration_20260908'
FILES = ['core/sector_identity.py', 'core/observation_strategy_router.py',
         'core/emotion_leader_pool.py', 'premarket_report.py', 'intraday_report.py',
         'preopen_quality_check.py', 'realtime_signal_engine.py', 'core/intraday_timing_v2.py']
TESTS = ['tests/test_mainline_identity_iteration.py', 'tests/test_intraday_health_repair.py',
         'tests/test_rotation_execution.py', 'tests/test_observation_strategy_router.py']


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None


def backup():
    OUT.mkdir(exist_ok=False)
    hashes = {}
    for name, root in [('source', ROOT), ('runtime', RUNTIME)]:
        for rel in FILES + ['release_manifest.json']:
            path = root / rel
            hashes[name + '/' + rel] = digest(path)
            if path.exists():
                target = OUT / 'backup' / name / rel
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(path, target)
    with (OUT / 'baseline.json').open('x') as stream:
        json.dump(hashes, stream, indent=2)
    print(OUT)


def save(name, value):
    with (OUT / name).open('x', encoding='utf-8') as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2, default=str)


def verify():
    sys.path.insert(0, str(ROOT))
    from core import sector_identity as identity, method_entry, intraday_timing_v2 as timing, local_market_data as market
    before = ROOT / 'output/strategy_review_20260908_153000'
    observations = json.loads((before / 'qualified_observations.json').read_text())
    inputs = json.loads((before / 'minute_inputs.json').read_text())
    original = {(x['symbol'], x['asof']): x['result'] for x in json.loads((before / 'method_replay.json').read_text())}
    catalog = identity.load_catalog(ROOT)
    assert len(catalog) >= 900
    catalog_before = digest(ROOT / 'data/reference/eastmoney_boards.json')
    profiles = identity.load_profiles(RUNTIME)
    configs = timing.load_config()['daily_method_entry']
    new_plans, migrated_plans, differences, confirmations = {}, {}, [], 0
    for obs in observations:
        code = obs['symbol']
        quote = profiles.get(code) or {}
        new_plans[code] = identity.resolve({'quote': quote}, catalog)
        migrated_plans[code] = identity.resolve({'quote': quote,
             'resonance_boards': (obs['strategy_contract'].get('daily_metrics') or {}).get('resonance_boards')}, catalog)
        if obs['strategy_key'] not in {'TREND_MA5', 'TREND_520'}:
            continue
        at = datetime.fromisoformat(obs['timestamp'])
        raw = [x for x in inputs.get(code, []) if x['bar_end'] <= str(at)]
        bars = timing.closed_bars(market._as_minute_rows(raw), 5, at)
        result = method_entry.evaluate(obs['strategy_contract'], bars, obs['price'], at, configs)
        confirmations += bool(result.get('eligible'))
        if result != original[(code, str(at))]:
            differences.append({'code': code, 'at': str(at)})
    assert not differences
    assert confirmations == 44
    assert len(new_plans) == 52
    for code in ('000759', '600693'):
        assert 'CPO概念' not in new_plans[code]['names']
        assert migrated_plans[code]['status'] == 'invalid_identity'
    tests = []
    native = '/Library/Developer/CommandLineTools/Library/Frameworks/Python3.framework/Versions/3.9/Resources/Python.app/Contents/MacOS/Python'
    for executable in (str(ROOT / '.venv_quant/bin/python'), native):
        result = subprocess.run([executable, '-m', 'unittest', 'discover', '-s', 'tests', '-q'],
                                cwd=ROOT, capture_output=True, text=True, timeout=180)
        tests.append({'executable': executable, 'returncode': result.returncode, 'output': result.stdout + result.stderr})
        assert result.returncode == 0, result.stderr
    assert digest(ROOT / 'data/reference/eastmoney_boards.json') == catalog_before, 'Tests modified the real identity cache'
    report = {'verified_at': str(datetime.now()), 'catalog_count': len(catalog),
              'catalog_hash': identity.catalog_hash(catalog), 'tests': tests,
              'method_replay_count': len(original), 'method_differences': differences,
              'method_confirmations': confirmations,
              'prospective_identity_counts': Counter(v['status'] for v in new_plans.values()),
              'prospective_identities': new_plans, 'legacy_identity_validation': migrated_plans,
              'limitations': 'Company profiles/catalog are post-close identity evidence, not historical executable signals. No historical full-board series is available; no replacement board returns or simulated profits were fabricated.'}
    name = 'validation_' + datetime.now().strftime('%H%M%S') + '.json'
    save(name, report)
    print(json.dumps({k: report[k] for k in ('catalog_count', 'method_replay_count', 'method_confirmations', 'prospective_identity_counts')}))
    return report


def deploy():
    verify()
    import install_launch_agent
    from research.deploy_strategy_optimization import account_snapshot
    baseline = json.loads((OUT / 'baseline.json').read_text())
    assert not (OUT / 'deployment.json').exists()
    assert digest(RUNTIME / 'release_manifest.json') == baseline['runtime/release_manifest.json']
    changes = []
    for rel in FILES:
        prior = OUT / 'backup/runtime' / rel
        assert digest(RUNTIME / rel) == digest(prior), 'Concurrent change: ' + rel
        changes.append({'path': rel, 'before': digest(prior), 'after': digest(ROOT / rel)})
    account_before = account_snapshot()
    for rel in TESTS:
        target = RUNTIME / rel
        if target.exists():
            backup_path = OUT / 'backup/runtime' / rel
            backup_path.parent.mkdir(parents=True, exist_ok=True)
            assert not backup_path.exists()
            shutil.copy2(target, backup_path)
    for rel in FILES + TESTS:
        target = RUNTIME / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / rel, target)
    from core import sector_identity
    catalog = sector_identity.load_catalog(ROOT)
    catalog = [{**x, '_coverage': {'complete': True, 'expected': len(catalog)}} for x in catalog]
    cache_result = sector_identity.update_catalog(RUNTIME, catalog)
    assert cache_result['status'] in {'updated', 'unchanged'}
    result = subprocess.run([str(install_launch_agent.PYTHON_APP_EXECUTABLE),
        str(ROOT / 'research/run_strategy_suite.py'), '--runtime', str(RUNTIME)], cwd=RUNTIME,
        capture_output=True, text=True, timeout=180)
    save('runtime_tests.json', {'returncode': result.returncode, 'output': result.stdout + result.stderr})
    assert result.returncode == 0, result.stdout + result.stderr
    assert account_snapshot() == account_before
    assert all(digest(ROOT / rel) == digest(RUNTIME / rel) for rel in FILES)
    manifest = install_launch_agent.release_manifest(RUNTIME)
    (RUNTIME / 'release_manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8')
    save('deployment.json', {'at': str(datetime.now()), 'release_id': manifest['release_id'],
                           'files': changes, 'account_unchanged': True,
                           'historical_plan_changed': False, 'scheduled_push_invoked': False,
                           'catalog': cache_result, 'runtime_tests_passed': True})
    print(json.dumps({'release_id': manifest['release_id'], 'account_unchanged': True}))


def finalize():
    verify()
    import install_launch_agent
    from research.deploy_strategy_optimization import account_snapshot
    baseline = json.loads((OUT / 'baseline.json').read_text())
    assert not (OUT / 'deployment.json').exists()
    assert digest(RUNTIME / 'release_manifest.json') == baseline['runtime/release_manifest.json']
    before = account_snapshot()
    save('finalization_inputs.json', {'at': str(datetime.now()), 'account': before,
         'runtime_hashes': {rel: digest(RUNTIME / rel) for rel in FILES + TESTS}})
    for rel in FILES + TESTS:
        shutil.copy2(ROOT / rel, RUNTIME / rel)
    result = subprocess.run([str(install_launch_agent.PYTHON_APP_EXECUTABLE),
        str(ROOT / 'research/run_strategy_suite.py'), '--runtime', str(RUNTIME)], cwd=RUNTIME,
        capture_output=True, text=True, timeout=180)
    save('runtime_tests_final.json', {'returncode': result.returncode, 'output': result.stdout + result.stderr})
    assert result.returncode == 0, result.stdout + result.stderr
    assert account_snapshot() == before
    assert all(digest(ROOT / rel) == digest(RUNTIME / rel) for rel in FILES + TESTS)
    from core import sector_identity
    catalog = sector_identity.load_catalog(RUNTIME)
    assert sector_identity.catalog_hash(catalog) == sector_identity.catalog_hash(sector_identity.load_catalog(ROOT))
    manifest = install_launch_agent.release_manifest(RUNTIME)
    (RUNTIME / 'release_manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8')
    save('deployment.json', {'at': str(datetime.now()), 'release_id': manifest['release_id'],
         'files': [{'path': rel, 'before': digest(OUT / 'backup/runtime' / rel),
                    'after': digest(RUNTIME / rel)} for rel in FILES],
         'account_unchanged_during_final_verification': True, 'account': before,
         'historical_plan_changed': False, 'scheduled_push_invoked': False,
         'catalog_count': len(catalog), 'catalog_hash': sector_identity.catalog_hash(catalog),
         'runtime_tests_passed': True, 'initial_runtime_test_failure': 'Empty profile placeholders overwrote known industry; fixed and regression tested.'})
    print(json.dumps({'release_id': manifest['release_id'], 'account_unchanged': True}))


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('mode', choices=['backup', 'verify', 'deploy', 'finalize'], nargs='?', default='backup')
    mode = parser.parse_args().mode
    {'backup': backup, 'verify': verify, 'deploy': deploy, 'finalize': finalize}[mode]()
