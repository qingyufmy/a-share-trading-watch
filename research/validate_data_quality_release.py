"""Read-only incident replay, staged tests, and a backed-up one-file release."""
import argparse
from collections import Counter
from copy import deepcopy
from datetime import datetime
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import install_launch_agent as install
import realtime_signal_engine as engine


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--deploy', action='store_true')
    args = parser.parse_args()
    runtime = install.RUNTIME_DIR
    date = datetime.now().strftime('%Y-%m-%d')
    out = ROOT / 'output' / ('data_quality_release_' + datetime.now().strftime('%Y%m%d_%H%M%S'))
    out.mkdir()

    def save(name, obj):
        with (out / name).open('x') as f:
            json.dump(obj, f, ensure_ascii=False, indent=2)

    health = json.loads((runtime / 'web_dashboard/data/runtime/signal_health.json').read_text())
    quotes = json.loads((runtime / 'data/runtime/quote_cache.json').read_text())
    current = datetime.now()
    daily = deepcopy(health['daily_contract_revalidation'])
    if 'pending_errors' not in daily:
        daily['pending_errors'] = {}
        for error in daily.get('errors', []):
            code, sep, reason = error.partition(':')
            if sep and code.isdigit() and len(code) == 6:
                daily['pending_errors'][code] = {'name': quotes.get(code, {}).get('name'), 'error': reason}
    samples = {c: quotes.get(c, {}) for c in daily['pending_errors']}
    save('captured_inputs.json', {'captured_at': str(current), 'health': health, 'quotes': samples})
    cache = {'daily_contract_revalidation_health': daily}
    corrected = deepcopy(health)
    corrected['daily_contract_revalidation'] = engine.reconcile_nontrading_contract_health(cache, samples, current)
    engine.apply_runtime_quality(corrected)
    save('health_replay.json', {'before': health, 'after': corrected,
                              'execution_changed': False, 'orders_created': 0, 'pushes_sent': 0})
    assert corrected['daily_contract_revalidation']['pending_retry'] == daily['pending_retry']

    ticks = []
    buf = ''
    with (runtime / 'logs/realtime_signal_engine.out.log').open() as f:
        for line in f:
            if not buf and not line.startswith('{'):
                continue
            buf += line
            if line.strip() != '}':
                continue
            try:
                row = json.loads(buf)
            except ValueError:
                continue
            buf = ''
            if str(row.get('updated_at', '')).startswith(date):
                ticks.append(row)
    alerts = []
    with (runtime / 'logs/realtime_signal_engine.log').open() as f:
        for line in f:
            if date not in line or 'engine_health_alert' not in line:
                continue
            row = json.loads(line)
            if row.get('event') == 'engine_health_alert' and row.get('ts', '').startswith(date):
                alerts.append(row)
    stock_counts = Counter()
    with (runtime / f'data/runtime/signal_audit_{date.replace("-", "")}.jsonl').open() as f:
        for line in f:
            row = json.loads(line)
            for item in row.get('rows', []):
                if item.get('symbol') in samples:
                    stock_counts['records'] += 1
                    stock_counts['plan_allowed'] += bool(item.get('plan_allowed'))
                    stock_counts['daily_qualified'] += bool(item.get('daily_qualified'))
    save('incident_audit.json', {
        'date': date, 'ticks': len(ticks), 'from': ticks[0]['updated_at'], 'through': ticks[-1]['updated_at'],
        'quality_counts': dict(Counter(str(t.get('quality_error')) for t in ticks)),
        'breadth_complete_ticks': sum(t.get('market_breadth', {}).get('coverage_ready') is True for t in ticks),
        'daily_failure_counts': dict(Counter(';'.join(t.get('daily_contract_revalidation', {}).get('errors', [])) for t in ticks)),
        'health_alerts': alerts, 'pending_stock_audit': dict(stock_counts),
        'limitation': '历史审计未保存逐轮原始零成交报价，不能宣称全部历史轮次已按新隔离逻辑精确回放；仅对捕获快照实值回放。',
    })
    old = json.loads((runtime / 'release_manifest.json').read_text())
    protected = {p: sha(runtime / p) for p in old['files'] if (runtime / p).is_file()}
    stage = out / 'stage'
    stage.mkdir()
    for name in install.RUNTIME_FILES:
        if (runtime / name).exists():
            shutil.copy2(runtime / name, stage / name)
    for folder, pattern in [('core', '*.py'), ('configs', '*.json')]:
        (stage / folder).mkdir()
        for p in (runtime / folder).glob(pattern):
            shutil.copy2(p, stage / folder / p.name)
    shutil.copy2(ROOT / 'realtime_signal_engine.py', stage / 'realtime_signal_engine.py')

    def run(name, cmd):
        result = subprocess.run(cmd, cwd=ROOT, text=True, capture_output=True, timeout=180)
        save(name, {'exit_code': result.returncode, 'output': result.stdout + result.stderr})
        assert result.returncode == 0, result.stdout + result.stderr

    run('source_tests.json', [str(ROOT / '.venv_quant/bin/python'), '-m', 'unittest', 'discover', '-s', 'tests', '-q'])
    run('staged_native_tests.json', [str(install.PYTHON_APP_EXECUTABLE), str(ROOT / 'research/run_strategy_suite.py'), '--runtime', str(stage)])
    if args.deploy:
        assert all(sha(runtime / p) == value for p, value in protected.items())
        backup = out / 'backup'
        backup.mkdir()
        for name in ('realtime_signal_engine.py', 'release_manifest.json'):
            shutil.copy2(runtime / name, backup / name)
        # The daemon finishes its current cycle and exits 75 on source change;
        # launchd then restarts it without interrupting a database transaction.
        shutil.copy2(stage / 'realtime_signal_engine.py', runtime / 'realtime_signal_engine.py')
        manifest = install.release_manifest(runtime)
        for key, value in old.items():
            if key not in ('release_id', 'generated_at', 'files'):
                manifest[key] = value
        manifest['data_quality_repair'] = 'nontrading_deny_only_quarantine_v1'
        (runtime / 'release_manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2))
        run('installed_native_tests.json', [str(install.PYTHON_APP_EXECUTABLE), str(ROOT / 'research/run_strategy_suite.py'), '--runtime', str(runtime)])
        assert all(sha(runtime / p) == value for p, value in protected.items() if p != 'realtime_signal_engine.py')
        save('acceptance.json', {'release_id': manifest['release_id'], 'deployed_at': str(datetime.now()),
                                 'changes': ['realtime_signal_engine.py'], 'other_runtime_code_unchanged': True,
                                 'order_database_written_by_validation': False, 'manual_pushes': 0,
                                 'health_before': health['engine_status'], 'health_replay_after': corrected['engine_status']})
    print(str(out))


if __name__ == '__main__':
    main()
