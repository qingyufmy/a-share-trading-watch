"""Stage, verify and narrowly deploy confirmed watchlist coverage."""
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
from datetime import datetime

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import install_launch_agent as install
from research.deploy_strategy_optimization import account_snapshot

CHANGES = ('after_close_report.py', 'core/tracking_registry.py', 'configs/tracking_watchlist.json')


def sha(p):
    return hashlib.sha256(p.read_bytes()).hexdigest()


def main():
    out = ROOT/'output'/('tracking_release_'+datetime.now().strftime('%Y%m%d_%H%M%S'))
    out.mkdir()
    runtime = install.RUNTIME_DIR
    account = account_snapshot()
    old = json.loads((runtime/'release_manifest.json').read_text())
    protected = {p: sha(runtime/p) for p in old['files'] if (runtime/p).exists()}
    reports = {str(p): sha(p) for p in (runtime/'web_dashboard/data/reports').glob('*.json')}
    stage = out/'stage'
    stage.mkdir()
    for name in install.RUNTIME_FILES:
        if (runtime/name).exists():
            shutil.copy2(runtime/name, stage/name)
    for folder, pattern in [('core','*.py'), ('configs','*.json')]:
        (stage/folder).mkdir()
        for p in (runtime/folder).glob(pattern):
            shutil.copy2(p, stage/folder/p.name)
    for name in CHANGES:
        shutil.copy2(ROOT/name, stage/name)

    def run(name, cmd, cwd=ROOT):
        result = subprocess.run(cmd, cwd=cwd, text=True, capture_output=True, timeout=180)
        with (out/name).open('x') as f:
            json.dump({'exit_code':result.returncode,'output':result.stdout+result.stderr},f,ensure_ascii=False,indent=2)
        assert result.returncode == 0, result.stdout+result.stderr
        return result.stdout

    run('source_tests.json', [str(ROOT/'.venv_quant/bin/python'), '-m','unittest','discover','-s','tests','-q'])
    run('staged_native_tests.json', [str(install.PYTHON_APP_EXECUTABLE), str(ROOT/'research/run_strategy_suite.py'),'--runtime',str(stage)])
    probe = '''import json
from unittest.mock import patch
import after_close_report as b
from core.tracking_registry import read
r=read(b.BASE_DIR)
with patch.object(b,'write_watchlist_fallback'), patch.object(b,'write_watchlist_snapshot'), patch.object(b,'write_observation_watchlist_snapshot'):
 core=b.read_watchlist_details()
 obs=b.read_observation_watchlist_details()
 plan=b.premarket_plan_observation_details(obs)
expected={x['code'] for x in r['records'] if x['kind']=='equity'}
actual={c for c,m in core['rows']+plan['rows']}
assert actual==expected,(len(actual),len(expected),sorted(expected-actual))
assert len(expected)==319 and len(obs['reference_instruments'])==6
assert not {x['code'] for x in obs['reference_instruments']} & actual
assert all(len(v)==len(set(v)) for v in obs['memberships'].values())
assert not obs['errors'],obs['errors']
print(json.dumps({'equity_coverage':len(actual),'reference_only':6,'core_count':len(core['rows']),'plan_observation_count':len(plan['rows']),'registry_entries':len(r['records']),'orders_created':0}))
'''
    run('staged_coverage.json', [str(install.PYTHON_APP_EXECUTABLE),'-c',probe], stage)
    backup = out/'backup'
    backup.mkdir()
    assert account_snapshot() == account
    assert all(sha(runtime/p)==v for p,v in protected.items())
    for name in CHANGES + ('release_manifest.json',):
        if (runtime/name).exists():
            (backup/name).parent.mkdir(parents=True,exist_ok=True)
            shutil.copy2(runtime/name,backup/name)
    for name in CHANGES:
        shutil.copy2(stage/name, runtime/name)
    run('installed_native_tests.json', [str(install.PYTHON_APP_EXECUTABLE),str(ROOT/'research/run_strategy_suite.py'),'--runtime',str(runtime)])
    result = run('installed_coverage.json',[str(install.PYTHON_APP_EXECUTABLE),'-c',probe],runtime)
    assert account_snapshot() == account
    assert all(sha(Path(p))==v for p,v in reports.items())
    assert all(sha(runtime/p)==v for p,v in protected.items() if p not in CHANGES)
    manifest = install.release_manifest(runtime)
    for k,v in old.items():
        if k not in ('release_id','generated_at','files'):
            manifest[k]=v
    manifest['tracking_registry_version']='full_directory_excluding_etf_20260914'
    (runtime/'release_manifest.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2))
    audit={'release_id':manifest['release_id'],'changes':list(CHANGES),'account_unchanged':True,
           'historical_reports_unchanged':True,'strategy_math_unchanged':True,'no_push':True,
           'coverage':json.loads(result)}
    with (out/'acceptance.json').open('x') as f:
        json.dump(audit,f,ensure_ascii=False,indent=2)
    print(json.dumps({'output':str(out),**audit},ensure_ascii=False))


if __name__=='__main__':
    main()
