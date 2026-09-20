"""Frozen-input regression and scoped after-close release; no signal execution."""
import argparse
from collections import Counter
from datetime import datetime, time
import difflib
import hashlib
import importlib.util
import json
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import install_launch_agent as install
from core import entry_quality, method_entry, method_replay, intraday_timing_v2 as timing
from research.deploy_strategy_optimization import account_snapshot

FILES = ('core/entry_quality.py', 'core/timing_replay.py', 'core/method_entry.py',
         'core/intraday_timing_v2.py', 'core/opportunity_rating.py', 'core/signal_contract.py',
         'core/sim_broker.py', 'core/strategy_discipline.py', 'realtime_signal_engine.py',
         'core/observation_strategy_router.py', 'configs/intraday_timing_v2.json')


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None


def save(out, name, data):
    with (out / name).open('x', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2, default=str)


def replay(out, runtime, baseline_method=None):
    old_path = baseline_method or runtime / 'core/method_entry.py'
    spec = importlib.util.spec_from_file_location('before_entry', old_path)
    old = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(old)
    old_hash = sha(old_path)
    summaries, changes = {}, []
    cfg = timing.load_config()
    for day in ('20260909', '20260910', '20260911', '20260914', '20260915'):
        path = runtime / f'data/runtime/signal_audit_{day}.jsonl'
        if not path.exists():
            summaries[day] = {'missing': True}
            continue
        counts = Counter()
        for line in path.open():
            sample = json.loads(line)
            for row in sample.get('rows', []):
                frozen = row.get('method_replay_input') or {}
                if not frozen or frozen.get('capture_error'):
                    counts['missing_method_input'] += 1
                    continue
                if frozen.get('sha256') != method_replay.digest({k:v for k,v in frozen.items() if k!='sha256'}):
                    counts['input_integrity_failed'] += 1
                    continue
                if frozen.get('evaluator_sha256') != old_hash:
                    counts['old_version_not_replayed'] += 1
                    continue
                when = datetime.fromisoformat(frozen['now'])
                before = old.evaluate(frozen['contract'], frozen['bars5'], frozen['price'], when, frozen['config'])
                if before != frozen['expected']:
                    counts['baseline_mismatch'] += 1
                    continue
                after = method_entry.evaluate(frozen['contract'], frozen['bars5'], frozen['price'], when, cfg['daily_method_entry'])
                counts['verified_nodes'] += 1
                counts['old_method_eligible'] += bool(before['eligible'])
                counts['new_method_eligible'] += bool(after['eligible'])
                counts['newly_allowed'] += bool(after['eligible'] and not before['eligible'])
                if before['eligible'] and not after['eligible']:
                    counts['blocked_by_new_evidence'] += 1
                    changes.append({'date': day, 'at': frozen['now'], 'symbol': row.get('symbol'),
                                    'price': frozen['price'], 'method': frozen['contract']['key'],
                                    'blockers': after['blockers']})
        summaries[day] = dict(counts)
    save(out, 'multiday_method_replay.json', {'days': summaries, 'changed_nodes': changes,
        'scope': 'Frozen daily-method branch only; nodes are not orders, trades or win-rate samples. No later market data used.'})
    assert all(not d.get('input_integrity_failed') and not d.get('baseline_mismatch') and not d.get('newly_allowed') for d in summaries.values()), summaries
    path = runtime / 'data/runtime/paper_trading.sqlite'
    with sqlite3.connect(path.as_uri()+'?mode=ro', uri=True) as con:
        orders = con.execute('SELECT symbol,created_at,signal_payload_json FROM paper_orders '
                             'WHERE trading_date=? AND side=? AND status=?', ('2026-09-15','BUY','FILLED')).fetchall()
    results = []
    for code, at, raw in orders:
        signal = json.loads(raw)
        row = {'code': code, 'quote': {'pct':signal['pct'], 'limit_up':signal.get('limit_up')},
               'pressure':signal.get('pressure_price'), 'strategy_contract':signal['strategy_contract'],
               'sector_momentum':signal['sector_momentum'], 'sector_rotation':signal['sector_rotation']}
        frozen = signal['timing_v2'].get('method_replay_input') or {}
        method = None
        if frozen:
            method = method_entry.evaluate(frozen['contract'],frozen['bars5'],frozen['price'],
                                           datetime.fromisoformat(frozen['now']),cfg['daily_method_entry'])
            close = frozen['bars5'][-1]['close']
        else:
            close = None
        target = entry_quality.target_evidence(row,signal['current_price'],close,signal.get('nearest_resistance'))
        identity = entry_quality.leader_identity(row,cfg) if signal['strategy_key']=='LEADER_EMOTION' else {}
        weak = entry_quality.weak_repair_evidence(frozen['contract'],frozen['bars5'],frozen['price'],signal['vwap'],
                 signal['timing_v2']['regime'],signal['timing_v2']['path_state']) if frozen else {}
        results.append({'symbol':code,'filled_at':at,'method_after':method,'targets_after':target,
                        'leader_identity':identity,'weak_repair':weak,
                        'scope':'exact daily-method input' if frozen else 'stored identity/pressure only; no full historical leader replay'})
    save(out,'two_entries_replay.json',results)
    assert len(results)==2
    assert not next(r for r in results if r['symbol']=='603031')['method_after']['eligible']
    bao = next(r for r in results if r['symbol']=='002552')
    assert not bao['leader_identity']['confirmed'] and bao['targets_after']['target']==56.38


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--deploy',action='store_true')
    parser.add_argument('--baseline-method',type=Path)
    args = parser.parse_args()
    runtime = install.RUNTIME_DIR
    out = ROOT/'output'/('entry_evidence_release_'+datetime.now().strftime('%Y%m%d_%H%M%S'))
    out.mkdir()
    print(str(out),flush=True)
    previous = install.release_manifest(runtime)
    changes = [{'path':name,'before':sha(runtime/name),'after':sha(ROOT/name)} for name in FILES]
    initial_account = account_snapshot()
    save(out,'baseline.json',{'runtime':previous,'changes':changes,'account':initial_account})
    backup = out/'backup'
    for name in (*FILES,'release_manifest.json'):
        if (runtime/name).exists():
            (backup/name).parent.mkdir(parents=True,exist_ok=True)
            shutil.copy2(runtime/name,backup/name)
    replay(out,runtime,args.baseline_method)
    stage = out/'stage'; stage.mkdir()
    for rel in install.RUNTIME_FILES:
        shutil.copy2(runtime/rel,stage/rel)
    for folder, pattern in (('core','*.py'),('configs','*.json')):
        (stage/folder).mkdir()
        for path in (runtime/folder).glob(pattern):
            shutil.copy2(path,stage/folder/path.name)
    for name in FILES:
        shutil.copy2(ROOT/name,stage/name)
    patch = ''.join(''.join(difflib.unified_diff(
        (backup/c['path']).read_text().splitlines(True) if c['before'] else [],
        (stage/c['path']).read_text().splitlines(True),fromfile='before/'+c['path'],tofile='after/'+c['path'])) for c in changes)
    with (out/'production.patch').open('x') as f:
        f.write(patch)
    def run(name,cmd):
        r = subprocess.run(cmd,cwd=ROOT,capture_output=True,text=True,timeout=300)
        save(out,name,{'returncode':r.returncode,'output':r.stdout+r.stderr})
        assert r.returncode==0,r.stdout+r.stderr
    run('source_tests.json',[str(ROOT/'.venv_quant/bin/python'),'-m','unittest','discover','-s','tests','-q'])
    run('staged_native_tests.json',[str(install.PYTHON_APP_EXECUTABLE),str(ROOT/'research/run_strategy_suite.py'),'--runtime',str(stage)])
    assert account_snapshot()==initial_account,'Account changed during validation; inspect'
    if not args.deploy:
        return
    assert datetime.now().time() >= time(15,15),'After-close deployment only'
    processes = subprocess.run(['pgrep','-fl','realtime_signal_engine.py'],capture_output=True,text=True)
    assert processes.returncode==1,'Realtime process running; do not replace a multi-file release mid-cycle'
    assert all(sha(runtime/c['path'])==c['before'] and sha(ROOT/c['path'])==c['after'] for c in changes)
    assert all(sha(runtime/name)==value for name,value in previous['files'].items())
    before_account = account_snapshot()
    for change in changes:
        if change['before'] != change['after']:
            shutil.copy2(stage/change['path'],runtime/change['path'])
    run('installed_native_tests.json',[str(install.PYTHON_APP_EXECUTABLE),str(ROOT/'research/run_strategy_suite.py'),'--runtime',str(runtime)])
    assert account_snapshot()==before_account,'Account changed during release; inspect'
    assert all(sha(runtime/c['path'])==c['after'] for c in changes)
    assert all(sha(runtime/name)==value for name,value in previous['files'].items() if name not in FILES)
    old = json.loads((backup/'release_manifest.json').read_text())
    manifest = install.release_manifest(runtime)
    for key,value in old.items():
        if key not in ('release_id','generated_at','files','strategy_version'):
            manifest[key]=value
    manifest['strategy_version']=timing.load_config()['strategy_version']
    manifest['entry_evidence_release']=out.name
    (runtime/'release_manifest.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2))
    save(out,'acceptance.json',{'release_id':manifest['release_id'],'deployed_at':str(datetime.now()),
          'files':changes,'account_unchanged':True,'other_runtime_files_unchanged':True,
          'manual_orders':0,'manual_pushes':0,'activation':'next scheduled process start',
          'restart_needed':False,'live_engine_was_running':False})
    print('Deployment verified: '+manifest['release_id'],flush=True)


if __name__=='__main__':
    main()
