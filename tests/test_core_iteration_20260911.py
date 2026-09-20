import copy
import time
import unittest
from datetime import datetime, timedelta
from tempfile import TemporaryDirectory
from unittest.mock import patch

import realtime_signal_engine as engine
import intraday_report as intra
from core import sector_identity, intraday_timing_v2 as timing, local_market_data as market, global_risk
import test_strategy_optimization as fixtures


class BreadthTests(unittest.TestCase):
    def calculate(self, amounts, times=None, ids=None):
        cache = {}
        results = []
        for i, amount in enumerate(amounts):
            rows = [{'f12': str(j+(ids[i] if ids else 0)), 'f3': 1 if j < 12 else -1 if j < 99 else 0,
                     'f6': amount} for j in range(100)]
            results.append(engine.build_market_breadth(rows, cache,
                now=times[i] if times else datetime(2026, 9, 11, 10)+timedelta(minutes=2*i), expected_total=100))
        return results

    def test_weak_breadth_independent_of_turnover(self):
        result = self.calculate([100, 101])[-1]
        self.assertEqual(result['state'], 'broad_weak')
        self.assertIsNone(result['amount_ratio'])
        regime = engine.build_a_share_market_regime({'sh000001': {'pct': -1.18},
            'sz399001': {'pct': -1.08}, 'sz399006': {'pct': -.49}}, breadth=result)
        self.assertEqual(regime['state'], 'rotation_defensive')

    def test_cumulative_increasing_rate_shrinks(self):
        result = self.calculate([100, 110, 115])[-1]
        self.assertEqual(result['state'], 'shrinking_weak')
        self.assertEqual(result['amount_ratio'], .5)

    def test_unequal_intervals_normalize_rates(self):
        start = datetime(2026, 9, 11, 10)
        result = self.calculate([100, 110, 115], [start, start+timedelta(minutes=2), start+timedelta(minutes=3)])[-1]
        self.assertEqual(result['amount_ratio'], 1)

    def test_cumulative_decrease_is_not_shrink(self):
        result = self.calculate([100, 110, 90])[-1]
        self.assertEqual(result['turnover_state'], 'cumulative_reset')
        self.assertIsNone(result['amount_ratio'])

    def test_lunch_duplicate_and_day_reset_have_no_rate(self):
        for times in ([datetime(2026,9,11,11,28), datetime(2026,9,11,11,30), datetime(2026,9,11,13,1)],
                      [datetime(2026,9,11,10)]*3,
                      [datetime(2026,9,10,10),datetime(2026,9,10,10,2),datetime(2026,9,11,10,4)]):
            self.assertIsNone(self.calculate([100,110,115],times)[-1]['amount_ratio'])

    def test_universe_change_and_partial_data_never_fake_rate(self):
        self.assertIsNone(self.calculate([100,110,115],ids=[0,0,1])[-1]['amount_ratio'])
        result = engine.build_market_breadth([{'f12':'1','f3':-1,'f6':100}],expected_total=1000)
        self.assertFalse(result['coverage_ready'])
        self.assertEqual(result['state'], 'data_stale')


class SectorTests(unittest.TestCase):
    def test_authorized_theme_requires_persistence_not_just_larger_return(self):
        boards = [{'f12':'BK1','f14':'玻璃玻纤','f3':1}, {'f12':'BK2','f14':'商业航天','f3':2}]
        quote={'industry':'玻璃玻纤','concepts':['商业航天']}
        metrics={};sector_identity.apply(metrics,sector_identity.resolve({'quote':quote},boards))
        result=intra.sector_momentum_for_candidate(quote,boards,contract={'key':'LEADER_EMOTION','daily_metrics':metrics},
            rotations={'玻璃玻纤':{'sustained':False},'商业航天':{'sustained':True,'leader_healthy':True}})
        self.assertEqual(result['board_id'],'BK2')
    def test_business_not_displaced_by_one_theme(self):
        boards = [{'f12':'BK1','f14':'玻璃玻纤','f3':2}, {'f12':'BK2','f14':'商业航天','f3':-2}]
        stock = {'quote': {'industry':'玻璃玻纤','concepts':['商业航天']}}
        result = sector_identity.resolve(stock, boards)
        self.assertEqual(result['names'], ['玻璃玻纤','商业航天'])
        self.assertEqual(result, sector_identity.resolve(stock,[{**b,'f3':99} for b in boards]))
        metrics = {}; sector_identity.apply(metrics,result)
        momentum = intra.sector_momentum_for_candidate(stock['quote'],boards,contract={'daily_metrics':metrics})
        self.assertEqual(momentum['board_id'], 'BK1')

    def test_industry_and_concept_keep_separate_codes(self):
        boards = [{'f12':'BK1','f14':'印制电路板'}, {'f12':'BK2','f14':'PCB'}]
        result = sector_identity.resolve({'quote':{'industry':'印制电路板','concepts':['PCB']}}, boards)
        self.assertEqual(result['ids'], ['BK1','BK2'])

    def test_explicit_authorization_is_not_silently_expanded(self):
        boards = [{'f12':'BK1','f14':'玻璃玻纤'},{'f12':'BK2','f14':'商业航天'}]
        result = sector_identity.resolve({'quote':{'industry':'玻璃玻纤','concepts':['商业航天']},
                                         'resonance_boards':['商业航天']},boards)
        self.assertEqual(result['names'], ['商业航天'])


class LeaderTests(unittest.TestCase):
    def decision(self, gap=-1, b=None, sector=True, prev=10, when=None):
        subject = {'quote':{'prev_close':prev,'open':10*(1+gap/100),'pct':2.5,'amount_wan':20000,'turnover':5},
            'strategy_contract':{'key':'LEADER_EMOTION','daily_qualified':True},
            'sector_momentum':{'emotion_ok':sector,'board_pct':2},
            'sector_rotation':{'sustained':sector,'leader_healthy':sector}}
        bars = b or [{'open':9.9,'low':9.96,'high':10.2,'close':10.12},
                     {'open':10.12,'low':10.12,'high':10.27,'close':10.25}]
        return timing._leader_opening_hold_state(subject,bars,10.1,.2,10.25,'TREND','NORMAL',
                       when or datetime(2026,9,11,9,40),timing.load_config())

    def test_small_gap_two_closed_bars_can_qualify(self):
        result = self.decision()
        self.assertTrue(result['eligible'],result)
        self.assertEqual(result['entry_variant'],'SMALL_GAP_RECLAIM')

    def test_small_gap_one_bar_cannot_qualify(self):
        self.assertFalse(self.decision(b=[{'open':9.9,'low':10.1,'high':10.27,'close':10.25}])['eligible'])

    def test_deep_gap_missing_close_weak_sector_late_stay_blocked(self):
        for kwargs in ({'gap':-6.72},{'prev':0},{'sector':False},{'when':datetime(2026,9,11,10,20)}):
            self.assertFalse(self.decision(**kwargs)['eligible'])

    def test_no_reclaim_or_falling_low_cannot_qualify(self):
        for b in ([{'high':10.2,'close':9.99,'low':9.95},{'high':10.27,'close':10.25,'low':10.12}],
                  [{'high':10.2,'close':10.12,'low':10.1},{'high':10.27,'close':10.25,'low':10.0}]):
            self.assertFalse(self.decision(b=b)['eligible'])


class PermissionAndRiskTests(unittest.TestCase):
    def test_scope_can_narrow_only_after_confirmed_recovery(self):
        fresh={'risk_level':'red','risk_scope':'technology','quotes':{'nasdaq':-1.2,'kospi':.2,'nikkei':1,'hsi':1}}
        with patch.object(global_risk,'infer_premarket_context',side_effect=[copy.deepcopy(fresh),copy.deepcopy(fresh)]):
            repaired=global_risk.infer_intraday_context({'risk_level':'red','risk_scope':'broad'},'2026-09-11',[],
                {'recovery_confirmed':True,'event_state':'deep_v_recovery'},datetime(2026,9,11,11))
            blocked=global_risk.infer_intraday_context({'risk_level':'red','risk_scope':'broad'},'2026-09-11',[],
                {'recovery_confirmed':False,'event_state':'shock_unrepaired'},datetime(2026,9,11,11))
        self.assertEqual(repaired['risk_scope'],'technology')
        self.assertEqual(blocked['risk_scope'],'broad')
        self.assertTrue(blocked['policy']['allow_core_attack_buy'])
        self.assertEqual(blocked['entry_risk_source'], 'external_only')
    def test_health_ok_can_mean_trading_closed(self):
        health = {'global_risk':{'policy':{'allow_core_attack_buy':False}}}
        engine.apply_runtime_quality(health)
        self.assertEqual(health['engine_status'],'ok')
        self.assertEqual(health['new_entry_status'],'risk_blocked')
        view = intra.market_permission_view({**health,'can_attack':True},{})
        self.assertFalse(view['can_attack'])
        self.assertFalse(view['preparation_blocked'])
        self.assertIn('全局风险',view['permission'])

    def test_funnel_exposes_real_veto(self):
        signal = {'symbol':'1','strategy_family':'THREE_METHOD',
                  'timing_v2':{'data_quality':{'market_data':{'market_gate':{'allowed':False}}}}}
        result = engine.entry_pipeline_summary([signal])
        self.assertEqual(result['passed']['global_readiness'],1)
        self.assertEqual(result['passed']['market_permission'],0)
        self.assertEqual(result['first_blocked_symbols']['market_permission'],['1'])

    def test_green_push_rechecks_current_global_permission(self):
        with patch.object(engine,'REALTIME_SIGNAL_FEISHU_ENABLED',True),patch.object(engine.urllib.request,'urlopen') as send:
            engine.send_feishu([{'scenario':'anything'}],{'new_entry_status':'risk_blocked'})
        send.assert_not_called()

    def test_card_explains_type_strategy_price_time_and_execution(self):
        text = engine.formal_entry_signal_brief({'symbol':'600001','name':'测试',
            'strategy_contract':{'key':'TREND_520','name':'520战法'},'signal_contract':{'exec_low':10,'exec_high':10.1,
             'invalid_price':9.8,'target_price':10.8,'expires_at':'2026-09-11 10:02'},
            'sector_momentum':{'board_name':'元件','board_pct':2}})
        for expected in ['趋势票','520战法','10.00','10.10','9.80','10.80','10:02','尚未确认模拟成交','元件']:
            self.assertIn(expected,text)

    def test_small_room_requires_structural_rr_not_one_daily_atr(self):
        c = fixtures.contract();c['daily_metrics']['daily_resistance_levels']=[10.20]
        with patch.object(fixtures,'contract',return_value=c):
            result = fixtures.TimingConsistencyTests().evaluate()
        self.assertLess(result['room_risk']['room_atr'],1)
        self.assertGreaterEqual(result['room_risk']['reward_risk'],1.5)
        self.assertTrue(result['entry_allowed'],result['blockers'])

    def test_nearby_pressure_still_blocks_poor_rr(self):
        c = fixtures.contract();c['daily_metrics']['daily_resistance_levels']=[10.05]
        with patch.object(fixtures,'contract',return_value=c):
            result = fixtures.TimingConsistencyTests().evaluate()
        self.assertFalse(result['entry_allowed'])
        self.assertTrue(any('盈亏比' in b for b in result['blockers']))


class FetchBudgetTests(unittest.TestCase):
    def test_timeout_defers_without_late_store_writes(self):
        state = {}
        def slow(*args):
            time.sleep(.06)
            return [{'close':10}]
        with TemporaryDirectory() as root,patch.object(market,'fetch_tencent_bars',side_effect=slow),\
             patch.object(market,'upsert_bars') as store,patch.object(market,'_load_today_minute_rows',return_value=([],None)):
            _, quality = market.refresh_intraday_minutes(root,['1','2','3'],state=state,max_workers=1,budget_seconds=.01)
            self.assertEqual(len(quality['deferred_symbols']),3)
            self.assertEqual(state['minute_codes'],set())
            time.sleep(.08)
            store.assert_not_called()


if __name__ == '__main__':
    unittest.main()
