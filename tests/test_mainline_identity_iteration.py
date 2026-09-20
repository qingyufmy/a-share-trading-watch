import json
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import intraday_report as intra
import realtime_signal_engine as engine
from core import sector_identity as identity, observation_strategy_router as router, emotion_leader_pool as pool


class MainlineIdentityTests(unittest.TestCase):
    def boards(self):
        return [{'f12': 'BK1', 'f14': '农化制品', 'f3': 3.0},
                {'f12': 'BK2', 'f14': 'CPO概念', 'f3': 4.0},
                {'f12': 'BK3', 'f14': '百货', 'f3': 2.0},
                {'f12': 'BK4', 'f14': '旅游酒店', 'f3': 1.0}]

    def test_hierarchical_industry_resolves_provider_id(self):
        result = identity.resolve({'quote': {'industry': '基础化工-化肥农药-复合肥'},
                                   'resonance_boards': ['基础化工-化肥农药-复合肥']}, self.boards())
        self.assertEqual(result['names'], ['农化制品'])
        self.assertEqual(result['ids'], ['BK1'])

    def test_valid_identity_reaches_sector_gate(self):
        stock = {'quote': {'industry': '基础化工-化肥农药-复合肥'}}
        metrics = {}
        identity.apply(metrics, identity.resolve(stock, self.boards()))
        contract = {'key': router.LEADER, 'is_observation_strategy': True, 'daily_metrics': metrics}
        momentum = intra.sector_momentum_for_candidate(stock['quote'], self.boards(), contract=contract)
        row = {'sector_momentum': momentum, 'sector_rotation': {'sustained': True, 'leader_healthy': True}}
        self.assertTrue(router.sector_resonance_gate(contract, row)[0])
        row['sector_rotation']['sustained'] = False
        self.assertFalse(router.sector_resonance_gate(contract, row)[0])

    def test_retail_cannot_borrow_cpo_from_old_lock(self):
        result = identity.resolve({'quote': {'industry': '商贸零售-零售-百货'},
                                   'resonance_boards': ['CPO概念']}, self.boards())
        self.assertEqual(result['status'], 'invalid_identity')
        self.assertFalse(result['ids'])

    def test_new_retail_plan_uses_company_industry(self):
        result = identity.resolve({'quote': {'industry': '商贸零售-零售-百货'},
                                   'news_theme_mentions': ['CPO']}, self.boards())
        self.assertEqual(result['ids'], ['BK3'])

    def test_verified_metadata_overrides_legacy_news_keywords(self):
        result = identity.resolve({'quote': {'industry': '百货', 'concepts': ['CPO'],
                                             'verified_concepts': []}}, self.boards())
        self.assertEqual(result['ids'], ['BK3'])

    def test_board_returns_cannot_change_selected_identity(self):
        stock = {'quote': {'industry': '百货', 'concepts': ['CPO', '农化制品']}}
        before = identity.resolve(stock, self.boards())
        after = identity.resolve(stock, [{**x, 'f3': -99 if x['f12'] == 'BK3' else 99} for x in self.boards()])
        self.assertEqual(before, after)
        self.assertEqual(before['ids'], ['BK3'])

    def test_missing_ambiguous_or_duplicate_catalog_fails_closed(self):
        stock = {'quote': {'concepts': ['CPO', '农化制品']}}
        self.assertEqual(identity.resolve(stock, self.boards())['status'], 'pending_identity')
        self.assertEqual(identity.resolve({'quote': {'industry': '百货'}}, [])['status'], 'pending_catalog')
        duplicate = self.boards() + [{'f12': 'BK5', 'f14': '百货'}]
        self.assertEqual(identity.resolve({'quote': {'industry': '百货'}}, duplicate)['status'], 'pending_catalog')

    def test_name_without_matching_code_cannot_pass_live_gate(self):
        metrics = {}
        identity.apply(metrics, identity.resolve({'quote': {'industry': '百货'}}, self.boards()))
        contract = {'key': router.LEADER, 'is_observation_strategy': True, 'daily_metrics': metrics}
        result = intra.sector_momentum_for_candidate({'industry': '百货'},
                 [{'f12': 'BK99', 'f14': '百货', 'f3': 9}], contract=contract)
        self.assertFalse(result['emotion_ok'])

    def test_hierarchical_delimiter_roundtrip(self):
        metrics = intra.plan_identity_metrics({'主线锁定': '休闲、生活及专业服务-休闲服务-旅游服务'})
        self.assertEqual(len(metrics['resonance_boards']), 1)
        result = identity.resolve({'quote': {'industry': '休闲、生活及专业服务-休闲服务-旅游服务'},
                                   'resonance_boards': metrics['resonance_boards']}, self.boards())
        self.assertEqual(result['ids'], ['BK4'])

    def test_cache_complete_only_no_market_values_or_partial_overwrite(self):
        now = datetime(2026, 9, 8, 16)
        boards = [{**b, '_coverage': {'complete': True, 'expected': 4}} for b in self.boards()]
        with TemporaryDirectory() as root:
            self.assertEqual(identity.update_catalog(root, boards, now)['status'], 'updated')
            path = Path(root) / 'data/reference/eastmoney_boards.json'
            original = path.read_bytes()
            loaded = identity.load_catalog(root, now)
            self.assertEqual(len(loaded), 4)
            self.assertTrue(all(set(x) == {'f12', 'f14'} for x in loaded))
            self.assertEqual(identity.update_catalog(root, boards[:1], now)['status'], 'rejected_incomplete')
            self.assertEqual(path.read_bytes(), original)
            self.assertEqual(identity.update_catalog(root, boards, now)['status'], 'unchanged')
            self.assertFalse(identity.load_catalog(root, now + timedelta(days=8)))
            self.assertFalse(identity.load_catalog(root, now - timedelta(days=1)))

    def test_identity_cache_cannot_be_used_as_live_resonance(self):
        metrics = {}
        identity.apply(metrics, identity.resolve({'quote': {'industry': '百货'}}, self.boards()))
        result = intra.sector_momentum_for_candidate({'industry': '百货'},
            [{k: b[k] for k in ('f12', 'f14')} for b in self.boards()], contract={'daily_metrics': metrics})
        self.assertFalse(result['emotion_ok'])

    def test_crosscheck_missing_stale_partial_and_not_listed_are_distinct(self):
        snapshot = {'source_date': '2026-09-07'}
        self.assertEqual(pool.crosscheck_status(snapshot, {}, '600001'), 'unavailable')
        self.assertEqual(pool.crosscheck_status(snapshot, {'source_date': '2026-09-03'}, '600001'), 'stale')
        partial = {'source_date': '2026-09-07', 'rows': [{'code': '600002', 'rank': 1}]}
        self.assertEqual(pool.crosscheck_status(snapshot, partial, '600001'), 'not_observed_partial')
        self.assertEqual(pool.crosscheck_status(snapshot, {**partial, 'complete': True}, '600001'), 'not_listed')
        self.assertEqual(pool.crosscheck_status(snapshot, partial, '600002'), 'verified')

    def test_quality_recovery_requires_three_samples_and_two_minutes(self):
        now = datetime(2026, 9, 8, 9, 31)
        state = {}
        for i in range(3):
            action, state = engine.engine_health_alert_transition(state, now=now+timedelta(seconds=5*i),
                error='MARKET_BREADTH_INCOMPLETE', incident_kind='data_quality')
        self.assertEqual(action, 'error')
        for seconds in [20, 30, 139]:
            action, state = engine.engine_health_alert_transition(state, now=now+timedelta(seconds=seconds))
            self.assertIsNone(action)
        action, state = engine.engine_health_alert_transition(state, now=now+timedelta(seconds=140))
        self.assertEqual(action, 'recovery')
        action, state = engine.engine_health_alert_transition(state, now=now+timedelta(seconds=150))
        self.assertIsNone(action)

    def test_partial_board_response_never_creates_ready_contract(self):
        result = identity.resolve({'quote': {'industry': '百货'}},
            [{**b, '_coverage': {'complete': False}} for b in self.boards()])
        self.assertEqual(result['status'], 'pending_catalog')

    def test_plan_handoff_requires_valid_unique_ids_and_preserves_cross_status(self):
        row = {'主线锁定': '百货', '主线代码': 'BK3', '主线状态': 'ready',
               '主线依据': 'company_industry', '双榜状态': 'stale'}
        metrics = intra.plan_identity_metrics(row)
        self.assertTrue(identity.ready(metrics))
        self.assertEqual(metrics['emotion_pool_cross_status'], 'stale')
        for value in ['', 'invalid', 'BK3、BK3']:
            self.assertFalse(identity.ready(intra.plan_identity_metrics({**row, '主线代码': value})))
        self.assertFalse(identity.ready({'resonance_identity': {'status': 'ready'}}))

    def test_catalog_shrink_rejected_and_corruption_detected(self):
        now = datetime(2026, 9, 8, 16)
        boards = [{**b, '_coverage': {'complete': True, 'expected': 4}} for b in self.boards()]
        with TemporaryDirectory() as root:
            identity.update_catalog(root, boards, now)
            path = Path(root) / 'data/reference/eastmoney_boards.json'
            original = path.read_bytes()
            small = [{**boards[0], '_coverage': {'complete': True, 'expected': 1}}]
            self.assertEqual(identity.update_catalog(root, small, now)['status'], 'rejected_universe_shrink')
            self.assertEqual(path.read_bytes(), original)
            payload = json.loads(original)
            payload['rows'][0]['f14'] = 'corrupted'
            path.write_text(json.dumps(payload))
            self.assertFalse(identity.load_catalog(root, now))

    def test_json_and_markdown_plan_handoff_preserve_identity_evidence(self):
        row = {'代码': '600001', '名称': 'test', '防守': '10', '修复': '11', '压力': '12',
               '主线锁定': '百货', '主线代码': 'BK3', '主线状态': 'ready',
               '概念': 'short', '完整概念': 'a、b、c、d、e、百货', '双榜状态': 'stale'}
        with TemporaryDirectory() as root:
            root = Path(root)
            data_dir = root / 'web_dashboard/data/reports'
            data_dir.mkdir(parents=True)
            (data_dir / 'premarket_20260908.json').write_text(json.dumps({'rows': [row]}))
            markdown = root / 'plan.md'
            markdown.write_text('| ' + ' | '.join(row) + ' |\n| ' + ' | '.join(['---'] * len(row))
                                + ' |\n| ' + ' | '.join(row.values()) + ' |\n')
            with patch.object(intra, 'BASE_DIR', root), patch.object(intra, 'REPORT_DATE', '2026-09-08'), \
                    patch.object(intra, 'current_watchlist_map', return_value={}):
                json_level = intra.read_premarket_levels_from_json()['600001']
            with patch.object(intra, 'read_premarket_levels_from_json', return_value={}), \
                    patch.object(intra, 'read_afterclose_levels_from_json', return_value={}), \
                    patch.object(intra, 'PREMARKET_REPORT', markdown), \
                    patch.object(intra, 'normalize_levels_to_watchlist', side_effect=lambda x: x):
                markdown_level = intra.read_premarket_levels()['600001']
            for level in (json_level, markdown_level):
                self.assertEqual(level['concepts'], row['完整概念'])
                metrics = router.contract_from_level(level)['daily_metrics']
                self.assertTrue(identity.ready(metrics))
                self.assertEqual(metrics['emotion_pool_cross_status'], 'stale')

    def test_empty_profile_fields_cannot_erase_known_company_identity(self):
        now = datetime(2026, 9, 8, 16)
        with TemporaryDirectory() as root:
            directory = Path(root) / 'data/runtime'
            directory.mkdir(parents=True)
            (directory / 'market_profiles.json').write_text(json.dumps({
                'updated_at': now.isoformat(), 'profiles': {
                    '600002': {'industry': '-', 'concepts': '-'},
                    '600001': {'industry': '船舶制造', 'concepts': '--'}}}))
            profiles = identity.load_profiles(root, now)
            self.assertNotIn('600002', profiles)
            self.assertEqual(profiles['600001'], {'industry': '船舶制造'})


if __name__ == '__main__':
    unittest.main()
