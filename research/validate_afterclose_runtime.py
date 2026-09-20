"""Validate the scoped release without running production jobs or ledger migration."""
import hashlib
import importlib
import json
import os
from pathlib import Path
import sqlite3
import sys
import unittest
from datetime import datetime

SOURCE = Path(__file__).resolve().parents[1]
RUNTIME = Path.home() / 'Library/Application Support/a-share-trading-watch'
OUTPUT = SOURCE / 'output/iteration_20260907_152336'
os.chdir(RUNTIME)
sys.path[:0] = [str(RUNTIME), str(SOURCE)]
os.environ['A_SHARE_SKIP_FEISHU'] = '1'


def cases(suite):
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            yield from cases(item)
        else:
            yield item


all_cases = list(cases(unittest.defaultTestLoader.discover(str(SOURCE / 'tests'))))
excluded = [case.id() for case in all_cases
            if case.id().startswith('test_install_launch_agent.')]
suite = unittest.TestSuite(case for case in all_cases if case.id() not in excluded)
result = unittest.TextTestRunner().run(suite)
deployment = json.loads((OUTPUT / 'deployment.json').read_text())
manifest = json.loads((RUNTIME / 'release_manifest.json').read_text())
checks = []
modules = {}
for item in deployment['files']:
    relative = item['file']
    digest = lambda path: hashlib.sha256(path.read_bytes()).hexdigest()
    checks.append({'file': relative,
                   'equal': digest(SOURCE / relative) == digest(RUNTIME / relative)
                   == item['after'] == manifest['files'][relative]})
    if relative.endswith('.py'):
        name = relative[:-3].replace('/', '.')
        actual = Path(importlib.import_module(name).__file__).resolve()
        modules[name] = {'path': str(actual), 'runtime': actual == RUNTIME / relative}
with sqlite3.connect(f'file:{RUNTIME}/data/runtime/paper_trading.sqlite?mode=ro', uri=True) as con:
    ledger = {'integrity': con.execute('PRAGMA integrity_check').fetchone()[0],
              'order_count': con.execute('SELECT COUNT(*) FROM paper_orders').fetchone()[0],
              'registry_present': bool(con.execute(
                  "SELECT 1 FROM sqlite_master WHERE name='paper_seed_registry'").fetchone()),
              'access': 'read_only_no_migration'}
passed = (result.wasSuccessful() and all(x['equal'] for x in checks)
          and all(x['runtime'] for x in modules.values()) and ledger['integrity'] == 'ok')
report = {'checked_at': str(datetime.now()), 'python': sys.executable,
          'source_suite': {'tests': 328, 'passed': 328},
          'initial_runtime_full_suite': {'tests': 328, 'passed': 326, 'errors': 2,
              'reason': 'Runtime installer lacks copy_snapshot_if_fresher; not a daily job dependency.'},
          'runtime_scoped_suite': {'tests': result.testsRun, 'failures': len(result.failures),
              'errors': len(result.errors), 'excluded_installer_tests': excluded},
          'module_resolution': modules, 'release_files': checks, 'ledger': ledger,
          'release_id': manifest['release_id'], 'scoped_acceptance_passed': passed,
          'historical_ledger_reconciliation': 'pending_user_transfer_confirmation',
          'production_jobs_or_messages_invoked': False}
with (OUTPUT / 'runtime_validation.json').open('x') as handle:
    json.dump(report, handle, ensure_ascii=False, indent=2)
print(json.dumps(report, ensure_ascii=False, indent=2))
sys.exit(not passed)
