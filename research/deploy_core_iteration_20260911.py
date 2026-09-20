"""Stage/test a scoped release; preserve schedules, plans, accounts and history."""
import argparse
from datetime import datetime, time
import difflib
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
import install_launch_agent as install
from research.deploy_strategy_optimization import account_snapshot

RUNTIME=install.RUNTIME_DIR
FILES=['realtime_signal_engine.py','intraday_report.py','core/sector_identity.py',
       'core/global_risk.py','core/local_market_data.py','core/intraday_timing_v2.py',
       'configs/intraday_timing_v2.json']


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def save(path,value):
    with path.open('x') as stream:
        json.dump(value,stream,ensure_ascii=False,indent=2)


def run_tests(out, executable, runtime=None):
    cmd=([str(executable),str(ROOT/'research/run_strategy_suite.py'),'--runtime',str(runtime)]
         if runtime else [str(executable),'-m','unittest','discover','-s','tests','-q'])
    result=subprocess.run(cmd,cwd=ROOT,capture_output=True,text=True,timeout=180)
    save(out,{'command':cmd,'exit_code':result.returncode,'output':result.stdout+result.stderr})
    assert result.returncode==0,result.stdout+result.stderr


def main(output,apply):
    assert datetime.now().time()>time(15,5),'Deploy only after intraday engine exit'
    before=account_snapshot()
    release_before=sha(RUNTIME/'release_manifest.json')
    changes=[]
    for rel in FILES:
        assert sha(RUNTIME/rel)==sha(output/rel),'Unexpected runtime/source baseline difference: '+rel
        changes.append({'path':rel,'before':sha(RUNTIME/rel),'after':sha(ROOT/rel)})
    backup=output/'runtime_before'
    backup.mkdir(exist_ok=False)
    for rel in FILES+['release_manifest.json']:
        (backup/rel).parent.mkdir(parents=True,exist_ok=True)
        shutil.copy2(RUNTIME/rel,backup/rel)
    stage=output/'staged_runtime'
    stage.mkdir(exist_ok=False)
    for rel in install.RUNTIME_FILES:
        shutil.copy2(RUNTIME/rel,stage/rel)
    for folder in ['core','configs']:
        (stage/folder).mkdir()
        for p in (RUNTIME/folder).glob('*.py' if folder=='core' else '*.json'):
            shutil.copy2(p,stage/folder/p.name)
    for rel in FILES:
        shutil.copy2(ROOT/rel,stage/rel)
    patch=''.join(''.join(difflib.unified_diff((backup/c['path']).read_text().splitlines(True),
        (ROOT/c['path']).read_text().splitlines(True),fromfile='before/'+c['path'],tofile='after/'+c['path'])) for c in changes)
    with (output/'production.patch').open('x') as f:
        f.write(patch)
    run_tests(output/'source_tests.json',ROOT/'.venv_quant/bin/python')
    run_tests(output/'native_staged_tests.json',install.PYTHON_APP_EXECUTABLE,stage)
    assert before==account_snapshot(),'Account changed during staging; stop to inspect'
    assert sha(RUNTIME/'release_manifest.json')==release_before
    for c in changes:
        assert sha(RUNTIME/c['path'])==c['before'] and sha(ROOT/c['path'])==c['after']
    save(output/'deployment_preview.json',{'changes':changes,'account_before':before,'apply':apply})
    if not apply:
        return
    for c in changes:
        shutil.copy2(stage/c['path'],RUNTIME/c['path'])
    run_tests(output/'native_runtime_tests.json',install.PYTHON_APP_EXECUTABLE,RUNTIME)
    assert before==account_snapshot(),'Account changed; release manifest not advanced'
    assert all(sha(RUNTIME/c['path'])==c['after'] for c in changes)
    manifest=install.release_manifest(RUNTIME)
    manifest['strategy_version']=json.loads((RUNTIME/'configs/intraday_timing_v2.json').read_text())['strategy_version']
    (RUNTIME/'release_manifest.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2))
    save(output/'deployment.json',{'release_id':manifest['release_id'],'deployed_at':str(datetime.now()),
        'changes':changes,'account_unchanged':True,'account':before,'plans_unchanged':True,
        'scheduler_unchanged':True,'report_or_push_or_order_invoked':False})
    print(json.dumps({'release_id':manifest['release_id'],'account_unchanged':True,'files':len(changes)}))


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--apply',action='store_true')
    args=parser.parse_args()
    main(args.output.resolve(),args.apply)
