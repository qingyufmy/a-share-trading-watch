"""Read-only closing performance; delivery state is separate from the trading ledger."""
import json
import math
import os
from pathlib import Path
import sqlite3
from datetime import datetime, time
import urllib.request

import paper_trading
from core import ledger_reconciliation


def rows(con, sql, args=()):
    cursor = con.execute(sql, args)
    names = [column[0] for column in cursor.description]
    return [dict(zip(names, row)) for row in cursor.fetchall()]


def number(value):
    try:
        value = float(value)
        return value if math.isfinite(value) else None
    except (ValueError, TypeError):
        return None


def ratio(pnl, base):
    return pnl / base * 100 if pnl is not None and base and base > 0 else None


def calculate(con, quotes, now, previous_date, initial_cash):
    day, at = now.strftime('%Y-%m-%d'), now.strftime('%Y-%m-%d %H:%M:%S')
    positions = rows(con, 'SELECT * FROM paper_positions WHERE quantity>0')
    orders = rows(con, "SELECT * FROM paper_orders WHERE status IN ('FILLED','PARTIAL_FILLED') AND created_at<=? ORDER BY created_at,order_id", (at,))
    quality = paper_trading.ledger_quality(con)
    previous = paper_trading.previous_account_snapshot(con, day, initial_cash)
    prior_ok = bool(previous and previous['trading_date'] == previous_date
                    and previous['snapshot_at'][11:19] >= '15:00:00')
    prior_positions = rows(con, 'SELECT * FROM paper_position_snapshots WHERE snapshot_at=? AND trading_date=?',
                           (previous['snapshot_at'], previous_date)) if prior_ok else []
    prior_map = {p['symbol']: p for p in prior_positions}
    prior_ok = prior_ok and abs(sum(p['market_value'] for p in prior_positions) - previous['market_value']) < .02
    cutoff = ledger_reconciliation.latest_cutoff(con)
    if cutoff and (not previous or previous['snapshot_at'] < cutoff):
        prior_ok = False
    seeds = rows(con, "SELECT * FROM paper_seed_registry WHERE status='verified' AND imported_at<=?", (at,))
    seed_total = sum(s['capital_value'] for s in seeds)
    seed_today = sum(s['capital_value'] for s in seeds if s['imported_at'][:10] == day)
    pos_map = {p['symbol']: p for p in positions}
    today = [o for o in orders if o['created_at'][:10] == day]
    selected = set(pos_map) | {o['symbol'] for o in today} | set(prior_map)
    issues, details = [], []
    total_value = 0.0
    for code in sorted(selected):
        pos, prior = pos_map.get(code, {}), prior_map.get(code, {})
        qty = int(pos.get('quantity') or 0)
        quote = quotes.get(code) or {}
        price = number(quote.get('close'))
        stamp = str(quote.get('datetime') or '')
        try:
            quote_at = datetime.strptime(stamp, '%Y%m%d%H%M%S')
            valid = quote_at.date() == now.date() and time(15) <= quote_at.time() and quote_at <= now
        except ValueError:
            valid = False
        marked = qty == 0 or (valid and price is not None and price > 0)
        mv = qty * price if qty and marked else 0.0 if not qty else None
        if not marked:
            issues.append(f'{code}缺少当日收盘报价')
        else:
            total_value += mv
        trades = [o for o in orders if o['symbol'] == code]
        current_trades = [o for o in trades if o['created_at'][:10] == day]
        def flow(items):
            return sum((1 if o['side'] == 'SELL' else -1) * o['qty'] * o['fill_price'] - o['fees_total'] for o in items)
        contributions = sum(s['capital_value'] for s in seeds if s['symbol'] == code)
        today_contributions = sum(s['capital_value'] for s in seeds if s['symbol'] == code and s['imported_at'][:10] == day)
        day_pnl = mv - float(prior.get('market_value') or 0) + flow(current_trades) - today_contributions if marked and prior_ok and quality['ready'] else None
        cumulative = mv + flow(trades) - contributions if marked and quality['ready'] else None
        day_base = float(prior.get('market_value') or 0) + sum(o['qty'] * o['fill_price'] + o['fees_total'] for o in current_trades if o['side'] == 'BUY') + today_contributions
        cumulative_base = sum(o['qty'] * o['fill_price'] + o['fees_total'] for o in trades if o['side'] == 'BUY') + contributions
        unrealized = mv - qty * pos['avg_cost'] if marked and qty and pos.get('avg_cost') is not None and quality['ready'] else 0.0 if not qty and quality['ready'] else None
        details.append({'symbol': code, 'name': pos.get('name') or prior.get('name') or (trades[-1]['name'] if trades else code),
                        'quantity': qty, 'sellable': pos.get('sellable', 0), 'avg_cost': pos.get('avg_cost'),
                        'close': price if marked and qty else None, 'market_value': mv,
                        'day_pnl': day_pnl, 'day_return_pct': ratio(day_pnl, day_base),
                        'cumulative_pnl': cumulative, 'cumulative_return_pct': ratio(cumulative, cumulative_base),
                        'unrealized_pnl': unrealized, 'fees_today': sum(o['fees_total'] for o in current_trades),
                        'buy_qty': sum(o['qty'] for o in current_trades if o['side'] == 'BUY'),
                        'sell_qty': sum(o['qty'] for o in current_trades if o['side'] == 'SELL')})
    cash = paper_trading.cash_from_filled_orders(con, initial_cash, cutoff_at=at)
    assets = cash + total_value if not issues else None
    cumulative = assets - initial_cash - seed_total if assets is not None and quality['ready'] else None
    daily = assets - previous['total_assets'] - seed_today if assets is not None and prior_ok and quality['ready'] else None
    if not quality['ready']:
        issues.append(quality['reason'])
    if not prior_ok:
        issues.append('缺少可比的上一交易日收盘快照，当日收益暂不可用')
    if daily is not None and abs(sum(p['day_pnl'] for p in details) - daily) > .02:
        issues.append('逐票当日盈亏与账户变化未对平，当日收益暂不可用')
        daily = None
        for p in details:
            p['day_pnl'] = p['day_return_pct'] = None
    unrealized = sum(p['unrealized_pnl'] for p in details) if all(p['unrealized_pnl'] is not None for p in details) else None
    return {'trading_date': day, 'generated_at': at, 'issues': issues,
            'account': {'initial_cash': initial_cash, 'cash': cash, 'market_value': total_value if assets is not None else None,
                        'total_assets': assets, 'position_pct': ratio(total_value, assets),
                        'day_pnl': daily, 'day_return_pct': ratio(daily, previous['total_assets'] if prior_ok else None),
                        'cumulative_pnl': cumulative, 'cumulative_return_pct': ratio(cumulative, initial_cash + seed_total),
                        'unrealized_pnl': unrealized, 'realized_pnl': cumulative - unrealized if cumulative is not None and unrealized is not None else None,
                        'fees_today': sum(o['fees_total'] for o in today), 'fills_today': len(today)},
            'positions': details}


def fmt(value, percent=False):
    return '暂不可用' if value is None else f'{value:+,.2f}' + ('%' if percent else '元')


def cards(report):
    a, details = report['account'], report['positions']
    groups = [details[i:i+8] for i in range(0, len(details), 8)] or [[]]
    output = []
    for i, group in enumerate(groups, 1):
        summary = (f"**账户总览**\n总资产 {fmt(a['total_assets'])}｜现金 {fmt(a['cash'])}\n"
                   f"持仓市值 {fmt(a['market_value'])}｜仓位 {fmt(a['position_pct'], True)}\n"
                   f"**当日收益 {fmt(a['day_pnl'])}（{fmt(a['day_return_pct'], True)}）**\n"
                   f"**累计收益 {fmt(a['cumulative_pnl'])}（{fmt(a['cumulative_return_pct'], True)}）**\n"
                   f"累计已实现 {fmt(a['realized_pnl'])}｜当前浮盈亏 {fmt(a['unrealized_pnl'])}\n"
                   f"今日成交 {a['fills_today']}笔｜已计费用 {fmt(a['fees_today'])}")
        lines = []
        for p in group:
            lines.append(f"**{p['name']}（{p['symbol']}）｜{'持仓' if p['quantity'] else '已清仓'}**\n"
                         f"持有 {p['quantity']}股｜可卖 {p['sellable']}股｜收盘 {fmt(p['close'])}\n"
                         f"当日 {fmt(p['day_pnl'])}（{fmt(p['day_return_pct'], True)}）\n"
                         f"逐票累计 {fmt(p['cumulative_pnl'])}（{fmt(p['cumulative_return_pct'], True)}）｜当前浮盈亏 {fmt(p['unrealized_pnl'])}\n"
                         f"今日买入 {p['buy_qty']}股 / 卖出 {p['sell_qty']}股")
        notes = ('**收益口径**\n账户当日：较上一交易日收盘净资产，扣除已核验外部转入。累计：较初始资金与已核验转入。\n'
                 '逐票当日包含今日卖出盈亏；当日收益率分母为昨持仓市值加今日买入成本。逐票累计含历史已实现与当前浮盈亏，收益率分母为历次买入及转入成本，不是年化或时间加权收益。\n'
                 '收益已扣账本记录费用；9月11日前历史保持原计费口径。模拟账户，不是实盘收益，也不是下单信号。')
        elements = [{'tag': 'div', 'text': {'tag': 'lark_md', 'content': s}} for s in
                    [summary, '\n\n'.join(lines) or '当前空仓，今日无逐票持仓变动。', notes]]
        if report['issues']:
            elements.insert(0, {'tag': 'div', 'text': {'tag': 'lark_md', 'content': '**数据待核验**\n' + '\n'.join(report['issues'])}})
        output.append({'msg_type': 'interactive', 'card': {'config': {'wide_screen_mode': True},
                      'header': {'template': 'grey' if report['issues'] else 'blue',
                                 'title': {'tag': 'plain_text', 'content': f"模拟盘收盘表现｜{report['trading_date']}｜{i}/{len(groups)}"}},
                      'elements': elements}})
    return output


def deliver(report, state_path, sender, skip=False):
    if skip:
        return {'code': 0, 'msg': 'preview only; no delivery state changed'}
    state_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(state_path) as con:
        con.execute('CREATE TABLE IF NOT EXISTS reports(day TEXT PRIMARY KEY,payload TEXT NOT NULL)')
        con.execute('CREATE TABLE IF NOT EXISTS deliveries(day TEXT,part INTEGER,status TEXT,result TEXT,PRIMARY KEY(day,part))')
        con.execute('INSERT OR IGNORE INTO reports VALUES (?,?)', (report['trading_date'], json.dumps(report, ensure_ascii=False)))
        con.commit()
        frozen = json.loads(con.execute('SELECT payload FROM reports WHERE day=?', (report['trading_date'],)).fetchone()[0])
        payloads = cards(frozen)
        for part, payload in enumerate(payloads):
            con.execute('BEGIN IMMEDIATE')
            row = con.execute('SELECT status FROM deliveries WHERE day=? AND part=?', (report['trading_date'], part)).fetchone()
            if row and row[0] == 'sent':
                con.commit()
                continue
            if row and row[0] in ('sending', 'uncertain'):
                con.rollback()
                raise RuntimeError('飞书发送结果待核验；为避免重复，本日该分页不自动重发')
            con.execute('INSERT OR REPLACE INTO deliveries VALUES (?,?,?,?)', (report['trading_date'], part, 'sending', ''))
            con.commit()
            try:
                result = sender(payload)
                code = result.get('code', result.get('StatusCode'))
                status = 'sent' if code == 0 else 'failed' if code is not None else 'uncertain'
            except Exception:
                con.execute('UPDATE deliveries SET status=?,result=? WHERE day=? AND part=?',
                            ('uncertain', 'transport error; response unknown', report['trading_date'], part))
                con.commit()
                raise RuntimeError('飞书响应未确认，停止自动重发并保留核验状态') from None
            con.execute('UPDATE deliveries SET status=?,result=? WHERE day=? AND part=?',
                        (status, json.dumps(result, ensure_ascii=False), report['trading_date'], part))
            con.commit()
            if status != 'sent':
                raise RuntimeError(f'飞书收盘表现卡发送未成功，状态{status}，返回码{code}')
    return {'code': 0, 'msg': 'all parts delivered or already delivered', 'parts': len(payloads)}


def main():
    from after_close_report import (current_datetime, previous_trading_date, is_a_share_trading_day,
                                    parse_tencent_quotes, infer_market, FEISHU_WEBHOOK)
    now = current_datetime()
    if not is_a_share_trading_day(now.date()) or now.time() < time(15, 15):
        print(json.dumps({'feishu_result': {'code': 0, 'msg': 'outside trading-day close window'}}))
        return
    base = Path(os.environ.get('A_SHARE_BASE_DIR', Path(__file__).resolve().parent))
    db = base / 'data/runtime/paper_trading.sqlite'
    with sqlite3.connect(db.as_uri() + '?mode=ro', uri=True) as con:
        con.execute('BEGIN')
        codes = [r[0] for r in con.execute('SELECT symbol FROM paper_positions WHERE quantity>0')]
        quotes = parse_tencent_quotes([(c, infer_market(c)) for c in codes]) if codes else {}
        report = calculate(con, quotes, now, previous_trading_date(now.strftime('%Y-%m-%d')), paper_trading.paper_initial_cash())
    archive = base / 'data/runtime/paper_close_reports'
    archive.mkdir(parents=True, exist_ok=True)
    path = archive / (now.strftime('%Y%m%d_%H%M%S_%f') + '.json')
    with path.open('x', encoding='utf-8') as handle:
        json.dump(report, handle, ensure_ascii=False, indent=2)
    def sender(payload):
        request = urllib.request.Request(FEISHU_WEBHOOK, data=json.dumps(payload, ensure_ascii=False).encode('utf-8'),
                                         headers={'Content-Type': 'application/json'})
        with urllib.request.urlopen(request, timeout=20) as response:
            return json.loads(response.read().decode('utf-8'))
    result = deliver(report, base / 'data/runtime/paper_close_delivery.sqlite', sender,
                     skip=os.environ.get('A_SHARE_SKIP_FEISHU') == '1' or os.environ.get('A_SHARE_DISABLE_FEISHU') == '1')
    print(json.dumps({'report': str(path), 'feishu_result': result}, ensure_ascii=False))


if __name__ == '__main__':
    main()
