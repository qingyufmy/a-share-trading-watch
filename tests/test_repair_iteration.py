import copy
import json
from datetime import datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from core import local_market_data as market, observation_strategy_router as router
from core import repair_observation as shadow, intraday_timing_v2 as timing
import premarket_report as premarket
import realtime_signal_engine as engine
from test_afterclose_iteration import contract
from test_local_market_data import sixty_minute_rows

NOW = datetime(2026, 9, 7, 9, 45)


def minutes(start=datetime(2026, 9, 7, 9, 31), count=120):
    return [dict(source='tencent', bar_end=str(start+timedelta(minutes=i)),
                 open=10, high=11, low=9, close=10+i/1000, volume=100) for i in range(count)]


class ClosedStructureTests(unittest.TestCase):
    def test_does_not_use_unclosed_morning(self):
        self.assertEqual(market.build_session_bars(minutes(), 120, NOW.replace(hour=11, minute=29)), [])

    def test_missing_and_duplicate_minutes_cannot_complete_session(self):
        rows = minutes(); rows[-2] = rows[0]
        self.assertEqual(market.build_session_bars(rows, 120, NOW.replace(hour=11, minute=30)), [])

    def test_lunch_and_close_are_separate_complete_sessions(self):
        rows = minutes()+minutes(datetime(2026, 9, 7, 13, 1))
        history, quality = market.merge_completed_120m([], rows, NOW.replace(hour=15, minute=0))
        self.assertEqual([b['bar_end'] for b in history], ['2026-09-07 11:30:00','2026-09-07 15:00:00'])
        self.assertEqual(history[-1]['close'], rows[-1]['close'])
        self.assertTrue(quality['current_session_ready'])

    def test_duplicate_overlay_is_idempotent(self):
        at = NOW.replace(hour=11, minute=30)
        first, _ = market.merge_completed_120m([], minutes(), at)
        second, _ = market.merge_completed_120m(first, minutes(), at)
        self.assertEqual(first, second)

    def test_start_stamped_session_canonical_end(self):
        rows = minutes(datetime(2026, 9, 7, 9, 30))
        for row in rows: row.pop('source')
        history, q = market.merge_completed_120m([], rows, NOW.replace(hour=11, minute=30))
        self.assertEqual(history[0]['bar_end'], '2026-09-07 11:30:00')
        self.assertTrue(q['current_session_ready'])

    def test_provider_completed_session_can_supply_missing_minutes(self):
        history = [{'bar_end':'2026-09-07 11:30:00','close':10}]
        self.assertTrue(market.merge_completed_120m(history, [], NOW.replace(hour=13, minute=20))[1]['current_session_ready'])

    def test_runtime_quality_rejects_old_history_after_close(self):
        with TemporaryDirectory() as tmp:
            for source in ('tencent','sina'):
                market.upsert_bars(tmp,'000001','60m',sixty_minute_rows(120,source))
            _, before = market.history_120m(tmp,'000001',NOW)
            _, stale = market.history_120m(tmp,'000001',NOW.replace(hour=11,minute=30))
            self.assertTrue(before['entry_ready'])
            self.assertFalse(stale['entry_ready'])
            market.upsert_bars(tmp,'000001','1m',minutes())
            history, after = market.history_120m(tmp,'000001',NOW.replace(hour=11,minute=30))
            self.assertTrue(after['entry_ready'],after)
            self.assertEqual(history[-1]['bar_end'],'2026-09-07 11:30:00')
            self.assertFalse(market.history_120m(tmp,'000001',NOW.replace(hour=15,minute=0))[1]['entry_ready'])


class ClassificationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        path = Path(__file__).resolve().parents[1]/'output/watchlist_replay_20260907_171920/daily_inputs_t1.json'
        cls.daily = json.loads(path.read_text())

    def classify(self, code):
        return router.classify_stock({'quote':{'code':code,'name':code,'industry':'电子'},'daily':self.daily[code]})

    def test_defu_recent_event_report_is_not_false_absence(self):
        c = self.classify('301511')
        self.assertEqual(c['daily_metrics']['cross_days_ago'],3)
        self.assertIn('存在近5日520事件',c['reason'])
        self.assertNotIn('无近5日',c['reason'])
        self.assertFalse(c['daily_qualified'])
        self.assertEqual(c['name'],'趋势修复观察')
        self.assertFalse(c['daily_metrics']['major_trend_intact'])

    def test_dongtian_is_repair_not_authorized_trend(self):
        c = self.classify('301183')
        self.assertTrue(c['daily_metrics']['repair_watch'])
        self.assertTrue(c['daily_metrics']['major_trend_intact'])
        self.assertFalse(c['daily_qualified'])
        self.assertEqual(c['allowed_patterns'],[])
        self.assertIn('无近5日',c['reason'])

    def test_missing_leader_data_is_explicit(self):
        c = self.classify('301511')
        self.assertIn('人气榜候选证据', c['daily_metrics']['missing_evidence'])
        self.assertIn('流通市值', c['daily_metrics']['missing_evidence'])
        self.assertIn('未核验，不等于指标不达标', router.daily_evidence_text(c))
        self.assertIn('流通市值', c['reason'])

    def test_rehydration_preserves_repair_reason_and_no_permission(self):
        c = self.classify('301183')
        level = {'strategy_key':c['key'],'strategy_name':c['name'],'strategy_style':c['style'],
                 'strategy_daily_metrics':c['daily_metrics'],'strategy_daily_evidence':c['reason']}
        rehydrated = router.contract_from_level(level)
        self.assertEqual(rehydrated['name'],'趋势修复观察')
        self.assertTrue(rehydrated['daily_metrics']['repair_watch'])
        self.assertFalse(router.daily_qualification_gate(rehydrated)[0])
        self.assertFalse(router.sector_resonance_gate(rehydrated,{})[0])


class ShadowTests(unittest.TestCase):
    def setUp(self):
        self.c = contract('OBSERVE_UNCLASSIFIED')
        self.c.update(daily_qualified=False, allowed_patterns=[])
        self.c['daily_metrics'].update(repair_watch=True,major_trend_intact=True,ma5=10,ma20=10,
            resonance_policy='locked_mainline_v1',resonance_boards=['电子'],
            indicator_seed={'ema12':10,'ema26':9.9,'dea':.05,'k':40,'d':35,
                            'highs8':[11]*8,'lows8':[9]*8,'avg_volume5':1000})
        self.bars = [dict(open=10,high=10.07,low=9.99+i*.01,close=p,volume=1000,
                         bar_end=str(NOW-timedelta(minutes=10-i*5)),timestamp_convention='end')
                     for i,p in enumerate((10.01,10.02,10.04))]
        self.row = {'sector_momentum':{'board_name':'电子','board_pct':2,'emotion_ok':True},
                    'sector_rotation':{'sustained':True,'leader_healthy':True},'rt_features':{'vwap':10.01}}

    def evaluate(self,now=NOW,regime='REPAIR'):
        return shadow.evaluate(self.c,self.bars,10.04,now,self.row,regime,timing.load_config())

    def test_positive_shadow_never_becomes_order_permission(self):
        original=copy.deepcopy(self.c)
        r=self.evaluate()
        self.assertTrue(r['shadow_ready'],r)
        self.assertFalse(r['entry_allowed'])
        self.assertEqual(r['order_action'],'NO_ORDER')
        self.assertEqual(self.c,original)

    def test_major_trend_blocks(self):
        self.c['daily_metrics']['major_trend_intact']=False
        self.assertFalse(self.evaluate()['shadow_ready'])

    def test_bear_blocks(self):
        self.assertFalse(self.evaluate(regime='BEAR')['shadow_ready'])

    def test_future_closed_bar_rejected(self):
        self.bars[-1]['bar_end']=str(NOW+timedelta(minutes=5))
        self.assertFalse(self.evaluate()['shadow_ready'])

    def test_missing_closed_bar_rejected(self):
        self.bars.pop(0)
        self.assertFalse(self.evaluate()['shadow_ready'])

    def test_developing_daily_not_added_to_its_own_seed(self):
        self.c['daily_metrics']['daily_asof']='2026-09-07'
        self.assertFalse(self.evaluate()['shadow_ready'])

    def test_wrong_theme_rejected(self):
        self.row['sector_momentum']['board_name']='深圳特区'
        self.assertFalse(self.evaluate()['shadow_ready'])

    def test_insufficient_volume_rejected(self):
        self.c['daily_metrics']['indicator_seed']['avg_volume5']=10000
        self.assertFalse(self.evaluate()['shadow_ready'])

    def test_weak_macd_rejected(self):
        self.c['daily_metrics']['indicator_seed']['ema12']=9
        self.assertFalse(self.evaluate()['shadow_ready'])

    def test_indicator_confirmation_matches_one_daily_update(self):
        r=self.evaluate();seed=self.c['daily_metrics']['indicator_seed']
        expected_dif=(seed['ema12']+2/13*(10.04-seed['ema12']))-(seed['ema26']+2/27*(10.04-seed['ema26']))
        self.assertAlmostEqual(r['indicators']['dif'],expected_dif)

    def test_timing_and_signal_do_not_promote_shadow(self):
        row={'code':'000001','name':'测试','quote':{'code':'000001','name':'测试','close':10.04,'prev_close':10,'open':10},
             'strategy_contract':self.c,**self.row}
        h=[dict(close=8+i*.008,low=7.9+i*.008,high=8.1+i*.008,volume=1000) for i in range(240)]
        with patch.object(timing,'closed_bars',side_effect=[self.bars,[]]):
            row['timing_v2']=timing.evaluate(row,[],NOW,history_120m=h)
        self.assertTrue(row['timing_v2']['repair_shadow']['shadow_ready'])
        self.assertFalse(row['timing_v2']['entry_allowed'])
        with patch.object(engine,'now_dt',return_value=NOW):
            signal=engine.v2_signal_for_row(row)
        self.assertNotIn(signal['scenario'],router.STRATEGY_ENTRY_SCENARIO_SET)
        self.assertFalse(signal['premarket_plan_allows_entry'])


if __name__=='__main__':
    unittest.main()
