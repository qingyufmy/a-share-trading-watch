import copy
from datetime import datetime
import unittest
from unittest.mock import patch

from core import global_risk, intraday_timing_v2 as timing, signal_contract
import test_afterclose_iteration as fixtures
import test_signal_contract_execution as contract_fixtures


class SectorDivergenceTests(unittest.TestCase):
    def setUp(self):
        self.ctx = {'entry_risk_source': 'external_only', 'policy': {'allow_core_attack_buy': True},
                    'a_share_entry_evidence': {'data_ready': True, 'systemic_confirmed': False},
                    'a_share_market_regime': {'state': 'transition'}}
        self.row = {'risk_bucket': 'technology',
                    'strategy_contract': {'key': 'LEADER_EMOTION', 'daily_qualified': True,
                                          'daily_metrics': {'resonance_identity': {'status': 'ready', 'ids': ['BK0459'], 'names': ['元件']}}},
                    'sector_momentum': {'board_id': 'BK0459', 'board_name': '元件', 'board_pct': 1.23,
                                        'emotion_ok': True, 'theme_evidence': {'valid': True}},
                    'sector_rotation': {'board_name': '元件', 'sustained': True, 'leader_healthy': True}}

    def test_planned_strong_sector_can_diverge_from_growth_style(self):
        for fn in (global_risk.core_attack_gate_status, global_risk.market_opportunity_gate_status):
            self.assertTrue(fn(self.ctx, self.row, datetime(2026, 9, 14, 9, 55))[0])

    def test_pulse_mismatch_missing_daily_and_invalid_theme_cannot_override(self):
        original = copy.deepcopy(self.row)
        mutations = [lambda r: r['strategy_contract'].update(daily_qualified=False),
                     lambda r: r['strategy_contract'].update(key='OBSERVE_UNCLASSIFIED'),
                     lambda r: r['sector_momentum'].update(board_id='unplanned'),
                     lambda r: r['sector_momentum'].update(board_name='unplanned'),
                     lambda r: r['sector_momentum'].update(board_pct=.79),
                     lambda r: r['sector_momentum'].update(board_pct=float('nan')),
                     lambda r: r['sector_momentum'].update(theme_evidence={'valid': False}),
                     lambda r: r['sector_rotation'].update(sustained=False),
                     lambda r: r['sector_rotation'].update(leader_healthy=False)]
        for i, mutate in enumerate(mutations):
            with self.subTest(case=i):
                r = copy.deepcopy(original)
                mutate(r)
                self.assertFalse(global_risk.core_attack_gate_status(self.ctx, r)[0])

    def test_systemic_or_stale_cannot_be_overridden_by_strong_sector(self):
        for source in ('a_share_systemic', 'a_share_data_unready'):
            ctx = copy.deepcopy(self.ctx)
            ctx.update(entry_risk_source=source, policy={'allow_core_attack_buy': False})
            self.assertFalse(global_risk.core_attack_gate_status(ctx, self.row)[0])
            self.assertFalse(global_risk.market_opportunity_gate_status(ctx, self.row)[0])

    def test_no_new_volume_threshold_in_sector_permission(self):
        self.row['rt_features'] = {'amount_ratio_1m': .4, 'amount_ratio_5m': .4}
        self.assertTrue(global_risk.confirmed_strategy_sector(self.ctx, self.row))


class MA5AnchorTests(unittest.TestCase):
    def evaluate(self, key='TREND_MA5', volume=1.2, price=10.04, gate=None):
        c = fixtures.contract(key)
        if key == 'TREND_520':
            c['daily_metrics']['ma20_pullback_reclaim'] = True
        row = {'quote': {'close': price, 'open': 10, 'prev_close': 10, 'high': 10.07, 'low': 9.99},
               'rt_features': {'vwap': 10.06, 'amount_ratio_1m': volume, 'amount_ratio_5m': volume},
               'strategy_contract': c}
        history = [{'open': 8+i*.008, 'high': 8.1+i*.008, 'low': 7.9+i*.008,
                    'close': 8+i*.008, 'volume': 1000} for i in range(240)]
        with patch.object(timing, 'closed_bars', side_effect=[fixtures.bars(), []]):
            return timing.evaluate(row, [], now=fixtures.NOW, history_120m=history,
                                   market_data={'market_gate': gate} if gate else None)

    def test_ma5_below_intraday_vwap_can_use_its_own_closed_anchor(self):
        result = self.evaluate()
        self.assertTrue(result['entry_allowed'], result['blockers'])
        self.assertEqual(result['execution_5m'], 'DAILY_MA5_RECLAIM')

    def test_520_still_requires_its_existing_vwap_confirmation(self):
        result = self.evaluate(key='TREND_520')
        self.assertFalse(result['entry_allowed'])
        self.assertIn('5分钟已收盘VWAP收复未完成', result['blockers'])

    def test_volume_and_systemic_gates_remain(self):
        for kwargs in ({'volume': .4}, {'gate': {'allowed': False, 'stage': 'CORE_GLOBAL_RISK_BLOCKED', 'reason': '国内系统性风险'}}):
            result = self.evaluate(**kwargs)
            self.assertFalse(result['entry_allowed'], result)

    def test_price_below_ma5_is_not_rescued(self):
        self.assertFalse(self.evaluate(price=9.99)['entry_allowed'])


class RiskRewardBandTests(unittest.TestCase):
    def test_gross_limit_is_inverted_and_rounded_down(self):
        cap = timing.risk_reward_price_ceiling(11, 9, 1.5)
        self.assertAlmostEqual(cap, 9.8)
        self.assertGreaterEqual((11-cap)/(cap-9), 1.5-1e-12)
        self.assertLess((11-cap-.01)/(cap+.01-9), 1.5)
        for target, stop in ((None, 9), (8, 9), (11, -1), (float('nan'), 9)):
            self.assertIsNone(timing.risk_reward_price_ceiling(target, stop, 1.5))

    def test_contract_upper_price_preserves_net_rr(self):
        c = signal_contract.from_signal(contract_fixtures.entry_signal(nearest_resistance=10.60, atr5m=1.0))
        self.assertLess(c.exec_high, 10.3)
        self.assertLessEqual(c.exec_high, c.rr_price_ceiling)
        cost = c.exec_high*.0003 + c.target_price*.0013
        self.assertGreaterEqual((c.target_price-c.exec_high-cost)/(c.exec_high-c.invalid_price+cost), 1.5)
        self.assertGreaterEqual(c.net_reward_risk, 1.5)

    def test_rr_cannot_be_manufactured_by_widening_band_downward(self):
        c = signal_contract.from_signal(contract_fixtures.entry_signal(nearest_resistance=10.3))
        self.assertLess(c.exec_high, c.current_price)
        self.assertFalse(c.sim_allowed)
        self.assertIn('current_price_above_rr_execution_band', signal_contract.validate_contract(c))

    def test_target_and_stop_not_rewritten_to_force_rr(self):
        c = signal_contract.from_signal(contract_fixtures.entry_signal(nearest_resistance=10.55))
        self.assertEqual(c.target_price, 10.55)
        self.assertEqual(c.invalid_price, 9.70)


if __name__ == '__main__':
    unittest.main()
