import copy
from datetime import datetime, timedelta
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from core import closed_liquidity, sector_identity, strategy_gap_research as gaps
from core import observation_strategy_router as router, intraday_timing_v2 as timing
from research import review_watchlist_top_gainers as audit


NOW = datetime(2026, 9, 15, 9, 33, 20)


def bar(at, o, h, low, close, volume=100):
    return {'bar_end': at.isoformat(sep=' '), 'timestamp_convention': 'end',
            'open': o, 'high': h, 'low': low, 'close': close, 'volume': volume}


def payload():
    return {'now': NOW.isoformat(), 'bars1': [
        bar(NOW.replace(minute=31, second=0), 10, 10.1, 9.99, 10.05),
        bar(NOW.replace(minute=32, second=0), 10.05, 10.12, 10, 10.08),
        bar(NOW.replace(minute=33, second=0), 10.08, 10.21, 10.02, 10.2, 150)],
        'bars5': [], 'quote': {'code': '600001', 'close': 10.2, 'datetime': '20260915093315'},
        'contract': {'key': router.LEADER, 'daily_qualified': True, 'daily_metrics': {'daily_atr14': 1}},
        'vwap': 10, 'regime': 'TREND', 'sector_ok': True, 'leadership': {'confirmed': True},
        'market': {'entry_ready': True, 'market_gate': {'allowed': True}},
        'target_evidence': {'target': 11, 'unconfirmed_reclaims': []}}


class ClosedLiquidityTests(unittest.TestCase):
    def test_immutable_closing_archive_survives_next_morning_and_verifies_hash(self):
        import json
        q = {'code': '600001', 'datetime': '20260914150001', 'close': 10, 'turnover': 10}
        with TemporaryDirectory() as folder:
            result = closed_liquidity.archive_completed_quotes(folder, {'600001': q}, datetime(2026, 9, 14, 15, 1))
            self.assertEqual(result['saved'], 1)
            altered = {**q, 'turnover': 50}
            self.assertEqual(closed_liquidity.archive_completed_quotes(folder, {'600001': altered}, datetime(2026, 9, 14, 15, 2))['existing'], 1)
            self.assertEqual(closed_liquidity.load_completed_quote(folder, '600001', '2026-09-14')['turnover'], 10)
            self.assertEqual(closed_liquidity.archive_completed_quotes(folder, {'600001': q}, NOW)['saved'], 0)
            path = Path(folder)/'data/reference/closing_liquidity/2026-09-14/600001.json'
            data = json.loads(path.read_text()); data['quote']['turnover'] = 20
            path.write_text(json.dumps(data))
            self.assertEqual(closed_liquidity.load_completed_quote(folder, '600001', '2026-09-14'), {})

    def test_wrong_symbol_cache_is_rejected(self):
        daily = [{'date': '2026-09-14', 'close': 10}]
        result = closed_liquidity.enrich(daily, {'code': '600001'},
            {'code': '600002', 'datetime': '20260914150001', 'close': 10, 'turnover': 10})
        self.assertNotIn('turnover', result[-1])

    def test_prior_close_fills_missing_not_morning_zero(self):
        daily = [{'date': '2026-09-14', 'close': 10, 'volume_lot': 100000, 'turnover': None}]
        old = copy.deepcopy(daily)
        morning = {'datetime': '20260915090000', 'close': 10, 'turnover': 0}
        cached = {'datetime': '20260914150001', 'close': 10, 'turnover': 10}
        got = closed_liquidity.enrich(daily, morning, cached)
        self.assertEqual(got[-1]['turnover'], 10)
        self.assertEqual(got[-1]['float_market_cap_yi'], 10)
        self.assertEqual(daily, old)

    def test_reject_stale_wrong_day_price_intraday_and_nan(self):
        daily = [{'date': '2026-09-14', 'close': 10}]
        q = {'datetime': '20260914150001', 'close': 10, 'turnover': 10}
        for update in ({'_stale': True}, {'datetime': '20260915150000'}, {'close': 11},
                       {'datetime': '20260914140000'}, {'turnover': float('nan')}):
            self.assertNotIn('turnover', closed_liquidity.enrich(daily, {**q, **update}, {})[-1])

    def test_completed_fields_not_overwritten_and_unknown_units_not_estimated(self):
        q = {'datetime': '20260914150001', 'close': 10, 'turnover': 20}
        result = closed_liquidity.enrich([{'date': '2026-09-14', 'close': 10, 'turnover': 5, 'volume': 1000}], q, {})
        self.assertEqual(result[-1]['turnover'], 5)
        self.assertNotIn('float_market_cap_yi', result[-1])


class ShadowResearchTests(unittest.TestCase):
    def test_fixed_cohort_includes_losers_and_missing_quotes(self):
        levels = {f'60000{i}': {} for i in range(6)}
        quotes = {c: {'pct': 2 if i < 4 else -2, 'datetime': '20260915093315'}
                  for i, c in enumerate(levels)}
        with patch.object(router, 'contract_from_level', return_value={'key': router.LEADER, 'daily_qualified': True}):
            cohort = gaps.leader_cohort(levels, quotes, NOW)
            self.assertTrue(cohort['qualified'])
            self.assertEqual(len(cohort['members']), 6)
            self.assertAlmostEqual(cohort['positive_fraction'], 4/6)
            del quotes['600005']
            self.assertFalse(gaps.leader_cohort(levels, quotes, NOW)['qualified'])

    def test_opening_capture_deduplicates_and_reads_without_orders(self):
        p = payload()
        row = {'quote': p['quote'], 'strategy_contract': p['contract']}
        research = gaps.capture_evaluate(row, p['bars1'], [], NOW, 'TREND', {})
        signals = [{'symbol': '600001', 'timing_v2': {'gap_research': research}}]
        with TemporaryDirectory() as folder:
            cache = {}
            self.assertTrue(gaps.append_opening_snapshot(signals, folder, cache, NOW))
            self.assertFalse(gaps.append_opening_snapshot(signals, folder, cache, NOW))
            self.assertFalse(gaps.append_opening_snapshot(signals, folder, cache, NOW.replace(hour=10)))
            result = audit.read_opening_research(Path(folder)/'gap_opening_20260915.jsonl', '2026-09-15', {'600001'})
            self.assertEqual(result['counts'], {'verified': 1, 'exact_match': 1})
            self.assertFalse(result['rows'][0]['result']['entry_allowed'])

    def test_repair_coverage_expands_without_promoting_contract(self):
        from test_repair_iteration import ShadowTests
        fixture = ShadowTests(); fixture.setUp()
        fixture.c['daily_metrics']['repair_watch'] = False
        result = fixture.evaluate()
        self.assertTrue(result['shadow_ready'], result)
        self.assertFalse(result['entry_allowed'])
        self.assertFalse(fixture.c['daily_qualified'])

    def test_repair_start_stamps_equal_end_stamps(self):
        from test_repair_iteration import ShadowTests
        fixture = ShadowTests(); fixture.setUp()
        before = fixture.evaluate()
        for b in fixture.bars:
            b['bar_end'] = (datetime.fromisoformat(b['bar_end'])-timedelta(minutes=1)).isoformat(sep=' ')
            b['timestamp_convention'] = 'start'
        after = fixture.evaluate()
        self.assertEqual(before['shadow_ready'], after['shadow_ready'], after)
        self.assertEqual(before['indicators'], after['indicators'])

    def test_early_positive_is_never_order(self):
        result = gaps.evaluate(payload())
        route = result['routes']['LEADER_EARLY_3M']
        self.assertTrue(route['shadow_ready'], route)
        self.assertFalse(result['entry_allowed'])
        self.assertEqual(route['order_action'], 'NO_ORDER')

    def test_early_constraints_cannot_be_skipped(self):
        for field in ('daily_qualified', 'sector', 'future', 'stale', 'volume', 'rr', 'chase', 'market', 'data', 'pressure'):
            p = payload()
            if field == 'daily_qualified': p['contract']['daily_qualified'] = False
            if field == 'sector': p['sector_ok'] = False
            if field == 'future': p['bars1'][-1]['bar_end'] = '2026-09-15 09:34:00'
            if field == 'stale': p['quote']['datetime'] = '20260915093000'
            if field == 'volume': p['bars1'][-1]['volume'] = 1
            if field == 'rr': p['target_evidence']['target'] = 10.21
            if field == 'chase': p['vwap'] = 9
            if field == 'market': p['market']['market_gate']['allowed'] = False
            if field == 'data': p['market']['entry_ready'] = False
            if field == 'pressure': p['target_evidence']['unconfirmed_reclaims'] = [{'price': 10.1}]
            self.assertFalse(gaps.evaluate(p)['routes']['LEADER_EARLY_3M']['shadow_ready'], field)

    def test_trend_continuation_positive_and_broken_retest(self):
        p = payload()
        now = NOW.replace(hour=10, minute=0)
        p.update(now=now.isoformat(), bars1=[], sector_ok=True)
        p['quote'].update(datetime='20260915100015', close=10.3)
        p['contract']['key'] = router.TREND_MA5
        points = [(10, 10.1, 9.9, 10), (10, 10.1, 9.9, 10), (10, 10.1, 9.9, 10),
                  (10, 10.4, 9.95, 10.3), (10.3, 10.25, 10.08, 10.15), (10.15, 10.35, 10.1, 10.3)]
        # Retest opens below its high, with an independent following confirmation.
        points[4] = (10.23, 10.25, 10.08, 10.15)
        p['bars5'] = [bar(now.replace(hour=9, minute=30, second=0)+timedelta(minutes=5*(i+1)), *v)
                      for i, v in enumerate(points)]
        route = gaps.evaluate(p)['routes']['TREND_CONTINUATION_RETEST']
        self.assertTrue(route['shadow_ready'], route)
        p['bars5'][-2]['low'] = 9
        self.assertFalse(gaps.evaluate(p)['routes']['TREND_CONTINUATION_RETEST']['shadow_ready'])

    def test_lunch_boundary_and_timestamp_conventions(self):
        now = NOW.replace(hour=13, minute=1)
        ends = gaps.expected_ends(now, 5)
        self.assertEqual(ends[-1].strftime('%H:%M'), '11:30')
        bars = [bar(t, 10, 11, 9, 10) for t in ends[-6:]]
        self.assertTrue(gaps.valid_tail(bars, 5, 6, now))
        for b in bars:
            b['bar_end'] = (datetime.fromisoformat(b['bar_end'])-timedelta(minutes=1)).isoformat()
            b['timestamp_convention'] = 'start'
        self.assertTrue(gaps.valid_tail(bars, 5, 6, now))
        bars[-1]['bar_end'] = '2026-09-15 13:00:00'
        self.assertFalse(gaps.valid_tail(bars, 5, 6, now))

    def test_replay_is_exact_and_tampering_rejected(self):
        p = payload()
        row = {'code': '600001', 'quote': p['quote'], 'strategy_contract': p['contract'], 'rt_features': {'vwap': 10}}
        result = gaps.capture_evaluate(row, p['bars1'], [], NOW, 'TREND', p['market'])
        self.assertEqual(gaps.replay(result['replay']), result['replay']['expected'])
        result['replay']['input']['quote']['close'] = 20
        with self.assertRaises(ValueError): gaps.replay(result['replay'])

    def test_bad_research_input_isolated(self):
        result = gaps.capture_evaluate({'quote': {'close': float('nan')}, 'strategy_contract': {}}, [], [], NOW, 'TREND', {})
        self.assertIn('capture_error', result)
        self.assertFalse(result['entry_allowed'])

    def test_research_failure_does_not_change_existing_timing_entry(self):
        import test_strategy_optimization as fixtures
        before = fixtures.TimingConsistencyTests().evaluate()
        with patch.object(gaps, 'capture_evaluate', side_effect=RuntimeError('injected')):
            after = fixtures.TimingConsistencyTests().evaluate()
        for key in ('entry_allowed', 'action', 'candidate_entry_pattern', 'position_action', 'blockers'):
            self.assertEqual(before[key], after[key])

    def test_independent_cohort_is_shadow_and_needs_same_cycle(self):
        p = payload()
        p['sector_ok'] = False
        p['cohort'] = {'asof': p['now'], 'qualified': True, 'leading_codes': ['600001']}
        r = gaps.evaluate(p)['routes']['INDEPENDENT_LEADER']
        self.assertTrue(r['shadow_ready'], r)
        self.assertFalse(r['entry_allowed'])
        p['cohort']['asof'] = '2026-09-14T09:33:20'
        self.assertFalse(gaps.evaluate(p)['routes']['INDEPENDENT_LEADER']['shadow_ready'])


class ClassificationAndAuditTests(unittest.TestCase):
    def test_e5_limit_ceiling_is_inapplicable_not_an_endless_wait(self):
        from test_intraday_timing_v2 import row
        subject = row(price=10.68, high=10.8, low=10)
        subject['quote'].update(prev_close=10, pct=6.8, amount_wan=100000, limit_up=10.8)
        subject['sector_momentum'] = {'emotion_ok': True, 'board_name': '测试板块', 'board_pct': 2}
        subject['sector_rotation'] = {'sustained': True, 'leader_healthy': True}
        bars = [{'high': 10.8, 'low': 10, 'close': 10.7, 'volume': 9000},
                {'high': 10.8, 'low': 10.45, 'close': 10.5, 'volume': 4000},
                {'high': 10.72, 'low': 10.5, 'close': 10.68, 'volume': 5000}]
        result = timing._leader_second_leg_state(subject, bars, [], 10.4, .3, 10.68,
                                                'TREND', 'NORMAL', True, True, timing.load_config())
        self.assertFalse(result['eligible'])
        self.assertEqual(result['status'], 'NOT_APPLICABLE_LIMIT_CEILING')

    def test_readiness_distinguishes_missing_unqualified_and_observe(self):
        self.assertEqual(router.contract_readiness({})['status'], 'CONTRACT_MISSING')
        self.assertEqual(router.contract_readiness({'key': router.OBSERVE})['status'], 'OBSERVATION_ONLY')
        self.assertEqual(router.contract_readiness({'key': router.TREND_520, 'daily_qualified': False})['status'], 'DAILY_NOT_QUALIFIED')

    def test_default_ranks_ten_not_five(self):
        quotes = {f'600{i:03}': {'code': f'600{i:03}', 'close': 100+i, 'prev_close': 100,
                               'pct': i, 'datetime': '20260915150000'} for i in range(12)}
        ranked, missing, total = audit.rank_closing_quotes(quotes, quotes, '2026-09-15')
        self.assertEqual(len(ranked), 10)
        self.assertEqual(total, 12)
        self.assertEqual(missing, [])

    def test_new_mainline_is_not_execution_authorized(self):
        stock = {'quote': {'industry': '塑料制品', 'concepts': ['复合集流体']}}
        boards = [{'f12': 'BK001', 'f14': '塑料制品'}, {'f12': 'BK002', 'f14': '复合集流体', 'f3': 20}]
        identity = sector_identity.resolve(stock, boards)
        research = sector_identity.research_candidates(stock, boards)
        self.assertEqual(identity['names'], ['塑料制品'])
        self.assertEqual(research[0]['name'], '复合集流体')
        self.assertFalse(research[0]['execution_authorized'])
        boards[1]['f3'] = -20
        self.assertEqual(sector_identity.research_candidates(stock, boards), research)


if __name__ == '__main__':
    unittest.main()
