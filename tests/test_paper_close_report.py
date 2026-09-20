import json
from datetime import datetime
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

import paper_trading
import paper_close_report as close
import run_trading_job as scheduler


class ClosingPerformanceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.con = paper_trading.init_db(self.base)
        self.addCleanup(self.con.close)
        self.now = datetime(2026, 9, 14, 15, 15)

    def order(self, key, code, side, qty, price, at, fee=0, status='FILLED'):
        self.con.execute('INSERT INTO paper_orders(order_id,symbol,name,side,qty,fill_price,created_at,trading_date,fees_total,status) VALUES (?,?,?,?,?,?,?,?,?,?)',
                         (key, code, code, side, qty, price, at, at[:10], fee, status))

    def position(self, code, qty, cost):
        self.con.execute('INSERT INTO paper_positions VALUES (?,?,?,?,?,?,?)', (code, code, qty, 0, cost, 'paper', '2026-09-14 14:00:00'))

    def previous(self, cash, positions=(), at='2026-09-11 15:01:00'):
        mv = sum(qty * price for _, qty, price in positions)
        self.con.execute('INSERT INTO paper_account_snapshots(snapshot_at,trading_date,cash,market_value,total_assets) VALUES (?,?,?,?,?)', (at, at[:10], cash, mv, cash+mv))
        for code, qty, price in positions:
            self.con.execute('INSERT INTO paper_position_snapshots(snapshot_at,trading_date,symbol,name,quantity,last_price,market_value) VALUES (?,?,?,?,?,?,?)',
                             (at, at[:10], code, code, qty, price, qty*price))

    def report(self, prices=None):
        quotes = {c: {'close': p, 'datetime': '20260914150000'} for c, p in (prices or {}).items()}
        return close.calculate(self.con, quotes, self.now, '2026-09-11', 10000)

    def test_first_buy_includes_fee_and_matches_account(self):
        self.previous(10000)
        self.order('b', '600001', 'BUY', 100, 10, '2026-09-14 10:00:00', 5)
        self.position('600001', 100, 10.05)
        r = self.report({'600001': 11})
        self.assertAlmostEqual(r['account']['day_pnl'], 95)
        self.assertAlmostEqual(r['positions'][0]['cumulative_pnl'], 95)
        self.assertAlmostEqual(r['account']['day_return_pct'], .95)
        self.assertAlmostEqual(r['positions'][0]['day_return_pct'], 95/1005*100)

    def test_full_exit_retained_and_daily_not_lifetime_profit(self):
        self.order('b', '600001', 'BUY', 100, 10, '2026-09-10 10:00:00', 5)
        self.previous(8995, [('600001', 100, 11)])
        self.order('s', '600001', 'SELL', 100, 12, '2026-09-14 10:00:00', 6)
        r = self.report()
        self.assertEqual(r['positions'][0]['quantity'], 0)
        self.assertAlmostEqual(r['account']['day_pnl'], 94)
        self.assertAlmostEqual(r['positions'][0]['day_pnl'], 94)
        self.assertAlmostEqual(r['positions'][0]['cumulative_pnl'], 189)
        self.assertAlmostEqual(r['account']['realized_pnl'], 189)

    def test_partial_exit_plus_add_and_fee_not_double_counted(self):
        self.order('b', '600001', 'BUY', 200, 10, '2026-09-10 10:00:00', 5)
        self.previous(7995, [('600001', 200, 11)])
        self.order('s', '600001', 'SELL', 100, 12, '2026-09-14 10:00:00', 5)
        self.order('b2', '600001', 'BUY', 100, 11.5, '2026-09-14 11:00:00', 5)
        self.position('600001', 200, 10.7875)
        r = self.report({'600001': 12})
        self.assertAlmostEqual(r['account']['day_pnl'], 240)
        self.assertAlmostEqual(r['positions'][0]['day_pnl'], 240)

    def test_empty_account_still_reported(self):
        self.previous(10000)
        r = self.report()
        self.assertEqual(r['account']['day_pnl'], 0)
        self.assertEqual(len(close.cards(r)), 1)
        self.assertIn('当前空仓', json.dumps(close.cards(r), ensure_ascii=False))

    def test_missing_baseline_does_not_substitute_open_position_return(self):
        self.order('b', '600001', 'BUY', 100, 10, '2026-09-14 10:00:00')
        self.position('600001', 100, 10)
        r = self.report({'600001': 11})
        self.assertIsNone(r['account']['day_pnl'])
        self.assertIsNone(r['positions'][0]['day_pnl'])
        self.assertEqual(r['account']['cumulative_pnl'], 100)

    def test_stale_intraday_baseline_rejected(self):
        self.previous(10000, at='2026-09-11 10:00:00')
        self.assertIsNone(self.report()['account']['day_pnl'])

    def test_missing_or_nonfinite_close_cannot_zero_value_position(self):
        self.previous(10000)
        self.position('600001', 100, 10)
        for p in (None, float('nan'), float('inf'), 0):
            r = self.report({'600001': p})
            self.assertIsNone(r['account']['total_assets'])
            self.assertIsNone(r['account']['cumulative_pnl'])

    def test_stale_or_future_quotes_rejected(self):
        self.previous(10000)
        self.position('600001', 100, 10)
        for stamp in ('20260911150000', '20260914145959', '20260914160000'):
            r = close.calculate(self.con, {'600001': {'close': 11, 'datetime': stamp}}, self.now, '2026-09-11', 10000)
            self.assertIsNone(r['account']['total_assets'])

    def test_unverified_ledger_hides_returns(self):
        self.previous(10000)
        self.con.execute("INSERT INTO paper_seed_registry(symbol,quantity,avg_cost,imported_at,status,capital_value,note) VALUES ('600001',100,10,'2026-09-01','legacy_review',0,'')")
        r = self.report()
        self.assertIsNone(r['account']['cumulative_pnl'])

    def test_unbalanced_prior_detail_hides_day_return(self):
        self.previous(10000)
        self.con.execute('UPDATE paper_account_snapshots SET market_value=100,total_assets=10100')
        self.assertIsNone(self.report()['account']['day_pnl'])

    def test_void_orders_ignored_and_read_only_compatible(self):
        self.previous(10000)
        self.order('v', '600001', 'SELL', 100, 10, '2026-09-14 10:00:00', status='VOID_RECONCILED')
        self.con.commit()
        self.con.execute('PRAGMA query_only=ON')
        self.assertEqual(self.report()['account']['cumulative_pnl'], 0)

    def test_delivery_once_and_preview_does_not_consume(self):
        self.previous(10000)
        r, state, send = self.report(), self.base/'delivery.sqlite', Mock(return_value={'code': 0})
        close.deliver(r, state, send, skip=True)
        self.assertFalse(state.exists())
        close.deliver(r, state, send)
        close.deliver(r, state, send)
        self.assertEqual(send.call_count, 1)

    def test_explicit_failure_retries_only_failed_page(self):
        self.previous(10000)
        r = self.report()
        r['positions'] = [{'symbol': str(i), 'name': 'test', 'quantity': 0, 'sellable': 0, 'close': None,
                           'day_pnl': 0, 'day_return_pct': None, 'cumulative_pnl': 0, 'cumulative_return_pct': None,
                           'unrealized_pnl': 0, 'buy_qty': 0, 'sell_qty': 0} for i in range(10)]
        state = self.base/'delivery.sqlite'
        send = Mock(side_effect=[{'code': 0}, {'code': 9499}])
        with self.assertRaises(RuntimeError):
            close.deliver(r, state, send)
        retry = Mock(return_value={'code': 0})
        close.deliver(r, state, retry)
        self.assertEqual(retry.call_count, 1)

    def test_uncertain_response_never_blindly_resends(self):
        self.previous(10000)
        state, send = self.base/'delivery.sqlite', Mock(side_effect=TimeoutError())
        with self.assertRaises(RuntimeError):
            close.deliver(self.report(), state, send)
        with self.assertRaises(RuntimeError):
            close.deliver(self.report(), state, send)
        self.assertEqual(send.call_count, 1)

    def test_daily_schedule_dedup_and_existing_nodes_unchanged(self):
        for h, m, expected in ((15, 0, 'intraday'), (15, 14, 'skip'), (15, 15, 'paperclose'), (16, 29, 'paperclose'), (16, 30, 'afterclose')):
            self.assertEqual(scheduler.resolve_mode('auto', self.now.replace(hour=h, minute=m)), expected)
        self.assertEqual(scheduler.automatic_node(self.now, [{'event': 'success', 'mode': 'paperclose'}]), 'skip')


if __name__ == '__main__':
    unittest.main()
