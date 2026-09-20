"""Audited one-time paper-ledger repair authorized by the user's confirmation."""
import argparse
import hashlib
import json
import shutil
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = Path.home() / 'Library/Application Support/a-share-trading-watch'
OUT = ROOT / 'output/ledger_reconcile_20260907_183559'
REL = 'data/runtime/paper_trading.sqlite'
SNAPSHOT = 'web_dashboard/data/runtime/paper_trading.json'
FILES = ['core/ledger_reconciliation.py', 'paper_trading.py']
CONFIRMATION = '用户2026-09-07明确确认：中国稀土没有外部转入和手工调整，继续完成修改。'
IDENTITY = '20260907-000831-no-external-transfer'


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def save(name, value):
    with (OUT / name).open('x', encoding='utf-8') as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2, default=str)


def db_snapshot(source, destination):
    assert not destination.exists()
    destination.parent.mkdir(parents=True, exist_ok=True)
    original = sqlite3.connect(f'file:{source}?mode=ro', uri=True)
    copy = sqlite3.connect(destination)
    try:
        original.backup(copy)
        assert copy.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
    finally:
        original.close()
        copy.close()


def prepare(paper, ledger):
    backup = OUT / 'backup/paper_trading_before.sqlite'
    db_snapshot(RUNTIME / REL, backup)
    shutil.copy2(RUNTIME / SNAPSHOT, OUT / 'backup/paper_trading_latest_before.json')
    con = sqlite3.connect(f'file:{backup}?mode=ro', uri=True)
    try:
        plan = ledger.preview(con, '000831')
        plan['cash_before'] = paper.cash_from_filled_orders(con)
        plan['order_count_before'] = con.execute('SELECT COUNT(*) FROM paper_orders').fetchone()[0]
        plan['positions_before'] = con.execute('SELECT * FROM paper_positions ORDER BY symbol').fetchall()
    finally:
        con.close()
    save('preview.json', plan)
    shadow = OUT / 'shadow'
    db_snapshot(backup, shadow / REL)
    con = paper.init_db(shadow)
    try:
        unchanged_tables = ('paper_buy_lots', 'paper_position_snapshots', 'paper_account_snapshots')
        original = {t: con.execute('SELECT * FROM ' + t).fetchall() for t in unchanged_tables}
        untouched_orders = con.execute("SELECT * FROM paper_orders WHERE symbol!='000831'").fetchall()
        result = ledger.apply(con, '000831', IDENTITY, plan['digest'], no_external_transfer=True,
                              no_manual_adjustment=True, confirmation=CONFIRMATION, now=datetime.now())
        assert paper.get_position(con, '000831')['quantity'] == 0
        assert paper.ledger_quality(con)['ready']
        assert abs(plan['cash_before'] - paper.cash_from_filled_orders(con) - plan['cash_reversal']) < .001
        assert con.execute('SELECT COUNT(*) FROM paper_orders').fetchone()[0] == plan['order_count_before']
        assert con.execute("SELECT * FROM paper_orders WHERE symbol!='000831'").fetchall() == untouched_orders
        assert all(con.execute('SELECT * FROM ' + t).fetchall() == original[t] for t in unchanged_tables)
        save('shadow_acceptance.json', {'passed':True, 'result':result,
                'cash_after':paper.cash_from_filled_orders(con), 'ledger_quality':paper.ledger_quality(con),
                'historical_snapshots_unchanged':True, 'other_orders_unchanged':True})
    finally:
        con.close()
    print('Shadow accepted:', len(plan['invalid_order_ids']), 'void corrections, cash reversal', plan['cash_reversal'])


def deploy():
    import install_launch_agent
    assert json.loads((OUT/'source_tests.json').read_text())['passed']
    assert json.loads((OUT/'shadow_acceptance.json').read_text())['passed']
    assert not (OUT/'deployment.json').exists()
    manifest_before = RUNTIME / 'release_manifest.json'
    assert sha(manifest_before) == sha(OUT/'backup/runtime/release_manifest.json')
    changes = []
    for rel in FILES:
        target = RUNTIME / rel
        if target.exists():
            assert sha(target) == sha(OUT/'backup/runtime'/rel), 'Runtime changed: ' + rel
        else:
            assert rel == 'core/ledger_reconciliation.py'
        changes.append({'file':rel, 'before':sha(target) if target.exists() else None, 'after':sha(ROOT/rel)})
    for rel in FILES:
        shutil.copy2(ROOT/rel, RUNTIME/rel)
    manifest = install_launch_agent.release_manifest(RUNTIME)
    assert all(manifest['files'][rel] == sha(ROOT/rel) == sha(RUNTIME/rel) for rel in FILES)
    manifest_before.write_text(json.dumps(manifest,ensure_ascii=False,indent=2),encoding='utf-8')
    save('deployment.json', {'files':changes, 'release_id':manifest['release_id']})
    print('Deployed', manifest['release_id'])


def correct(paper, ledger):
    assert json.loads((OUT/'runtime_tests.json').read_text())['passed']
    assert Path(paper.__file__).resolve() == RUNTIME/'paper_trading.py'
    assert Path(ledger.__file__).resolve() == RUNTIME/'core/ledger_reconciliation.py'
    assert not (OUT/'correction_applied.json').exists()
    previous = OUT/'backup/paper_trading_latest_before.json'
    assert sha(RUNTIME/SNAPSHOT) == sha(previous), 'Latest snapshot changed; re-preview required'
    plan = json.loads((OUT/'preview.json').read_text())
    current = datetime.now()
    con = paper.init_db(RUNTIME)
    try:
        result = ledger.apply(con, '000831', IDENTITY, plan['digest'], no_external_transfer=True,
                              no_manual_adjustment=True, confirmation=CONFIRMATION, now=current)
        assert paper.get_position(con, '000831')['quantity'] == 0
        assert paper.ledger_quality(con)['ready']
        assert con.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
        assert con.execute('SELECT COUNT(*) FROM paper_orders').fetchone()[0] == plan['order_count_before']
        save('correction_applied.json', {'result':result, 'cash_after':paper.cash_from_filled_orders(con),
                                       'ledger_quality':paper.ledger_quality(con), 'order_count_unchanged':True})
    finally:
        con.close()
    # Refresh only the current account view with preserved close marks. No report or push.
    old = json.loads(previous.read_text())
    prices = {p['symbol']: {k:p.get(k) for k in ('last_price','prev_close','pct','name')}
              for p in old['positions']}
    assert all(p.get('last_price') and p['last_price'] > 0 for p in prices.values())
    latest = paper.write_latest_snapshot(RUNTIME, trading_date='2026-09-07', price_map=prices, now=current)
    assert all(p['symbol'] != '000831' for p in latest['positions'])
    assert latest['account']['ledger_quality']['ready']
    assert latest['account']['day_pnl'] is None
    save('current_account_verified.json', {'account':latest['account'], 'positions':latest['positions'],
                    'status_counts':latest['status_counts'], 'price_source':'preserved Sep 7 close marks',
                    'historical_reports_rewritten':False, 'broker_orders_or_feishu_sent':False})
    print('Corrected account:', {k:latest['account'][k] for k in ('cash','market_value','total_assets','day_pnl')})


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=['prepare','deploy','apply'])
    action = parser.parse_args().action
    sys.path.insert(0,str(RUNTIME if action == 'apply' else ROOT))
    import paper_trading as paper
    from core import ledger_reconciliation as ledger
    if action == 'prepare':
        prepare(paper,ledger)
    elif action == 'deploy':
        deploy()
    else:
        correct(paper,ledger)
