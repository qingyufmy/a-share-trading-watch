import copy
import json
import os
import sqlite3
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from core import method_entry, intraday_timing_v2 as timing, observation_strategy_router as router
from core import signal_tracking
from core import opportunity_rating
import paper_trading as paper
import intraday_report as intra
import realtime_signal_engine as engine


NOW = datetime(2026, 9, 7, 10, 0)


def contract(key='TREND_MA5'):
    return {'key': key, 'is_observation_strategy': True, 'daily_qualified': True,
            'allowed_patterns': list(router.STRATEGY_META[key]['allowed_patterns']),
            'daily_metrics': {'daily_asof': '2026-09-04', 'daily_atr14': .2,
                              'close_sum4': 40, 'close_sum19': 190, 'qualification_count': 3,
                              'macd': {'bullish': True}, 'kdj_confirmed': True,
                              'daily_resistance_levels': [10.8]}}


def bars():
    return [{'open': 10, 'high': 10.07, 'low': low, 'close': close, 'volume': 1000,
             'bar_end': (NOW - timedelta(minutes=11 - i * 5)).isoformat(sep=' ')}
            for i, (low, close) in enumerate([(9.99, 10.01), (10.0, 10.02), (10.01, 10.04)])]


class NativeMethodTests(unittest.TestCase):
    def evaluate(self, c=None, b=None, price=10.04, now=NOW):
        return method_entry.evaluate(c or contract(), b or bars(), price, now, {})

    def test_daily_ma5_positive(self):
        result = self.evaluate()
        self.assertTrue(result['eligible'], result)
        self.assertAlmostEqual(result['anchor_ma5'], 10.008)
        self.assertEqual(result['risk_period'], '1d')

    def test_below_daily_anchor_is_not_buy(self):
        self.assertFalse(self.evaluate(price=9.99)['eligible'])

    def test_insufficient_daily_factors(self):
        c = contract(); c['daily_metrics']['qualification_count'] = 2
        self.assertFalse(self.evaluate(c)['eligible'])

    def test_incomplete_daily_basis_fails_closed(self):
        c = contract(); c['daily_metrics'].pop('close_sum4')
        self.assertFalse(self.evaluate(c)['eligible'])

    def test_same_day_daily_data_is_future_for_plan(self):
        c = contract(); c['daily_metrics']['daily_asof'] = '2026-09-07'
        self.assertFalse(self.evaluate(c)['eligible'])

    def test_future_five_minute_bar_is_rejected(self):
        b = bars(); b[-1]['bar_end'] = '2026-09-07 10:04:00'
        self.assertFalse(self.evaluate(b=b)['eligible'])

    def test_previous_day_intraday_bar_is_rejected(self):
        b = bars(); b[0]['bar_end'] = '2026-09-04 14:59:00'
        self.assertFalse(self.evaluate(b=b)['eligible'])

    def test_duplicate_bar_is_rejected(self):
        b = bars(); b[0]['bar_end'] = b[1]['bar_end']
        self.assertFalse(self.evaluate(b=b)['eligible'])

    def test_end_stamped_bar_is_closed_at_its_timestamp(self):
        b = bars()
        for bar in b:
            bar['timestamp_convention'] = 'end'
            bar['bar_end'] = str(datetime.fromisoformat(bar['bar_end']) + timedelta(minutes=1))
        self.assertTrue(self.evaluate(b=b)['eligible'])

    def test_no_chasing_daily_anchor(self):
        self.assertFalse(self.evaluate(price=10.2)['eligible'])

    def test_520_ma20_reclaim_positive(self):
        c = contract('TREND_520'); c['daily_metrics']['ma20_pullback_reclaim'] = True
        result = self.evaluate(c)
        self.assertTrue(result['eligible'], result)
        self.assertEqual(result['anchor_name'], 'MA20')

    def test_520_requires_macd(self):
        c = contract('TREND_520'); c['daily_metrics']['ma20_pullback_reclaim'] = True
        c['daily_metrics']['macd']['bullish'] = False
        self.assertFalse(self.evaluate(c)['eligible'])

    def test_520_requires_kdj(self):
        c = contract('TREND_520'); c['daily_metrics']['ma20_pullback_reclaim'] = True
        c['daily_metrics']['kdj_confirmed'] = False
        self.assertFalse(self.evaluate(c)['eligible'])

    def test_520_next_day_cross_does_not_require_another_pullback(self):
        c = contract('TREND_520'); c['daily_metrics'].update(fresh_cross=True, cross_days_ago=0)
        b = bars()
        for i, bar in enumerate(b):
            bar.update(low=10.055 + .01*i, high=10.065 + .01*i, close=10.06 + .015*i)
        b[-1]['high'] = 10.10
        result = self.evaluate(c, b, 10.09)
        self.assertTrue(result['eligible'], result)
        self.assertEqual(result['entry_variant'], 'CROSS_NEXT_DAY_CONFIRM')
        c['daily_metrics']['cross_days_ago'] = 4
        self.assertFalse(self.evaluate(c, b, 10.09)['eligible'])

    def test_daily_method_full_timing_has_no_15m_ma20_setup_dependency(self):
        c = contract()
        row = {'quote': {'close': 10.04, 'open': 10, 'prev_close': 10, 'high': 10.07, 'low': 9.99},
               'rt_features': {'vwap': 10.01, 'amount_ratio_1m': 1.2, 'amount_ratio_5m': 1.2},
               'strategy_contract': c}
        history = [{'open': 8 + i*.008, 'high': 8.1+i*.008, 'low': 7.9+i*.008,
                    'close': 8+i*.008, 'volume': 1000} for i in range(240)]
        with patch.object(timing, 'closed_bars', side_effect=[bars(), []]):
            result = timing.evaluate(row, [], now=NOW, history_120m=history, history_15m=[])
        self.assertTrue(result['entry_allowed'], result['blockers'])
        self.assertEqual(result['extension_state'], 'CONTROLLED_DAILY_ANCHOR')
        self.assertEqual(result['risk_timeframe'], '1d')

    def test_native_entry_reaches_signal_contract_and_paper_fill(self):
        for key in ('TREND_MA5', 'TREND_520'):
            with self.subTest(method=key):
                self.assert_entry_chain(key)

    def assert_entry_chain(self, key):
        c = contract(key)
        c.update(name=router.STRATEGY_META[key]['name'], style='趋势票')
        if key == 'TREND_520':
            c['daily_metrics']['ma20_pullback_reclaim'] = True
        c['daily_metrics'].update(resonance_policy='locked_mainline_v1', resonance_boards=['液冷服务器'])
        row = {'code': '000001', 'quote': {'code': '000001', 'name': '隔离测试', 'close': 10.04,
               'open': 10, 'prev_close': 10, 'high': 10.07, 'low': 9.99},
               'rt_features': {'vwap': 10.01, 'amount_ratio_1m': 1.2, 'amount_ratio_5m': 1.2,
                               'available': True, 'last_minute_time': '09:59:00', 'last_volume': 10000,
                               'atr5m': .08, 'atr1m': .02},
               'strategy_contract': c,
               'sector_momentum': {'board_name': '液冷服务器', 'board_pct': 2, 'emotion_ok': True},
               'sector_rotation': {'sustained': True, 'leader_healthy': True},
               'premarket_plan_loaded': True, 'premarket_plan_allows_entry': True,
               'premarket_plan_action': '条件试仓', 'premarket_plan_source': 'isolated_test',
               'planned_target_position_pct': .03, 'planned_max_position_pct': .03,
               'planned_v2_probe_position_pct': .01}
        history = [{'open': 8 + i*.008, 'high': 8.1+i*.008, 'low': 7.9+i*.008,
                    'close': 8+i*.008, 'volume': 1000} for i in range(240)]
        with patch.object(timing, 'closed_bars', side_effect=[bars(), []]):
            row['timing_v2'] = timing.evaluate(row, [{'m': '09:59:00', 'p': 10.04}], now=NOW, history_120m=history)
        with patch.object(engine, 'now_dt', return_value=NOW), patch.object(intra, 'REPORT_DATE', '2026-09-07'):
            signal = engine.v2_signal_for_row(row)
        self.assertEqual(signal['scenario'], router.STRATEGY_ENTRY_SCENARIOS[key], signal)
        self.assertTrue(signal['signal_contract']['sim_allowed'], signal['signal_contract'])
        with TemporaryDirectory() as tmp:
            fill = paper.maybe_execute_signal(tmp, signal, {}, NOW)
            self.assertIn(fill['status'], ('FILLED', 'PARTIAL_FILLED'), fill)
            con = paper.init_db(tmp)
            try:
                self.assertGreater(paper.get_position(con, '000001')['quantity'], 0)
                self.assertEqual(paper.get_position(con, '000001')['sellable'], 0)
            finally:
                con.close()
        signal['paper_trade'] = engine.compact_paper_trade(fill)
        with patch.object(engine, 'now_dt', return_value=NOW), \
             patch.object(engine, 'REALTIME_SIGNAL_FEISHU_ENABLED', True), \
             patch.dict(os.environ, {'A_SHARE_SKIP_FEISHU': '0'}), \
             patch.object(intra, 'FEISHU_WEBHOOK', 'https://example.invalid/test'), \
             patch.object(engine.urllib.request, 'urlopen') as post:
            post.return_value.__enter__.return_value.read.return_value = b'{"code":0}'
            engine.send_feishu([signal], {'engine_status': 'ok'})
            post.assert_called_once()
            payload = json.loads(post.call_args[0][0].data)
            self.assertEqual(payload['card']['header']['template'], 'green')
            self.assertIn('日线', json.dumps(payload, ensure_ascii=False))
            post.reset_mock()
            signal['paper_trade'] = {'status': 'NO_ORDER', 'reason': 'ledger review'}
            engine.send_feishu([signal], {'engine_status': 'degraded'})
            post.assert_not_called()

    def test_rating_uses_the_method_history_requirement(self):
        quality = {'minute_available': True, 'closed_5m_bars': 1, 'closed_15m_bars': 20,
                   'required_closed_5m': 1, 'required_closed_15m': 20}
        self.assertTrue(opportunity_rating._data_health({'data_quality': quality})[0])
        quality['closed_5m_bars'] = 0
        self.assertFalse(opportunity_rating._data_health({'data_quality': quality})[0])


class SeedLedgerTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.con = paper.init_db(self.root); self.addCleanup(self.con.close)
        self.seed = {'000831': {'quantity': 100, 'sellable': 100, 'cost': 56.73}}

    def test_full_sell_cannot_be_reseeded_even_after_restart(self):
        paper.seed_positions(self.con, self.seed, now=NOW)
        paper.update_position_after_fill(self.con, {'symbol': '000831'}, 'SELL', 100, 55.86, NOW)
        restarted = paper.init_db(self.root)
        try:
            paper.seed_positions(restarted, self.seed, now=NOW)
            self.assertEqual(paper.get_position(restarted, '000831')['quantity'], 0)
            self.assertTrue(paper.ledger_quality(restarted)['ready'])
        finally:
            restarted.close()

    def test_partial_sell_not_overwritten(self):
        self.seed['000831']['quantity'] = self.seed['000831']['sellable'] = 200
        paper.seed_positions(self.con, self.seed, now=NOW)
        paper.update_position_after_fill(self.con, {'symbol': '000831'}, 'SELL', 100, 55.86, NOW)
        paper.seed_positions(self.con, self.seed, now=NOW)
        self.assertEqual(paper.get_position(self.con, '000831')['quantity'], 100)

    def test_unknown_legacy_inventory_is_preserved_and_flagged(self):
        self.con.execute("INSERT INTO paper_positions VALUES ('000831','x',100,100,56.73,'positions_file','2026-09-04')")
        self.con.commit()
        other = paper.init_db(self.root)
        try:
            self.assertFalse(paper.ledger_quality(other)['ready'])
            self.assertEqual(paper.get_position(other, '000831')['quantity'], 100)
        finally:
            other.close()

    def test_sold_legacy_position_cannot_be_reimported(self):
        self.con.execute("INSERT INTO paper_orders(order_id,symbol,status,side,qty) VALUES ('old','000831','FILLED','SELL',100)")
        self.con.commit()
        paper.seed_positions(self.con, self.seed, now=NOW)
        self.assertEqual(paper.get_position(self.con, '000831')['quantity'], 0)
        self.assertFalse(paper.ledger_quality(self.con)['ready'])

    def test_imported_capital_is_not_profit(self):
        paper.seed_positions(self.con, self.seed, now=NOW)
        account = paper.annotate_account_quality(self.con, {'initial_cash': 1000000, 'total_pnl': 5673})
        self.assertAlmostEqual(account['total_pnl'], 0)

    def test_new_security_flow_is_not_day_profit(self):
        paper.seed_positions(self.con, self.seed, now=NOW)
        account = paper.annotate_account_quality(self.con, {
            'initial_cash': 1000000, 'total_pnl': 5673, 'day_pnl': 5673,
            'previous_snapshot_at': '2026-09-04 16:30:00', 'updated_at': '2026-09-07 10:01:00',
            'previous_total_assets': 1000000})
        self.assertAlmostEqual(account['day_pnl'], 0)

    def test_unknown_ledger_profit_is_null(self):
        paper.seed_positions(self.con, self.seed, now=NOW)
        self.con.execute("UPDATE paper_seed_registry SET status='legacy_review'"); self.con.commit()
        result = paper.annotate_account_quality(self.con, {'total_pnl': 9999, 'day_pnl': 9999})
        self.assertIsNone(result['day_pnl']); self.assertIsNone(result['total_pnl'])

    def test_unknown_ledger_blocks_buy(self):
        paper.seed_positions(self.con, self.seed, now=NOW)
        self.con.execute("UPDATE paper_seed_registry SET status='legacy_review'"); self.con.commit()
        result = paper.maybe_execute_signal(self.root, {'symbol': '000001', 'trading_date': '2026-09-07',
                                                       'scenario': 'STRATEGY_520_ENTRY'}, {}, NOW)
        self.assertEqual(result['status'], 'NO_ORDER')
        self.assertFalse(result['ledger_quality']['ready'])


class EventAndSectorTests(unittest.TestCase):
    def event_bars(self):
        b = bars()
        b[0].update(high=10.02, close=10)
        b[1].update(high=10.03, close=10.01)
        b[2].update(high=10.10, close=10.08, low=10.02)
        b.append({**b[-1], 'high': 10.09, 'low': 10.04, 'close': 10.07, 'bar_end': '2026-09-07 10:04:00'})
        return b

    def test_recent_breakout_survives_latest_bar_not_breaking(self):
        result = timing.recent_breakout_evidence(self.event_bars(), 10.07, .05)
        self.assertTrue(result['confirmed']); self.assertEqual(result['age_bars'], 1)

    def test_breakout_low_invalidates_event(self):
        b = self.event_bars(); b[-1]['low'] = 9.99
        self.assertFalse(timing.recent_breakout_evidence(b, 10.07, .05)['confirmed'])

    def test_breakout_expiry(self):
        b = self.event_bars(); b[-1]['bar_end'] = '2026-09-07 10:19:00'
        self.assertFalse(timing.recent_breakout_evidence(b, 10.07, .05)['confirmed'])

    def test_breakout_cannot_cross_lunch(self):
        b = self.event_bars(); b[-1]['bar_end'] = '2026-09-07 13:04:00'
        self.assertFalse(timing.recent_breakout_evidence(b, 10.07, .05)['confirmed'])

    def test_close_under_breakout_invalidates(self):
        b = self.event_bars(); b[-1]['close'] = 10.02
        self.assertFalse(timing.recent_breakout_evidence(b, 10.07, .05)['confirmed'])

    def test_late_blocked_candidate_is_not_missed_trade(self):
        state = signal_tracking.state_for({'timing_v2': {'trigger_missed': True}}, {'hard_vetoed': True})
        self.assertNotEqual(state, 'TRIGGERED_BUT_MISSED')

    def test_explicit_mainline_does_not_switch_to_stronger_other_board(self):
        c = contract(); c['daily_metrics'].update(resonance_policy='locked_mainline_v1', resonance_boards=['液冷服务器'])
        result = intra.sector_momentum_for_candidate(
            {'industry': '汽车零部件', 'concepts': '液冷服务器,华为概念'},
            [{'f14': '液冷服务器', 'f3': .2}, {'f14': '华为概念', 'f3': 4}], contract=c)
        self.assertEqual(result['board_name'], '液冷服务器'); self.assertFalse(result['emotion_ok'])

    def test_wrong_mainline_is_blocked_even_when_strong(self):
        c = contract(); c['daily_metrics'].update(resonance_policy='locked_mainline_v1', resonance_boards=['液冷服务器'])
        row = {'sector_momentum': {'board_name': '数字货币', 'board_pct': 4, 'emotion_ok': True},
               'sector_rotation': {'sustained': True, 'leader_healthy': True}}
        self.assertFalse(router.sector_resonance_gate(c, row)[0])
        row['sector_momentum']['board_name'] = '液冷服务器'
        self.assertTrue(router.sector_resonance_gate(c, row)[0])

    def test_broad_board_never_grants_resonance(self):
        row = {'sector_momentum': {'board_name': '深圳特区', 'board_pct': 4, 'emotion_ok': True},
               'sector_rotation': {'sustained': True, 'leader_healthy': True}}
        self.assertFalse(router.sector_resonance_gate(contract(), row)[0])

    def test_ambiguous_theme_requires_plan(self):
        self.assertEqual(router.planned_resonance({'quote': {'concepts': '液冷,数字货币'}})[0], [])


class BreadthIterationTests(unittest.TestCase):
    def test_pagination_uses_stable_identity_sort_and_retries(self):
        calls = []
        def fetch(url, timeout):
            calls.append(url)
            page = int(url.split('pn=')[1].split('&')[0])
            return {'data': {'total': 4, 'diff': [{'f12': str(page*2+i)} for i in range(2)]}}
        with patch.object(engine, 'MARKET_BREADTH_PAGE_SIZE', 2), patch.object(engine, 'fetch_json_fast', side_effect=fetch):
            result = engine.fetch_market_breadth_snapshot_fast(2)
        self.assertEqual(len(result['rows']), 4)
        self.assertTrue(all('fid=f12' in url for url in calls))
        self.assertEqual(result['failed_pages'], [])

    def test_failed_page_is_reported(self):
        def fetch(url, timeout):
            return {'data': {'total': 4, 'diff': [{'f12': '1'}, {'f12': '2'}] if 'pn=1&' in url else []}}
        with patch.object(engine, 'MARKET_BREADTH_PAGE_SIZE', 2), patch.object(engine, 'fetch_json_fast', side_effect=fetch):
            result = engine.fetch_market_breadth_snapshot_fast(4)
        self.assertEqual(result['failed_pages'], [2])

    def test_failed_refresh_does_not_relabel_last_good_as_fresh(self):
        good = {'state': 'balanced', 'updated_at': '2026-09-07 10:00:00', 'up_ratio': .7}
        cache = {'market_breadth_last_good': good, 'market_breadth': good}
        with patch.object(engine, 'fetch_resilient_market_breadth', return_value={'rows': []}):
            result = engine.refresh_market_breadth(cache, NOW)
        self.assertFalse(result['coverage_ready']); self.assertIsNone(result['up_ratio'])
        self.assertEqual(cache['market_breadth_last_good'], good)

    def test_partial_breadth_hides_biased_ratios(self):
        with patch.object(engine, 'fetch_resilient_market_breadth', return_value={
            'rows': [{'f12': '000001', 'f3': 1}], 'reported_total': 5000, 'failed_pages': [2]}), \
             patch.object(engine, 'write_market_profile_cache'):
            result = engine.refresh_market_breadth({}, NOW)
        self.assertFalse(result['coverage_ready']); self.assertIsNone(result['up_ratio'])


if __name__ == '__main__':
    unittest.main()
