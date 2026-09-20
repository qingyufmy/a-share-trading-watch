import unittest
from datetime import datetime
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

import after_close_report as base
import intraday_report as intra
import realtime_signal_engine as engine
from core import observation_strategy_router as router
from core import intraday_timing_v2 as timing


class IntradayChainTests(unittest.TestCase):
    def test_board_provider_page_cap_does_not_hide_liquid_cooling(self):
        def response(url, **kwargs):
            query = parse_qs(urlparse(url).query)
            self.assertEqual(query['fid'], ['f12'])
            page = int(query['pn'][0])
            rows = [{'f12': f'BK{i:04}', 'f14': f'board{i}', 'f3': 0.2}
                    for i in range((page - 1) * 100, min(page * 100, 203))]
            if page == 2:
                rows[5].update(f14='液冷服务器', f3=1.59)
            return {'data': {'total': 203, 'diff': rows}}

        with patch.object(base, 'fetch_json', side_effect=response):
            boards, _ = base.fetch_boards()
        self.assertEqual(len(boards), 203)
        self.assertTrue(boards[0]['_coverage']['complete'])
        self.assertEqual(boards[0]['f14'], '液冷服务器')

    def test_partial_board_fetch_exposes_failed_page_without_fabricating_it(self):
        def response(url, **kwargs):
            if parse_qs(urlparse(url).query)['pn'] == ['2']:
                raise TimeoutError('page2')
            return {'data': {'total': 200, 'diff': [{'f12': 'BK1', 'f14': '出版', 'f3': 2}]}}

        with patch.object(base, 'fetch_json', side_effect=response):
            boards, _ = base.fetch_boards()
        self.assertEqual(len(boards), 1)
        self.assertFalse(boards[0]['_coverage']['complete'])
        self.assertEqual(boards[0]['_coverage']['failed_pages'], [2])

    def test_weak_industry_does_not_override_explicit_strong_concept(self):
        boards = [{'f14': '汽车零部件', 'f3': 0.1}, {'f14': '液冷服务器', 'f3': 1.59},
                  {'f14': '软件开发', 'f3': 5.0}]
        result = intra.sector_momentum_for_candidate(
            {'industry': '汽车零部件', 'concepts': ['液冷服务器']}, boards)
        self.assertEqual(result['board_name'], '液冷服务器')
        self.assertEqual(len(result['matched_boards']), 2)

    def test_leader_contributes_to_all_verified_boards(self):
        boards = [{'f14': '汽车零部件', 'f3': 1.2}, {'f14': '液冷服务器', 'f3': 1.8}]
        candidate = {'quote': {'code': '002536', 'industry': '汽车零部件', 'concepts': '液冷服务器',
                               'close': 11, 'open': 10, 'pct': 5, 'amount_wan': 100000}}
        cache = {}
        engine.update_board_rotation_state(boards, [candidate], cache, datetime(2026, 9, 7, 10, 0))
        states = engine.update_board_rotation_state(boards, [candidate], cache, datetime(2026, 9, 7, 10, 2))
        for state in states.values():
            self.assertTrue(state['leader_healthy'])
            self.assertTrue(state['sustained'])

    def test_daily_numeric_evidence_survives_contract_handoff(self):
        metrics = {'ma5': 32.83, 'ma20': 30.12, 'daily_asof': '2026-09-04',
                   'kdj': {'k': 50}, 'amount_ratio_5d': 1.3}
        contract = router.contract_from_level({'strategy_key': router.TREND_MA5,
                                              'strategy_daily_metrics': metrics})
        for key, value in metrics.items():
            self.assertEqual(contract['daily_metrics'][key], value)
        self.assertFalse(contract['daily_qualified'])

    def test_unready_leader_reports_authorized_route_not_trend_setup(self):
        subject = {'quote': {'close': 10, 'open': 10, 'prev_close': 10},
                   'strategy_contract': {'key': 'LEADER_EMOTION',
                                         'is_observation_strategy': True, 'daily_qualified': True,
                                         'allowed_patterns': ['V2_E5B_LEADER_OPENING_HOLD']}}
        with patch.object(timing, '_setup_state') as generic:
            decision = timing.evaluate(subject, [], now=datetime(2026, 9, 7, 10, 20))
        generic.assert_not_called()
        self.assertFalse(decision['entry_allowed'])
        self.assertIn(decision['leader_opening_hold']['reason'], decision['blockers'])

    def test_pipeline_does_not_count_sells_or_observation_as_entries(self):
        signal = {'symbol': '1', 'strategy_family': 'THREE_METHOD',
                  'premarket_plan_allows_entry': True, 'premarket_plan_complete': True,
                  'strategy_daily_qualified': True, 'sector_resonance_ok': True,
                  'strategy_gate_ok': True, 'timing_v2': {'candidate_entry_pattern': 'V2_E3_MA20_STRUCTURAL_RECLAIM',
                                                       'entry_allowed': False}, 'scenario': 'V2_WAIT'}
        summary = engine.entry_pipeline_summary([signal, {**signal, 'symbol': '2', 'scenario': 'V2_REDUCE'}])
        self.assertEqual(summary['passed']['setup'], 2)
        self.assertEqual(summary['passed']['formal'], 0)
        self.assertEqual(summary['first_blocked_symbols']['execution'], ['1', '2'])


if __name__ == '__main__':
    unittest.main()
