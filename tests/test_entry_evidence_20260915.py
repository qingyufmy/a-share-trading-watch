import copy
from datetime import datetime
import unittest
from unittest.mock import patch

from core import entry_quality as quality, intraday_timing_v2 as timing, method_entry
from core import opportunity_rating, signal_contract, sim_broker, strategy_discipline
import test_afterclose_iteration as daily
import test_signal_contract_execution as contracts
import test_intraday_timing_v2 as old


class TargetTests(unittest.TestCase):
    def test_baoding_near_pressure_not_replaced_to_improve_rr(self):
        row = {'pressure': 56.38, 'quote': {'limit_up': 59.4},
               'strategy_contract': {'daily_metrics': {'daily_resistance_levels': [58.5, 62.35]}}}
        result = quality.target_evidence(row, 56.2, 56.42, 59.4)
        self.assertEqual(result['target'], 56.38)
        self.assertEqual(result['source'], 'planned_pressure')
        self.assertLess((result['target']-56.2)/(56.2-54.875626), .14)

    def test_breakout_must_be_closed_and_still_hold(self):
        row = {'pressure': 10.3, 'quote': {'limit_up': 11},
               'strategy_contract': {'daily_metrics': {'daily_resistance_levels': [10.8]}}}
        self.assertTrue(quality.target_evidence(row, 10.35, 10.29)['unconfirmed_reclaims'])
        result = quality.target_evidence(row, 10.35, 10.35)
        self.assertEqual(result['target'], 10.8)
        self.assertFalse(result['unconfirmed_reclaims'])
        self.assertEqual(quality.target_evidence(row, 10.28, 10.35)['target'], 10.3)

    def test_limit_cap_and_missing_finite_target(self):
        self.assertEqual(quality.target_evidence({'pressure': 12, 'quote': {'limit_up': 11}}, 10, 10)['target'], 11)
        self.assertIsNone(quality.target_evidence({'pressure': float('nan')}, 10, 10)['target'])

    def test_opening_helper_rejects_close_unbroken_pressure(self):
        row = old.row(10.35, 10.4, 10.18)
        row.update(pressure=10.4, strategy_contract={'key': 'LEADER_EMOTION', 'daily_qualified': True},
                   sector_momentum={'emotion_ok': True, 'board_pct': 2},
                   sector_rotation={'sustained': True, 'leader_healthy': True})
        row['quote'].update(open=10.2, pct=3.5, amount_wan=20000)
        bar = {'open': 10.2, 'high': 10.4, 'low': 10.18, 'close': 10.35}
        result = timing._leader_opening_hold_state(row, [bar], 10.25, .1, 10.35, 'TREND', 'GAP_HOLD',
                                                   datetime(2026, 9, 15, 9, 35), timing.load_config())
        self.assertFalse(result['eligible'])
        self.assertEqual(result['target_evidence']['target'], 10.4)


class LeadershipTests(unittest.TestCase):
    def row(self, pct=4.07, leader='603186', leader_pct=10):
        return {'code': '002552', 'quote': {'pct': pct}, 'sector_momentum': {'board_pct': 2.62},
                'sector_rotation': {'sustained': True, 'leader_healthy': True,
                                    'leader': {'code': leader, 'pct': leader_pct}}}

    def test_other_stock_limit_up_is_not_own_leadership(self):
        result = quality.leader_identity(self.row(), {})
        self.assertFalse(result['confirmed'])
        self.assertEqual(result['role'], 'POPULAR_CANDIDATE')

    def test_sample_leader_and_leading_group_both_supported(self):
        self.assertTrue(quality.leader_identity(self.row(leader='002552', leader_pct=4.07), {})['confirmed'])
        self.assertTrue(quality.leader_identity(self.row(pct=8.5), {})['confirmed'])

    def test_missing_leader_or_underperforming_board_cannot_prove_leadership(self):
        for row in (self.row(leader=''), self.row(pct=2, leader='002552', leader_pct=2)):
            self.assertFalse(quality.leader_identity(row, {})['confirmed'])


class MethodTests(unittest.TestCase):
    def anfu(self):
        c = daily.contract()
        c['daily_metrics'].update(daily_asof='2026-09-14', close_sum4=170.08, close_sum19=767.24,
                                  daily_atr14=2.489285714, daily_resistance_levels=[44.52])
        bars = [{'open': o, 'high': h, 'low': l, 'close': cl, 'volume': v,
                 'bar_end': '2026-09-15 '+end, 'timestamp_convention': 'end'}
                for o,h,l,cl,v,end in [(43.01,43.10,42.92,42.98,1067,'10:45:00'),
                                      (42.98,43.03,42.86,42.92,1211,'10:50:00'),
                                      (42.92,42.98,42.88,42.94,499,'10:55:00')]]
        return c, bars

    def test_anfu_no_longer_counts_as_anchor_touch(self):
        c, bars = self.anfu()
        result = method_entry.evaluate(c,bars,42.86,datetime(2026,9,15,10,59),{})
        self.assertFalse(result['eligible'])
        self.assertIn('等待日线MA5', result['blockers'][0])

    def test_even_wide_touch_cannot_rescue_two_cent_weak_confirm_or_live_low_break(self):
        c,bars = self.anfu()
        result = method_entry.evaluate(c,bars,42.86,datetime(2026,9,15,10,59),{'max_touch_distance_pct': 2})
        self.assertFalse(result['eligible'])
        self.assertTrue(any('幅度不足' in x for x in result['blockers']))
        self.assertTrue(any('跌破确认' in x for x in result['blockers']))

    def test_touch_cannot_be_same_bar_as_confirmation(self):
        bars = daily.bars()
        for bar in bars[:-1]:
            bar.update(low=10.05, high=10.07, close=10.06)
        bars[-1].update(open=10, low=9.99, close=10.08, high=10.09)
        self.assertFalse(method_entry.evaluate(daily.contract(), bars, 10.08, daily.NOW, {})['eligible'])

    def test_weak_repair_requires_directional_resume_not_volume_alone(self):
        c,bars = self.anfu()
        for volume in (499,5000):
            bars[-1]['volume'] = volume
            result = quality.weak_repair_evidence(c,bars,42.94,43.36,'REPAIR','RALLY_FAILURE')
            self.assertTrue(result['required']); self.assertFalse(result['confirmed'])
        bars[-1].update(high=43.12,close=43.10,volume=1500)
        self.assertTrue(quality.weak_repair_evidence(c,bars,43.10,43.36,'REPAIR','RALLY_FAILURE')['confirmed'])
        self.assertFalse(quality.weak_repair_evidence(c,bars,42.99,43.36,'REPAIR','RALLY_FAILURE')['confirmed'])

    def test_strong_trend_ma5_below_vwap_not_globally_banned(self):
        self.assertFalse(quality.weak_repair_evidence(daily.contract(),daily.bars(),10.04,10.06,'TREND','NORMAL')['required'])
        self.assertTrue(method_entry.evaluate(daily.contract(),daily.bars(),10.04,daily.NOW,{})['eligible'])


class GuardTests(unittest.TestCase):
    def guard(self, stamp='10:00:00', convention='end'):
        return quality.confirmation_guard({'bar_end': '2026-09-15 '+stamp,'timestamp_convention': convention},
                                           datetime(2026,9,15,14),10,10.02)

    def test_live_break_cannot_be_rescued_by_better_rr(self):
        self.assertIsNotNone(quality.guard_reason(self.guard(),10.01,datetime(2026,9,15,10,1)))
        self.assertIsNone(quality.guard_reason(self.guard(),10.02,datetime(2026,9,15,10,1)))

    def test_new_bar_expires_even_inside_ninety_second_ttl(self):
        self.assertIsNone(quality.guard_reason(self.guard(),10.1,datetime(2026,9,15,10,4,59)))
        self.assertIsNotNone(quality.guard_reason(self.guard(),10.1,datetime(2026,9,15,10,5)))

    def test_lunch_start_end_stamp_and_future(self):
        self.assertEqual(self.guard('11:30:00')['valid_until'],'2026-09-15 13:05:00')
        self.assertEqual(self.guard('09:59:00','start')['confirmed_at'],'2026-09-15 10:00:00')
        self.assertIsNotNone(quality.guard_reason(self.guard(),10.1,datetime(2026,9,15,9,59)))
        bad = quality.confirmation_guard({'bar_end':'2026-09-16 10:00:00'},daily.NOW,10)
        self.assertTrue(bad.get('error'))

    def signal(self):
        s = contracts.entry_signal()
        s['updated_at'] = '2026-08-26 10:04:30'
        s['timing_v2']['confirmation_guard'] = {'required':True, 'confirmed_at':'2026-08-26 10:00:00',
            'valid_until':'2026-08-26 10:05:00','price_floor':9.99}
        return s

    def test_contract_and_broker_enforce_bar_expiry(self):
        s = self.signal()
        c = signal_contract.from_signal(s)
        self.assertEqual(c.expires_at,'2026-08-26 10:05:00')
        self.assertGreaterEqual(c.exec_low,9.99)
        self.assertEqual(sim_broker.simulate_signal(s,{},datetime(2026,8,26,10,4,31),100).status,'FILLED')
        self.assertNotIn(sim_broker.simulate_signal(s,{},datetime(2026,8,26,10,5),100).status, {'FILLED','PARTIAL_FILLED'})

    def test_refresh_timestamp_does_not_renew_confirmation(self):
        s = self.signal(); s['updated_at'] = '2026-08-26 10:05:01'
        self.assertFalse(signal_contract.from_signal(s).sim_allowed)

    def test_price_changes_cannot_cross_confirmation_floor(self):
        s = self.signal(); s['current_price'] = 9.98
        self.assertFalse(signal_contract.from_signal(s).sim_allowed)

    def test_new_version_missing_evidence_fails_closed(self):
        s = contracts.entry_signal()
        s['timing_v2']['version'] = timing.load_config()['strategy_version']
        self.assertFalse(signal_contract.from_signal(s).sim_allowed)

    def test_guard_changes_contract_identity(self):
        s = self.signal(); before = signal_contract.from_signal(s).contract_hash
        s['timing_v2']['confirmation_guard']['price_floor'] = 10
        self.assertNotEqual(before,signal_contract.from_signal(s).contract_hash)

    def test_rationale_uses_contract_band_and_separates_structure(self):
        s = self.signal(); s.update(signal_contract=signal_contract.from_signal(s).to_dict(),structural_invalidation=9.5)
        result = strategy_discipline.build_strategy_rationale(s,'BUY')
        self.assertIn('入场失效',result['framework']['chips'])
        self.assertIn('大结构参考',result['framework']['chips'])
        self.assertIn('执行契约成本后RR',result['framework']['space'])
        self.assertNotIn('9.50',result['framework']['space'])

    def test_discipline_uses_same_contract_band_as_broker(self):
        s = self.signal()
        s['signal_contract'] = signal_contract.from_signal(s).to_dict()
        s['execution_band_high'] = 9.99
        self.assertTrue(strategy_discipline._buy_chase_ok(s)[0])
        s['current_price'] = 9.98
        self.assertFalse(strategy_discipline._buy_chase_ok(s)[0])


class IntegrationTests(unittest.TestCase):
    def test_weak_ma5_exception_is_guarded_in_full_timing_but_can_repair(self):
        for repaired in (False, True):
            bars = daily.bars()
            price = 10.08 if repaired else 10.04
            if repaired:
                bars[-1].update(close=10.08, high=10.10)
            row = {'quote': {'close':price,'open':10,'prev_close':10,'high':10.10,'low':9.99},
                   'strategy_contract':daily.contract(),
                   'rt_features':{'vwap':10.12,'amount_ratio_1m':1.2,'amount_ratio_5m':1.2,'atr5m':.1}}
            with patch.object(timing,'closed_bars',side_effect=[bars,[]]), \
                 patch.object(timing,'_full_regime',return_value=('REPAIR',{},[])):
                result = timing.evaluate(row,[{'m':'09:59:00','p':price}],daily.NOW,
                                         history_120m=old.history_120m())
            self.assertEqual(result['entry_allowed'],repaired,result['blockers'])
            if repaired:
                self.assertEqual(result['confirmation_guard']['trigger_level'],10.07)
            else:
                self.assertTrue(any('MA5弱势修复' in x for x in result['blockers']))

    def test_daily_target_must_not_skip_unbroken_nearer_pressure(self):
        row = {'quote': {'close':10.04,'open':10,'prev_close':10,'high':10.07,'low':9.99},
               'pressure':10.05,'strategy_contract':daily.contract(),
               'rt_features':{'vwap':10.01,'amount_ratio_1m':1.2,'amount_ratio_5m':1.2,'atr5m':.1}}
        with patch.object(timing,'closed_bars',side_effect=[daily.bars(),[]]):
            result = timing.evaluate(row,[{'m':'09:59:00','p':10.04}],daily.NOW,
                                     history_120m=old.history_120m())
        self.assertFalse(result['entry_allowed'])
        self.assertEqual(result['levels']['nearest_resistance'],10.05)
        self.assertTrue(any('盈亏比不足' in x for x in result['blockers']))

    def evaluate_leader(self, leader='000001'):
        row = old.row(10.35,10.4,10.18)
        row.update(code='000001',pressure=10.30,
                   strategy_contract={'key':'LEADER_EMOTION','daily_qualified':True,'is_observation_strategy':True,
                                      'allowed_patterns':['V2_E5B_LEADER_OPENING_HOLD']},
                   sector_momentum={'emotion_ok':True,'board_pct':2},
                   sector_rotation={'sustained':True,'leader_healthy':True,'leader':{'code':leader,'pct':10 if leader!='000001' else 3.5}})
        row['quote'].update(open=10.2,pct=3.5,limit_up=11,amount_wan=20000)
        row['rt_features']['vwap'] = 10.25
        row['rt_features']['atr5m'] = .1
        bar = {'open':10.2,'high':10.4,'low':10.18,'close':10.35,'volume':10000,
               'bar_end':'2026-09-15 09:35:00','timestamp_convention':'end'}
        cfg = timing.load_config(); cfg['history']['min_15m_bars'] = 0
        with patch.object(timing,'closed_bars',side_effect=[[bar],[]]):
            return timing.evaluate(row,[{'m':'09:35:00','p':10.35}],datetime(2026,9,15,9,35),
                                   history_120m=old.history_120m(),config=cfg)

    def test_real_opening_route_still_passes_with_own_leadership(self):
        r = self.evaluate_leader()
        self.assertTrue(r['entry_allowed'],r['blockers'])
        self.assertEqual(r['entry_pattern'],'V2_E5B_LEADER_OPENING_HOLD')
        self.assertEqual(r['confirmation_guard']['valid_until'],'2026-09-15 09:40:00')
        self.assertEqual(r['timing_replay_input']['expected']['entry_allowed'],True)
        self.assertTrue(r['timing_replay_input'].get('sha256'))

    def test_other_leader_cannot_grant_formal_permission(self):
        r = self.evaluate_leader('600000')
        self.assertFalse(r['entry_allowed'])
        self.assertTrue(any('本票领导力' in x for x in r['blockers']))
        score = opportunity_rating.rate_signal({'timing_v2':r})
        self.assertNotEqual(score['grade'],'A')
        self.assertFalse(score['execution_ready'])
        self.assertFalse(score['score_is_probability'])

    def test_expired_or_broken_confirmation_cannot_keep_high_execution_score(self):
        r = self.evaluate_leader()
        r['entry_allowed'] = False
        r['entry_quality']['confirmation_reason'] = '现价已破坏确认结构'
        rating = opportunity_rating.rate_signal({'timing_v2':r})
        self.assertEqual(rating['components']['execution_5m'],0)
        self.assertNotEqual(rating['grade'],'A')


if __name__ == '__main__':
    unittest.main()
