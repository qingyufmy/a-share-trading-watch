import json
import os
import sqlite3
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

import paper_trading as paper
import realtime_signal_engine as engine
from core import paper_costs, kaipanla_snapshot as kpl


class CostModelTests(unittest.TestCase):
    def test_small_clip_minimum_commission_is_in_entry_rr(self):
        contract = {'target_price': 10.9, 'invalid_price': 9.7, 'expected_slippage': .02}
        small = paper_costs.entry_check('600000', 100, 10.05, '2026-09-11', contract)
        larger = paper_costs.entry_check('600000', 1000, 10.05, '2026-09-11', contract)
        self.assertGreater(larger['net_reward_risk'], small['net_reward_risk'])
        self.assertFalse(paper_costs.entry_check('600000', 100, 10.05, '2026-09-11', {})['passed'])
    def test_minimum_sell_tax_and_cent_rounding(self):
        with patch.dict(os.environ, {}, clear=True):
            buy = paper_costs.estimate('600000', 'BUY', 100, 10, '2026-09-11')
            sell = paper_costs.estimate('600000', 'SELL', 100, 11, '2026-09-14')
        self.assertEqual(buy['total'], 5.01)
        self.assertEqual(sell['total'], 5.56)
        self.assertEqual(buy['stamp'], 0)

    def test_old_date_preserves_cost_free_history(self):
        self.assertEqual(paper_costs.estimate('600000', 'SELL', 100, 9, '2026-09-09')['total'], 0)

    def test_invalid_settings_do_not_silently_remove_fees(self):
        for bad in ('NaN', '-0.1', 'Infinity', '0.5'):
            with patch.dict(os.environ, {'A_SHARE_PAPER_COMMISSION_RATE': bad}):
                with self.assertRaises(ValueError):
                    paper_costs.estimate('600000', 'BUY', 100, 10, '2026-09-11')

    def test_unknown_instrument_not_charged_equity_tax(self):
        with self.assertRaises(ValueError):
            paper_costs.estimate('513350', 'SELL', 100, 10, '2026-09-11')

    def test_configurable_commission_is_frozen_in_result(self):
        with patch.dict(os.environ, {'A_SHARE_PAPER_COMMISSION_RATE': '0.0002'}):
            result = paper_costs.estimate('600000', 'BUY', 10000, 10, '2026-09-11')
        self.assertEqual(result['commission'], 20)
        self.assertEqual(result['commission_rate'], .0002)


class AtomicFillTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.con = paper.init_db(self.root)
        self.addCleanup(self.con.close)
        self.now = datetime(2026, 9, 11, 10)
        self.env = patch.dict(os.environ, {}, clear=True)
        self.env.start()
        self.addCleanup(self.env.stop)

    def payload(self, side='BUY', order_id='b1', day='2026-09-11', qty=100, price=10):
        signal = {'symbol': '600000', 'name': 'test', 'scenario': 'STRATEGY_MA5_ENTRY', 'trading_date': day}
        return {'order_id': order_id, 'created_at': day + ' 10:00:00', **signal,
                'side': side, 'qty': qty, 'fill_price': price, 'status': 'FILLED', 'signal': signal}

    def fill(self, **kwargs):
        payload = self.payload(**kwargs)
        return paper.record_fill(self.con, self.root, payload, datetime.fromisoformat(payload['created_at']))

    def test_cash_cost_inventory_and_fifo_agree_after_t1_roundtrip(self):
        result = self.fill()
        self.assertEqual(result['fees_total'], 5.01)
        self.assertAlmostEqual(paper.cash_from_filled_orders(self.con), 998994.99)
        self.assertAlmostEqual(paper.get_position(self.con, '600000')['avg_cost'], 10.0501)
        self.assertEqual(paper.get_position(self.con, '600000')['sellable'], 0)
        self.assertAlmostEqual(paper.day_buy_stats(self.con, '600000', '2026-09-11')['avg_price'], 10.0501)
        self.assertEqual(self.fill(side='SELL', order_id='same_day')['status'], 'REJECTED')
        paper.settle_t1_buys(self.con, '2026-09-14')
        self.fill(side='SELL', order_id='s1', day='2026-09-14', price=11)
        self.assertAlmostEqual(paper.cash_from_filled_orders(self.con), 1000089.43)
        self.assertEqual(paper.get_position(self.con, '600000')['quantity'], 0)
        orders = paper.load_orders(self.root)
        self.assertAlmostEqual(orders[-1]['realized_pnl'], 89.43)
        self.assertEqual(orders[-1]['fees_total'], 5.56)
        self.assertAlmostEqual(paper.account_exposure_snapshot(self.con)['recorded_fees_total'], 10.57)

    def test_formal_signal_flows_through_real_broker_costs_and_dedup(self):
        from test_signal_contract_execution import entry_signal
        from core import signal_contract
        signal = entry_signal(trading_date='2026-09-11', updated_at='2026-09-11 10:00:00',
                              planned_v2_probe_position_pct=.01, planned_max_position_pct=.03,
                              premarket_plan_complete=True, premarket_plan_allows_entry=True, position_multiplier=1)
        signal.update(signal_contract.attach_contract(signal))
        now = datetime(2026, 9, 11, 10, 0, 30)
        result = paper.maybe_execute_signal(self.root, signal, {}, now)
        self.assertEqual(result['status'], 'FILLED', result)
        self.assertEqual(result['qty'], 1000)
        self.assertEqual(result['fees_total'], 5.1)
        self.assertTrue(result['signal']['sim_result']['cost_check']['passed'])
        again = paper.maybe_execute_signal(self.root, signal, {}, now)
        self.assertEqual(again['status'], 'FILLED_ALREADY')
        self.assertEqual(paper.get_position(self.con, '600000')['quantity'], 1000)

    def test_order_write_failure_rolls_back_inventory_lots_and_cash(self):
        with patch.object(paper, 'write_order', side_effect=sqlite3.OperationalError('disk full')):
            with self.assertRaises(sqlite3.OperationalError):
                self.fill()
        for table in ('paper_positions', 'paper_buy_lots', 'paper_orders', 'paper_fill_outbox'):
            self.assertEqual(self.con.execute('SELECT COUNT(*) FROM ' + table).fetchone()[0], 0)
        self.assertEqual(paper.cash_from_filled_orders(self.con), 1000000)

    def test_fill_cannot_update_another_signals_inventory(self):
        payload = self.payload()
        payload['signal'] = {**payload['signal'], 'symbol': '600001'}
        with self.assertRaises(ValueError):
            paper.record_fill(self.con, self.root, payload, self.now)
        self.assertEqual(self.con.execute('SELECT COUNT(*) FROM paper_positions').fetchone()[0], 0)

    def test_partial_fill_is_costed_once_and_duplicate_does_not_change_ledger(self):
        payload = self.payload()
        payload['status'] = 'PARTIAL_FILLED'
        paper.record_fill(self.con, self.root, payload, self.now)
        before = paper.cash_from_filled_orders(self.con)
        result = paper.record_fill(self.con, self.root, payload, self.now)
        self.assertEqual(result['status'], 'FILLED_ALREADY')
        self.assertEqual(result['fees_total'], 5.01)
        self.assertEqual(paper.cash_from_filled_orders(self.con), before)
        self.assertEqual(paper.get_position(self.con, '600000')['quantity'], 100)
        self.assertEqual(len(paper.runtime_paths(self.root)['events'].read_text().splitlines()), 1)

    def test_event_failure_preserves_fill_and_retry_only_mirrors_event(self):
        with patch.object(Path, 'open', side_effect=OSError('disk full')):
            result = self.fill()
        self.assertIn('event_warning', result)
        self.assertEqual(self.con.execute('SELECT published FROM paper_fill_outbox').fetchone()[0], 0)
        cash = paper.cash_from_filled_orders(self.con)
        self.assertIsNone(paper.flush_fill_events(self.con, self.root))
        self.assertEqual(self.con.execute('SELECT published FROM paper_fill_outbox').fetchone()[0], 1)
        self.assertIsNone(paper.flush_fill_events(self.con, self.root))
        self.assertEqual(len(paper.runtime_paths(self.root)['events'].read_text().splitlines()), 1)
        self.assertEqual(paper.cash_from_filled_orders(self.con), cash)

    def test_cash_rechecked_with_fees_before_committing(self):
        with patch.dict(os.environ, {'A_SHARE_PAPER_INITIAL_CASH': '1000'}):
            self.assertEqual(self.fill()['status'], 'REJECTED')
        self.assertEqual(self.con.execute('SELECT COUNT(*) FROM paper_orders').fetchone()[0], 0)

    def test_legacy_fill_not_recharged_by_new_model(self):
        self.fill(day='2026-09-10')
        self.assertEqual(paper.load_orders(self.root)[0]['fees_total'], 0)
        self.assertEqual(paper.cash_from_filled_orders(self.con), 999000)

    def test_fee_survives_dashboard_serialization_and_fill_card(self):
        result = self.fill()
        compact = engine.compact_paper_trade(result)
        self.assertEqual(compact['fees_total'], 5.01)
        self.assertIn('5.01元', engine.paper_fill_brief({'paper_trade': compact}))

    def test_fractional_fifo_sale_allocates_buy_and_sell_costs(self):
        self.fill(qty=200)
        paper.settle_t1_buys(self.con, '2026-09-14')
        self.fill(side='SELL', order_id='s1', day='2026-09-14', price=11)
        self.assertAlmostEqual(paper.load_orders(self.root)[-1]['realized_pnl'], 91.93)


class CapturedPopularityTests(unittest.TestCase):
    def test_actual_intraday_date_and_undated_review_kept_distinct(self):
        root = Path(__file__).resolve().parents[1] / 'output/kaipanla_completion_20260910'
        if not root.exists():
            self.skipTest('UI evidence only present in source workspace')
        now = datetime(2026, 9, 10, 18)
        intra = kpl.parse_capture((root / 'intraday.txt').read_text(), 'intraday', now)
        review = kpl.parse_capture((root / 'review.txt').read_text(), 'review', now)
        self.assertEqual(intra['count'], 31)
        self.assertEqual(review['count'], 31)
        self.assertTrue(intra['complete'] and review['complete'])
        self.assertEqual(intra['source_date'], '2026-09-10')
        self.assertFalse(review['date_verified'])
        with tempfile.TemporaryDirectory() as directory:
            kpl.archive(directory, intra)
            kpl.archive(directory, review)
            loaded = kpl.load(directory, '2026-09-10', now=now)
            self.assertEqual(loaded['list_type'], 'intraday')
            self.assertEqual(loaded['count'], 31)
