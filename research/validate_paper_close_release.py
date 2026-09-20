"""Validate a read-only closing card and install only its scheduled report path."""
import argparse
from datetime import datetime
import hashlib
import json
from pathlib import Path
import plistlib
import shutil
import sqlite3
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import install_launch_agent as install
import paper_close_report as report
import paper_trading
from research.deploy_strategy_optimization import account_snapshot


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def save(out, name, value):
    with (out/name).open('x', encoding='utf-8') as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2)


def main(out, apply):
    from after_close_report import parse_tencent_quotes, previous_trading_date, infer_market
    out.mkdir()
    runtime = install.RUNTIME_DIR
    before = account_snapshot()
    old_manifest = json.loads((runtime/'release_manifest.json').read_text())
    with sqlite3.connect((runtime/'data/runtime/paper_trading.sqlite').as_uri()+'?mode=ro', uri=True) as con:
        con.execute('BEGIN')
        codes = [r[0] for r in con.execute('SELECT symbol FROM paper_positions WHERE quantity>0')]
        quotes = parse_tencent_quotes([(c, infer_market(c)) for c in codes]) if codes else {}
        now = datetime.now()
        result = report.calculate(con, quotes, now, previous_trading_date(now.strftime('%Y-%m-%d')), paper_trading.paper_initial_cash())
    save(out, 'today_preview.json', result)
    save(out, 'today_card_preview.json', report.cards(result))
    report.deliver(result, out/'preview_delivery.sqlite', lambda _: (_ for _ in ()).throw(AssertionError('No push')), skip=True)
    assert not (out/'preview_delivery.sqlite').exists()
    with (out/'今日模拟盘收盘表现预览.md').open('x', encoding='utf-8') as handle:
        handle.write('# 今日模拟盘收盘表现（本地预览，未推送）\n\n')
        for card in report.cards(result):
            for element in card['card']['elements']:
                handle.write(element['text']['content']+'\n\n')
    stage = out/'staged_runtime'
    stage.mkdir()
    for rel in install.RUNTIME_FILES:
        if (runtime/rel).exists():
            shutil.copy2(runtime/rel, stage/rel)
    for folder in ('core','configs'):
        (stage/folder).mkdir()
        for source in (runtime/folder).glob('*.py' if folder=='core' else '*.json'):
            shutil.copy2(source, stage/folder/source.name)
    changes = ('paper_close_report.py','run_trading_job.py')
    for rel in changes:
        shutil.copy2(ROOT/rel, stage/rel)
    for name, cmd in [('source_tests.json', [str(ROOT/'.venv_quant/bin/python'), '-m','unittest','discover','-s','tests','-q']),
                      ('native_tests.json', [str(install.PYTHON_APP_EXECUTABLE),str(ROOT/'research/run_strategy_suite.py'),'--runtime',str(stage)])]:
        test = subprocess.run(cmd, cwd=ROOT, text=True, capture_output=True, timeout=180)
        save(out,name,{'exit_code':test.returncode,'output':test.stdout+test.stderr})
        assert test.returncode == 0, test.stdout+test.stderr
    assert account_snapshot() == before
    scheduler = plistlib.loads(install.PLIST_PATH.read_bytes())
    assert scheduler.get('StartInterval') == 60
    assert scheduler.get('EnvironmentVariables',{}).get('A_SHARE_DISABLE_FEISHU') == '0'
    hashes = {p:sha(runtime/p) for p in old_manifest['files'] if (runtime/p).exists()}
    save(out,'acceptance_preview.json',{'account_unchanged':True,'no_push':True,'existing_hashes':hashes,
                                      'scheduled_time':'15:15 trading days', 'issues':result['issues']})
    if not apply:
        return
    backup = out/'runtime_backup'
    backup.mkdir()
    for rel in changes+('release_manifest.json',):
        if (runtime/rel).exists():
            shutil.copy2(runtime/rel,backup/rel)
    assert all(sha(runtime/p)==digest for p,digest in hashes.items())
    for rel in changes:
        shutil.copy2(stage/rel,runtime/rel)
    test = subprocess.run([str(install.PYTHON_APP_EXECUTABLE),str(ROOT/'research/run_strategy_suite.py'),'--runtime',str(runtime)],
                          cwd=ROOT,text=True,capture_output=True,timeout=180)
    save(out,'installed_native_tests.json',{'exit_code':test.returncode,'output':test.stdout+test.stderr})
    assert test.returncode == 0, test.stdout+test.stderr
    assert account_snapshot() == before
    assert all(sha(runtime/p)==digest for p,digest in hashes.items() if p not in changes)
    assert all(sha(runtime/p)==sha(ROOT/p) for p in changes)
    assert plistlib.loads(install.PLIST_PATH.read_bytes()) == scheduler
    manifest = install.release_manifest(runtime)
    for key,value in old_manifest.items():
        if key not in ('release_id','generated_at','files'):
            manifest[key] = value
    manifest['paper_close_report_version'] = 'daily_close_performance_20260914'
    (runtime/'release_manifest.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2))
    save(out,'deployment.json',{'deployed_at':str(datetime.now()),'release_id':manifest['release_id'],
                              'changes':list(changes),'account_unchanged':True,'trading_logic_unchanged':True,
                              'schedule':'trading days 15:15, recovery window until 16:30',
                              'scheduler_plist_unchanged':True,'no_manual_push':True})
    print(json.dumps({'account':result['account'],'issues':result['issues'],'release_id':manifest['release_id']},ensure_ascii=False))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--apply',action='store_true')
    args=parser.parse_args()
    main(args.output.resolve(),args.apply)
