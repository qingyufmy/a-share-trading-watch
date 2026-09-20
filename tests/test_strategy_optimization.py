import copy
import json
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from core import method_entry, observation_strategy_router as router, intraday_timing_v2 as timing
import realtime_signal_engine as engine
from test_afterclose_iteration import contract, bars, NOW


class EntryReachabilityTests(unittest.TestCase):
    def setUp(self):
        self.c = contract('TREND_520')
        self.c['daily_metrics'].update(fresh_cross=True, cross_days_ago=3, ma20_pullback_reclaim=False)

    def test_older_cross_can_wait_for_new_intraday_ma20_touch(self):
        result = method_entry.evaluate(self.c, bars(), 10.04, NOW, {})
        self.assertTrue(result['eligible'], result)
        self.assertEqual(result['anchor_name'], 'MA20')

    def test_older_cross_still_cannot_buy_without_ma20_touch(self):
        b = bars()
        for bar in b:
            for key in ('low', 'high', 'close'):
                bar[key] += .5
        self.assertFalse(method_entry.evaluate(self.c, b, 10.54, NOW, {})['eligible'])

    def test_permission_alone_without_daily_event_is_not_520(self):
        self.c['daily_metrics']['fresh_cross'] = False
        self.assertFalse(method_entry.evaluate(self.c, bars(), 10.04, NOW, {})['eligible'])

    def test_macd_and_kdj_risk_overlays_remain(self):
        for key in ('macd', 'kdj_confirmed'):
            c = copy.deepcopy(self.c)
            c['daily_metrics'][key] = {'bullish': False} if key == 'macd' else False
            self.assertFalse(method_entry.evaluate(c, bars(), 10.04, NOW, {})['eligible'])

    def test_touch_does_not_mutate_frozen_authorization(self):
        original = copy.deepcopy(self.c)
        method_entry.evaluate(self.c, bars(), 10.04, NOW, {})
        self.assertEqual(original, self.c)

    def test_touch_memory_default_unchanged(self):
        r = method_entry.evaluate(self.c, bars(), 10.04, NOW, {})
        self.assertEqual(r['touch_lookback_bars'], 3)
        self.assertEqual(r['touch_anchor_period'], '1d_developing_at_touch')

    def test_each_touch_uses_its_own_developing_daily_anchor(self):
        c = contract('TREND_MA5')
        b = bars()
        b[0].update(low=9.94, high=10.02, close=9.95)
        b[1].update(low=10.17, high=10.22, close=10.20)
        b[2].update(low=10.20, high=10.32, close=10.30)
        r = method_entry.evaluate(c, b, 10.30, NOW, {})
        self.assertEqual(r['touch_bar_end'], b[0]['bar_end'])
        self.assertFalse(r['eligible'])  # Correct historical touch is not permission to chase.

    def test_research_memory_cannot_use_future_bar(self):
        b = bars(); b[-1]['bar_end'] = '2026-09-07 10:05:00'
        self.assertFalse(method_entry.evaluate(self.c, b, 10.04, NOW, {'touch_lookback_bars': 6})['eligible'])


class IndependentRoutingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.daily = json.loads((Path(__file__).resolve().parents[1] /
            'output/watchlist_replay_20260907_173811/daily_inputs_t1.json').read_text())

    def stock(self, code):
        return {'quote': {'code': code, 'industry': '文化传媒'}, 'daily': self.daily[code]}

    def test_huace_uses_ready_ma5_not_old_cross_route(self):
        c = router.classify_stock(self.stock('300133'))
        self.assertEqual(c['key'], 'TREND_MA5')
        self.assertTrue(c['daily_qualified'])
        self.assertEqual(c['daily_metrics']['qualification_count'], 3)
        self.assertTrue(c['daily_metrics']['method_candidates']['TREND_520']['daily_qualified'])
        self.assertNotIn('V2_E3_MA20_STRUCTURAL_RECLAIM', c['allowed_patterns'])

    def test_live_quote_cannot_change_daily_method(self):
        stock = self.stock('300133')
        original = router.classify_stock(stock)
        stock['quote'].update(close=900, low=1, high=999, pct=50, amount_wan=9999999)
        self.assertEqual(router.classify_stock(stock)['key'], original['key'])

    def test_520_enhancement_failure_does_not_hide_independently_ready_ma5(self):
        with patch.object(router, '_kdj_metrics', return_value={
                'k': 45, 'd': 55, 'j': 25, 'bullish': False, 'not_overheated': True}):
            c = router.classify_stock(self.stock('300133'))
        self.assertEqual(c['key'], 'TREND_MA5')
        self.assertFalse(c['daily_metrics']['method_candidates']['TREND_520']['daily_qualified'])

    def test_real_ma20_reclaim_keeps_520_priority(self):
        c = router.classify_stock(self.stock('600685'))
        self.assertEqual(c['key'], 'TREND_520')
        self.assertTrue(c['daily_metrics']['method_candidates']['TREND_MA5']['daily_qualified'])

    def test_repair_watch_not_promoted(self):
        for code in ('301183', '301511'):
            c = router.classify_stock(self.stock(code))
            self.assertFalse(c['daily_qualified'])
            self.assertEqual(c['allowed_patterns'], [])

    def test_candidate_qualification_does_not_confuse_entry_readiness(self):
        for code in self.daily:
            c = router.classify_stock(self.stock(code))
            if c['key'] == 'TREND_MA5':
                m = c['daily_metrics']
                candidate = m['method_candidates']['TREND_MA5']
                self.assertEqual(candidate['daily_qualified'], c['daily_qualified'])
                self.assertEqual(candidate['entry_evidence_ready'], m['qualification_count'] >= 3)

    def test_plan_handoff_keeps_selected_method_and_evidence(self):
        c = router.classify_stock(self.stock('300133'))
        restored = router.contract_from_level({'strategy_key': c['key'],
            'strategy_daily_metrics': c['daily_metrics'], 'strategy_daily_evidence': c['reason']})
        self.assertEqual(restored['key'], c['key'])
        self.assertEqual(restored['daily_metrics']['method_candidates'], c['daily_metrics']['method_candidates'])
        self.assertFalse(router.execution_gate(restored, 'V2_E3_MA20_STRUCTURAL_RECLAIM')[0])


class OuterDataGateTests(unittest.TestCase):
    def decision(self, key, structure=True, setup=False, quality=True):
        row = {'code': '000001', 'strategy_contract': contract(key)}
        with patch.object(engine.local_market_data, 'history_120m', return_value=([], {'entry_ready': structure, 'blockers': []})), \
             patch.object(engine.local_market_data, 'history_15m', return_value=([{'close': 10}], {'entry_ready': setup, 'blockers': ['15m missing']})), \
             patch.object(engine.global_risk, 'core_attack_gate_status', return_value=(True, 'OK', '')), \
             patch.object(engine, 'preopen_quality_entry_gate', return_value={'allowed': quality, 'reason': 'test'}), \
             patch.object(engine.intraday_timing_v2, 'evaluate', side_effect=lambda *a, **kw: kw):
            return engine.timing_v2_for_row(row, [], current=NOW, position={'sellable': 100})

    def test_daily_methods_do_not_require_15m_history_for_entry(self):
        for key in ('TREND_520', 'TREND_MA5'):
            r = self.decision(key)
            self.assertTrue(r['market_data']['entry_ready'])
            self.assertNotIn('15m missing', r['market_data']['blockers'])
            self.assertEqual(r['history_15m'], [{'close': 10}])
            self.assertEqual(r['position']['sellable'], 100)

    def test_leader_still_requires_15m_history(self):
        self.assertFalse(self.decision('LEADER_EMOTION')['market_data']['entry_ready'])

    def test_daily_still_requires_120m(self):
        self.assertFalse(self.decision('TREND_MA5', structure=False)['market_data']['entry_ready'])

    def test_quality_gate_not_bypassed(self):
        self.assertFalse(self.decision('TREND_MA5', quality=False)['market_data']['entry_ready'])


class TimingConsistencyTests(unittest.TestCase):
    def evaluate(self, b=None, cfg=None, market=None, now=NOW):
        row = {'strategy_contract': contract(),
               'quote': {'close': 10.04, 'open': 10, 'prev_close': 10, 'high': 10.07, 'low': 9.99},
               'rt_features': {'vwap': 10.01, 'amount_ratio_1m': 1.2, 'amount_ratio_5m': 1.2}}
        h = [{'high': 8.1+i*.008, 'low': 7.9+i*.008, 'close': 8+i*.008, 'volume': 1000} for i in range(240)]
        with patch.object(timing, 'closed_bars', side_effect=[b or bars(), []]):
            return timing.evaluate(row, [], now=now, history_120m=h, config=cfg, market_data=market)

    def test_native_pullback_uses_one_low_confirmation_not_two(self):
        b = bars(); b[0]['low'] = 10.01; b[1]['low'] = 9.99
        r = self.evaluate(b)
        self.assertTrue(r['entry_allowed'], r['blockers'])
        self.assertEqual(r['execution_gates']['higher_low_required_bars'], 2)

    def test_falling_latest_low_remains_blocked(self):
        b = bars(); b[-1]['low'] = 9.98
        self.assertFalse(self.evaluate(b)['entry_allowed'])

    def test_daily_method_uses_explicit_daily_distance_and_structural_rr(self):
        r = self.evaluate()
        self.assertEqual(r['execution_gates']['vwap_distance_period'], '1d')
        self.assertEqual(r['execution_gates']['max_vwap_distance_atr'], .25)
        self.assertEqual(r['room_risk']['min_room_atr'], 0)
        self.assertEqual(r['execution_gates']['risk_atr_period'], '1d')

    def test_invalid_atr_period_fails_closed(self):
        cfg = timing.load_config(); cfg['method_execution']['TREND_MA5']['vwap_distance_period'] = 'bad'
        self.assertFalse(self.evaluate(cfg=cfg)['entry_allowed'])

    def test_daily_atr_variant_has_explicit_denominator(self):
        cfg = timing.load_config(); cfg['method_execution']['TREND_MA5']['vwap_distance_period'] = '1d'
        r = self.evaluate(cfg=cfg)
        self.assertEqual(r['execution_gates']['vwap_distance_atr_value'], .2)
        self.assertEqual(r['execution_gates']['vwap_distance_period'], '1d')

    def test_market_red_not_bypassed(self):
        r = self.evaluate(market={'market_gate': {'allowed': False, 'stage': 'CORE_GLOBAL_RISK_BLOCKED'}})
        self.assertFalse(r['entry_allowed'])

    def test_late_entry_remains_blocked(self):
        r = self.evaluate(now=NOW.replace(hour=14, minute=50))
        self.assertFalse(r['entry_allowed'])
        self.assertIn('14:45后不新开仓', r['blockers'])

    def test_expired_opening_path_does_not_hide_active_second_leg(self):
        c = contract('LEADER_EMOTION')
        c['allowed_patterns'] = ['V2_E5B_LEADER_OPENING_HOLD', 'V2_E5_LEADER_SECOND_LEG']
        row = {'quote': {'close': 10.4, 'open': 10, 'prev_close': 10}, 'strategy_contract': c}
        r = timing.evaluate(row, [], now=datetime(2026, 9, 7, 11, 0))
        self.assertEqual(r['leader_route_windows']['V2_E5B_LEADER_OPENING_HOLD'], 'expired')
        self.assertNotIn(r['leader_opening_hold']['reason'], r['blockers'])
        self.assertIn(r['leader_second_leg']['reason'], r['blockers'])


if __name__ == '__main__':
    unittest.main()
