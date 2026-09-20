"""Explicitly authorized correction of stale reimports, never a broker order."""
import hashlib
import json
import math
from decimal import Decimal


def ensure_schema(con):
    con.execute('''CREATE TABLE IF NOT EXISTS paper_reconciliations (
        reconciliation_id TEXT PRIMARY KEY, symbol TEXT NOT NULL,
        created_at TEXT NOT NULL, affected_from TEXT NOT NULL,
        confirmation TEXT NOT NULL, before_json TEXT NOT NULL, result_json TEXT NOT NULL
    )''')


def latest_cutoff(con):
    if not con.execute("SELECT 1 FROM sqlite_master WHERE name='paper_reconciliations'").fetchone():
        return None
    return con.execute('SELECT MAX(created_at) FROM paper_reconciliations').fetchone()[0]


def _rows(con, sql, args):
    cursor = con.execute(sql, args)
    columns = [item[0] for item in cursor.description]
    return [dict(zip(columns, row)) for row in cursor.fetchall()]


def preview(con, symbol):
    """Require a fully closed funded position; ambiguous partial oversells stop."""
    positions = _rows(con, 'SELECT * FROM paper_positions WHERE symbol=?', (symbol,))
    registry = _rows(con, 'SELECT * FROM paper_seed_registry WHERE symbol=?', (symbol,))
    orders = _rows(con, 'SELECT * FROM paper_orders WHERE symbol=? ORDER BY created_at, order_id', (symbol,))
    if len(positions) != 1 or positions[0]['source'] != 'positions_file' or positions[0]['quantity'] <= 0:
        raise ValueError('Expected a positive stale positions_file inventory')
    if len(registry) != 1 or registry[0]['status'] != 'legacy_review' or registry[0]['capital_value']:
        raise ValueError('Expected unverified legacy inventory with no capital contribution')
    balance, bought, invalid = 0, 0, []
    closed_at = None
    for order in orders:
        if order['status'] == 'PARTIAL_FILLED':
            raise ValueError('Partial fill history requires separate fill-level reconciliation')
        if order['status'] != 'FILLED':
            continue
        qty, price = order['qty'], order['fill_price']
        if not isinstance(qty, int) or qty <= 0 or price is None or not math.isfinite(price) or price <= 0:
            raise ValueError('Invalid filled quantity or price')
        if order['side'] == 'BUY':
            if invalid:
                raise ValueError('Later buys require a separate reconciliation')
            balance += qty
            bought += qty
        elif order['side'] == 'SELL':
            if qty > balance:
                if balance or not bought:
                    raise ValueError('Unfunded or partially oversold history is ambiguous')
                invalid.append(order)
            else:
                balance -= qty
                if balance == 0:
                    closed_at = order['created_at']
        else:
            raise ValueError('Unexpected filled order side')
    if balance != 0 or not invalid or not closed_at:
        raise ValueError('Expected full liquidation followed by duplicate-inventory sales')
    before = {'position': positions[0], 'registry': registry[0], 'orders': orders}
    digest = hashlib.sha256(json.dumps(before, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    reversal = sum((Decimal(str(o['fill_price'])) * o['qty'] - Decimal(str(o.get('fees_total') or 0))
                    for o in invalid), Decimal(0))
    return {'symbol': symbol, 'digest': digest, 'before': before, 'closed_at': closed_at,
            'invalid_order_ids': [o['order_id'] for o in invalid],
            'invalid_quantity': sum(o['qty'] for o in invalid),
            'cash_reversal': float(reversal.quantize(Decimal('.01'))),
            'position_before': positions[0]['quantity'], 'position_after': 0,
            'affected_from': invalid[0]['created_at']}


def apply(con, symbol, reconciliation_id, expected_digest, *, no_external_transfer,
          no_manual_adjustment, confirmation, now):
    if no_external_transfer is not True or no_manual_adjustment is not True or not str(confirmation).strip():
        raise ValueError('Explicit user confirmation of no transfers or manual changes is required')
    if not reconciliation_id or con.in_transaction:
        raise ValueError('An id and a standalone transaction are required')
    at = now.strftime('%Y-%m-%d %H:%M:%S')
    con.execute('BEGIN IMMEDIATE')
    try:
        ensure_schema(con)
        existing = con.execute('SELECT symbol, result_json FROM paper_reconciliations WHERE reconciliation_id=?',
                               (reconciliation_id,)).fetchone()
        if existing:
            if existing[0] != symbol:
                raise ValueError('Reconciliation id belongs to another symbol')
            con.commit()
            return {**json.loads(existing[1]), 'already_applied': True}
        plan = preview(con, symbol)
        if plan['digest'] != expected_digest:
            raise ValueError('Ledger changed since preview; correction not applied')
        if at <= max(o['created_at'] for o in plan['before']['orders']):
            raise ValueError('Correction must follow the recorded history')
        for order_id in plan['invalid_order_ids']:
            changed = con.execute('''UPDATE paper_orders SET status='VOID_RECONCILED',
                reason=COALESCE(reason,'') || ? WHERE order_id=? AND status='FILLED' ''',
                ('; 历史重复导入对账作废（非真实撤单），审计=' + reconciliation_id, order_id))
            if changed.rowcount != 1:
                raise ValueError('Order changed during reconciliation')
        con.execute('''UPDATE paper_positions SET quantity=0, sellable=0, source='paper_reconciled', updated_at=?
                       WHERE symbol=?''', (at, symbol))
        con.execute('''UPDATE paper_seed_registry SET quantity=0, status='reconciled_no_transfer',
                       capital_value=0, note=? WHERE symbol=?''',
                    ('User confirmed no transfers/manual adjustments; audit=' + reconciliation_id, symbol))
        result = {k: v for k, v in plan.items() if k != 'before'}
        result.update(reconciliation_id=reconciliation_id, created_at=at,
                      order_status='VOID_RECONCILED', already_applied=False)
        con.execute('INSERT INTO paper_reconciliations VALUES (?,?,?,?,?,?,?)',
                    (reconciliation_id, symbol, at, plan['affected_from'], str(confirmation),
                     json.dumps(plan['before'], ensure_ascii=False), json.dumps(result, ensure_ascii=False)))
        con.commit()
        return result
    except Exception:
        con.rollback()
        raise
