"""Scoped regression and backup-checked deployment; no trading or messaging."""
import argparse
import copy
import hashlib
import importlib.util
import json
import shutil
import sys
import unittest
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = Path.home() / 'Library/Application Support/a-share-trading-watch'
OUT = ROOT / 'output/repair_iteration_20260907_173128'
FILES = ['core/local_market_data.py', 'core/observation_strategy_router.py',
         'core/intraday_timing_v2.py', 'core/repair_observation.py',
         'premarket_report.py', 'realtime_signal_engine.py']


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def save(name, value):
    path = OUT / name
    if path.exists():
        path = path.with_name(path.stem + '_' + datetime.now().strftime('%H%M%S%f') + path.suffix)
    with path.open('x', encoding='utf-8') as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2, default=str)


def tests(runtime):
    from core import intraday_timing_v2, local_market_data, observation_strategy_router, repair_observation
    import premarket_report
    import realtime_signal_engine
    modules = [intraday_timing_v2, local_market_data, observation_strategy_router,
               repair_observation, premarket_report, realtime_signal_engine]
    base = RUNTIME if runtime else ROOT
    paths = {m.__name__: str(Path(m.__file__).resolve()) for m in modules}
    assert all(Path(p).is_relative_to(base) for p in paths.values()), paths
    suite = unittest.defaultTestLoader.discover(str(ROOT / 'tests'))
    def flatten(items):
        for item in items:
            if isinstance(item, unittest.TestSuite):
                yield from flatten(item)
            else:
                yield item
    all_tests = list(flatten(suite))
    excluded = [t for t in all_tests if runtime and t.id().startswith('test_install_launch_agent.')]
    result = unittest.TextTestRunner(verbosity=1).run(unittest.TestSuite(t for t in all_tests if t not in excluded))
    save('runtime_tests.json' if runtime else 'source_tests.json',
         {'tests': result.testsRun, 'passed': result.wasSuccessful(),
          'errors': [(str(t), detail) for t, detail in result.errors],
          'failures': [(str(t), detail) for t, detail in result.failures],
          'excluded': [t.id() for t in excluded], 'module_paths': paths, 'python': sys.executable})
    assert result.wasSuccessful()


def frozen_regression():
    from core import intraday_timing_v2 as current
    path = OUT / 'backup/source/core/intraday_timing_v2.py'
    spec = importlib.util.spec_from_file_location('repair_before_timing', path)
    before = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = before
    spec.loader.exec_module(before)
    frozen_path = ROOT / 'output/acceptance_intraday_20260907_105101/inputs.json'
    frozen = json.loads(frozen_path.read_text())
    fields = ('entry_allowed', 'entry_pattern', 'candidate_entry_pattern', 'blockers',
              'position_action', 'position_reason', 'exit_reference')
    changes = []
    for item in frozen['rows']:
        opts = {'now': datetime.fromisoformat(frozen['asof']), 'config': frozen['config']}
        a = before.evaluate(**copy.deepcopy(item), **copy.deepcopy(opts))
        b = current.evaluate(**copy.deepcopy(item), **copy.deepcopy(opts))
        diff = {k: [a.get(k), b.get(k)] for k in fields if a.get(k) != b.get(k)}
        if diff:
            changes.append({'symbol': item['row']['code'], 'changes': diff})
    save('frozen_regression.json', {'symbols': len(frozen['rows']), 'changes': changes,
                                    'fields': fields, 'input_sha256': sha(frozen_path)})
    assert not changes, changes
    print('Frozen input regression:', len(frozen['rows']), 'unchanged')


def deploy():
    import install_launch_agent
    assert not (OUT / 'deployment.json').exists(), 'Release already applied'
    assert json.loads((OUT / 'source_tests.json').read_text())['passed']
    assert not json.loads((OUT / 'frozen_regression.json').read_text())['changes']
    changes = []
    for rel in FILES:
        source, target, backup = ROOT / rel, RUNTIME / rel, OUT / 'backup/runtime' / rel
        if target.exists():
            assert backup.exists() and sha(target) == sha(backup), 'Runtime changed: ' + rel
        else:
            assert rel == 'core/repair_observation.py', 'Unexpected missing file: ' + rel
        changes.append({'file': rel, 'before': sha(target) if target.exists() else None, 'after': sha(source)})
    manifest_path = RUNTIME / 'release_manifest.json'
    backup_manifest = OUT / 'backup/runtime/release_manifest.json'
    assert not backup_manifest.exists()
    shutil.copy2(manifest_path, backup_manifest)
    for rel in FILES:
        shutil.copy2(ROOT / rel, RUNTIME / rel)
    manifest = install_launch_agent.release_manifest(RUNTIME)
    assert all(sha(ROOT / rel) == sha(RUNTIME / rel) == manifest['files'][rel] for rel in FILES)
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8')
    save('deployment.json', {'at': str(datetime.now()), 'release_id': manifest['release_id'],
                             'files': changes, 'source_runtime_equal': True,
                             'orders_or_messages_generated': False, 'ledger_modified': False})
    print('Deployed', manifest['release_id'], len(FILES), 'files')


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=['test', 'runtime-test', 'deploy'])
    action = parser.parse_args().action
    sys.path.insert(0, str(RUNTIME if action == 'runtime-test' else ROOT))
    if action == 'runtime-test':
        sys.path.insert(1, str(ROOT))
    if action == 'deploy':
        deploy()
    else:
        tests(action == 'runtime-test')
        if action == 'test':
            frozen_regression()
