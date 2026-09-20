import json
import unittest
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import paper_trading as paper
from core import ledger_reconciliation as ledger

NOW = datetime(2026, 9, 7, 18, 40)
FILLS = [
    ('2026-08-12', 'BUY', 500, 56.73), ('2026-08-27', 'SELL', 200, 55.88),
    ('2026-08-28 10:15:11', 'SELL', 100, 59.98), ('2026-08-28 13:45:10', 'SELL', 100, 60.16),
    ('2026-08-31', 'SELL', 100, 59.28), ('2026-09-01', 'SELL', 100, 59.56),
    ('2026-09-02', 'SELL', 100, 57.78), ('2026-09-03', 'SELL', 100, 58.13),
    ('2026-09-04', 'SELL', 100, 57.36), ('2026-09-07', 'SELL', 100, 55.86),
]


class ReconciliationTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.con = paper.init_db(self.root)
        self.addCleanup(self.con.close)
        for index, (at, side, qty, price) in enumerate(FILLS):
            at = at if len(at) > 10 else at + ' 09:45:00'
            self.con.execute('''INSERT INTO paper_orders
                (order_id,trading_date,created_at,symbol,side,qty,fill_price,status,reason)
                VALUES (?,?,?,?,?,?,?,'FILLED','original evidence')''',
                (str(index), at[:10], at, '000831', side, qty, price))
        self.con.execute("INSERT INTO paper_positions VALUES ('000831','中国稀土',100,100,56.73,'positions_file','2026-09-07 15:01:02')")
        self.con.execute("INSERT INTO paper_seed_registry VALUES ('000831',100,56.73,'2026-09-07 15:01:02','legacy_review',0,'review')")
        self.con.execute("INSERT INTO paper_positions VALUES ('000989','九芝堂',100,100,9.59,'paper','2026-09-07 09:48:47')")
        self.con.commit()

    def apply(self, **kwargs):
        opts = dict(no_external_transfer=True, no_manual_adjustment=True,
                    confirmation='用户确认无外部转入和手工调整', now=NOW)
        opts.update(kwargs)
        return ledger.apply(self.con, '000831', 'test-correction', ledger.preview(self.con, '000831')['digest'], **opts)

    def test_preview_exact_liquidation_and_excess_sales(self):
        result = ledger.preview(self.con, '000831')
        self.assertEqual(result['closed_at'][:10], '2026-08-31')
        self.assertEqual(result['invalid_order_ids'], ['5','6','7','8','9'])
        self.assertEqual(result['invalid_quantity'], 500)
        self.assertEqual(result['cash_reversal'], 28869)
        self.assertEqual(paper.get_position(self.con, '000831')['quantity'], 100)

    def test_correct_cash_and_inventory_without_deleting_history(self):
        before_cash = paper.cash_from_filled_orders(self.con)
        other = paper.get_position(self.con, '000989')
        result = self.apply()
        self.assertAlmostEqual(before_cash - paper.cash_from_filled_orders(self.con), 28869)
        self.assertAlmostEqual(paper.cash_from_filled_orders(self.con), 1_000_753)
        self.assertEqual(paper.get_position(self.con, '000831')['quantity'], 0)
        self.assertEqual(paper.get_position(self.con, '000989'), other)
        self.assertEqual(self.con.execute('SELECT COUNT(*) FROM paper_orders').fetchone()[0], 10)
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM paper_orders WHERE status='VOID_RECONCILED'").fetchone()[0], 5)
        self.assertTrue(paper.ledger_quality(self.con)['ready'])
        before = json.loads(self.con.execute('SELECT before_json FROM paper_reconciliations').fetchone()[0])
        self.assertEqual(before['orders'][5]['status'], 'FILLED')
        self.assertEqual(before['orders'][5]['reason'], 'original evidence')
        self.assertFalse(result['already_applied'])

    def test_idempotent_after_restart(self):
        plan = ledger.preview(self.con, '000831')
        self.apply()
        other = paper.init_db(self.root)
        try:
            before_cash = paper.cash_from_filled_orders(other)
            result = ledger.apply(other, '000831', 'test-correction', plan['digest'],
                                  no_external_transfer=True, no_manual_adjustment=True,
                                  confirmation='confirmed', now=NOW)
            self.assertTrue(result['already_applied'])
            self.assertEqual(paper.cash_from_filled_orders(other), before_cash)
        finally:
            other.close()

    def test_stale_external_seed_cannot_recreate_position(self):
        self.apply()
        paper.seed_positions(self.con, {'000831': {'quantity':100,'sellable':100,'cost':56.73}}, now=NOW)
        self.assertEqual(paper.get_position(self.con, '000831')['quantity'], 0)

    def test_no_authorization_no_correction(self):
        for options in ({'no_external_transfer':False}, {'no_manual_adjustment':False}, {'confirmation':''}):
            with self.assertRaises(ValueError):
                self.apply(**options)
        self.assertEqual(paper.get_position(self.con, '000831')['quantity'], 100)

    def test_changed_ledger_rejected(self):
        digest = ledger.preview(self.con, '000831')['digest']
        self.con.execute("UPDATE paper_orders SET fill_price=56 WHERE order_id='9'")
        self.con.commit()
        with self.assertRaisesRegex(ValueError, 'changed since preview'):
            ledger.apply(self.con, '000831', 'test', digest, no_external_transfer=True,
                         no_manual_adjustment=True, confirmation='confirmed', now=NOW)
        self.assertEqual(paper.get_position(self.con, '000831')['quantity'], 100)

    def test_partial_oversell_is_not_silently_clipped(self):
        self.con.execute("UPDATE paper_orders SET qty=150 WHERE order_id='4'")
        self.con.commit()
        with self.assertRaisesRegex(ValueError, 'partially oversold'):
            ledger.preview(self.con, '000831')

    def test_unknown_initial_inventory_rejected(self):
        self.con.execute("UPDATE paper_orders SET status='REJECTED' WHERE order_id='0'")
        self.con.commit()
        with self.assertRaises(ValueError):
            ledger.preview(self.con, '000831')

    def test_verified_capital_is_not_removed(self):
        self.con.execute("UPDATE paper_seed_registry SET status='verified',capital_value=5673")
        self.con.commit()
        with self.assertRaises(ValueError):
            ledger.preview(self.con, '000831')

    def test_partial_fill_requires_fill_level_reconciliation(self):
        self.con.execute("UPDATE paper_orders SET status='PARTIAL_FILLED' WHERE order_id='9'")
        self.con.commit()
        with self.assertRaisesRegex(ValueError, 'Partial fill'):
            ledger.preview(self.con, '000831')

    def test_atomic_rollback_preserves_inventory_and_orders(self):
        self.con.execute("CREATE TRIGGER reject_inventory_change BEFORE UPDATE ON paper_positions BEGIN SELECT RAISE(ABORT,'blocked'); END")
        self.con.commit()
        with self.assertRaises(Exception):
            self.apply()
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM paper_orders WHERE status='FILLED'").fetchone()[0], 10)
        self.assertEqual(self.con.execute('SELECT COUNT(*) FROM paper_reconciliations').fetchone()[0], 0)

    def test_old_account_and_position_snapshots_not_used(self):
        self.con.execute("INSERT INTO paper_account_snapshots(snapshot_at,trading_date,total_assets) VALUES ('2026-09-04 16:30:00','2026-09-04',1100000)")
        self.con.execute("INSERT INTO paper_position_snapshots(snapshot_at,trading_date,symbol,market_value) VALUES ('2026-09-04 16:30:00','2026-09-04','000831',6000)")
        self.con.commit()
        self.apply()
        self.assertIsNone(paper.previous_account_snapshot(self.con, '2026-09-07'))
        account = paper.account_exposure_snapshot(self.con, trading_date='2026-09-07',
                    price_map={'000989':{'last_price':8,'prev_close':9}}, now=NOW)
        self.assertIsNone(account['day_pnl'])
        self.assertIsNotNone(account['total_pnl'])
        self.assertEqual(account['day_pnl_basis'], 'awaiting_clean_previous_close')
        self.assertEqual(self.con.execute('SELECT COUNT(*) FROM paper_account_snapshots').fetchone()[0], 1)

    def test_clean_snapshot_restores_next_day_baseline(self):
        self.apply()
        self.con.execute("INSERT INTO paper_account_snapshots(snapshot_at,trading_date,total_assets) VALUES ('2026-09-07 18:41:00','2026-09-07',1001553)")
        self.con.commit()
        prev = paper.previous_account_snapshot(self.con, '2026-09-08')
        self.assertEqual(prev['total_assets'], 1001553)
        account = paper.account_exposure_snapshot(self.con, trading_date='2026-09-08',
                    price_map={'000989':{'last_price':8.1}}, now=datetime(2026,9,8,10))
        self.assertAlmostEqual(account['day_pnl'], 10)

    def test_voided_sales_not_recounted_as_realized_profit(self):
        self.apply()
        rows = paper.load_orders(self.root)
        pnl = paper.realized_pnl_by_order(rows)
        self.assertTrue(all(order_id not in pnl for order_id in ('5','6','7','8','9')))
        self.assertAlmostEqual(sum(r['realized_pnl'] for r in pnl.values()), 753)

    def test_voided_sale_not_marked_as_new_execution_intent(self):
        self.apply()
        count = paper.record_execution_intent_marks(self.con,'2026-09-07',
                    price_map={'000831':{'last_price':55.86}},now=NOW)
        self.assertEqual(count, 0)

    def test_voided_order_id_cannot_be_overwritten(self):
        self.apply()
        with self.assertRaisesRegex(ValueError, 'cannot be overwritten'):
            paper.write_order(self.con,self.root,{'order_id':'9','status':'FILLED'})
        self.assertEqual(self.con.execute("SELECT status FROM paper_orders WHERE order_id='9'").fetchone()[0], 'VOID_RECONCILED')

    def test_replayed_voided_signal_cannot_sell_a_new_position(self):
        oid = '2026-09-07:000831:V2_REDUCE:SELL'
        self.con.execute("UPDATE paper_orders SET order_id=? WHERE order_id='9'", (oid,))
        self.con.commit()
        self.apply()
        self.con.execute("UPDATE paper_positions SET quantity=100,sellable=100,source='paper' WHERE symbol='000831'")
        self.con.commit()
        signal = {'trading_date':'2026-09-07','symbol':'000831','scenario':'V2_REDUCE'}
        with patch.object(paper, 'update_position_after_fill') as fill:
            result = paper.maybe_execute_signal(self.root,signal,{},NOW)
        self.assertEqual(result['status'],'NO_ORDER')
        self.assertIn('对账作废',result['reason'])
        fill.assert_not_called()
        self.assertEqual(paper.get_position(self.con,'000831')['quantity'],100)

    def test_new_position_can_be_managed_after_reconciliation(self):
        self.apply()
        paper.update_position_after_fill(self.con,{'symbol':'000831','name':'中国稀土','trading_date':'2026-09-07','scenario':'test'},'BUY',100,60,NOW)
        paper.seed_positions(self.con,{'000831':{'quantity':100,'sellable':100,'cost':56.73}},now=NOW)
        position = paper.get_position(self.con,'000831')
        self.assertEqual(position['quantity'],100)
        self.assertEqual(position['sellable'],0)
        self.assertEqual(position['avg_cost'],60)
        self.assertTrue(paper.ledger_quality(self.con)['ready'])


if __name__ == '__main__':
    unittest.main()
