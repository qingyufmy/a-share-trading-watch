"""Scoped backup, isolated verification and deployment of Kaipanla ingestion."""
import argparse
import hashlib
import json
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = Path.home() / 'Library/Application Support/a-share-trading-watch'
OUT = ROOT / 'output/kaipanla_iteration_20260908'
FILES = ['core/kaipanla_snapshot.py', 'core/emotion_leader_pool.py']
sys.path.insert(0, str(ROOT))


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None


def save(name, value):
    with (OUT / name).open('x', encoding='utf-8') as f:
        json.dump(value, f, ensure_ascii=False, indent=2, default=str)


def backup():
    OUT.mkdir(exist_ok=False)
    hashes = {}
    for label, base in [('source', ROOT), ('runtime', RUNTIME)]:
        for rel in FILES + ['release_manifest.json']:
            hashes[label + '/' + rel] = sha(base / rel)
            if (base / rel).exists():
                dest = OUT / 'backup' / label / rel
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(base / rel, dest)
    save('baseline.json', hashes)


def deploy():
    import install_launch_agent as install
    from research.deploy_strategy_optimization import account_snapshot
    baseline = json.loads((OUT / 'baseline.json').read_text())
    before = account_snapshot()
    results = []
    for executable in [ROOT / '.venv_quant/bin/python', install.PYTHON_APP_EXECUTABLE]:
        result = subprocess.run([str(executable), '-m', 'unittest', 'discover', '-s', 'tests', '-q'],
                                cwd=ROOT, capture_output=True, text=True, timeout=180)
        results.append({'returncode': result.returncode, 'output': result.stdout + result.stderr})
        assert result.returncode == 0, result.stderr
    save('source_tests.json', results)
    for rel in FILES + ['release_manifest.json']:
        assert sha(RUNTIME / rel) == baseline['runtime/' + rel], rel
    save('deploy_inputs.json', {'account': before, 'hashes': {r: sha(ROOT / r) for r in FILES}})
    for rel in FILES + ['tests/test_kaipanla_snapshot.py']:
        shutil.copy2(ROOT / rel, RUNTIME / rel)
    result = subprocess.run([str(install.PYTHON_APP_EXECUTABLE), str(ROOT / 'research/run_strategy_suite.py'),
                             '--runtime', str(RUNTIME)], cwd=RUNTIME, capture_output=True, text=True, timeout=180)
    save('runtime_tests.json', {'returncode': result.returncode, 'output': result.stdout + result.stderr})
    assert result.returncode == 0, result.stdout + result.stderr
    assert account_snapshot() == before
    assert all(sha(ROOT / rel) == sha(RUNTIME / rel) for rel in FILES)
    manifest = install.release_manifest(RUNTIME)
    (RUNTIME / 'release_manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2))
    save('deployment.json', {'at': str(datetime.now()), 'release_id': manifest['release_id'],
                            'account_unchanged': True, 'files': FILES})
    print(manifest['release_id'])


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('mode', choices=['backup', 'deploy'])
    {'backup': backup, 'deploy': deploy}[parser.parse_args().mode]()
