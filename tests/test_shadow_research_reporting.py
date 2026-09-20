import copy
from datetime import datetime, timedelta
import json
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from core import shadow_research_reporting as reporting, strategy_gap_research as gaps
from test_top10_iteration import payload, NOW


def fixture():
    p = payload()
    result = gaps.evaluate(p)
    frozen = {'input': p, 'expected': copy.deepcopy(result), 'evaluator_sha256': 'test-version'}
    frozen['sha256'] = gaps.digest(frozen)
    result['replay'] = frozen
    signal = {'symbol': '600001', 'name': '样本股票', 'timing_v2': {'gap_research': result}}
    return signal, {'600001': p['quote']}


class ShadowReportingTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.signal, self.quotes = fixture()

    def record(self, now=NOW):
        return reporting.record_tick(self.base, [self.signal], self.quotes, now)

    def summary(self, now=None, quotes=None):
        return reporting.review(self.base, '2026-09-15', quotes or {}, now or NOW)

    def test_first_trigger_restart_dedup_and_no_account_files(self):
        original = copy.deepcopy(self.signal)
        self.assertEqual(self.record()['new'], 1)
        self.assertEqual(self.record()['new'], 0)
        self.assertEqual(self.record(NOW+timedelta(seconds=5))['new'], 0)
        self.assertEqual(len(self.summary()['events']), 1)
        self.assertEqual(self.signal, original)
        self.assertEqual([p.name for p in (self.base/'data/runtime').iterdir()], ['shadow_research.sqlite3'])

    def test_not_ready_malformed_stale_wrong_symbol_and_future_rejected(self):
        for mutate in ('not_ready', 'tamper', 'stale', 'symbol', 'future'):
            self.signal, self.quotes = fixture()
            research = self.signal['timing_v2']['gap_research']
            if mutate == 'not_ready':
                research['routes']['LEADER_EARLY_3M']['shadow_ready'] = False
            elif mutate == 'tamper':
                research['replay']['input']['vwap'] = 1
            elif mutate == 'stale':
                self.quotes['600001']['_stale'] = True
            elif mutate == 'symbol':
                self.quotes['600001']['code'] = '600002'
            else:
                self.quotes['600001']['datetime'] = '20260915093400'
            self.assertEqual(self.record()['new'], 0, mutate)

    def test_no_entry_collection_outside_session_or_after_cutoff(self):
        for now in (NOW.replace(hour=12), NOW.replace(hour=15, minute=1), NOW.replace(hour=14, minute=45)):
            self.assertEqual(self.record(now)['new'], 0)

    def test_repair_included_but_no_order_and_missing_target_rejected(self):
        self.signal['timing_v2'] = {'repair_shadow': {'mode': 'SHADOW_ONLY', 'entry_allowed': False,
            'order_action': 'NO_ORDER', 'shadow_ready': True, 'blockers': [],
            'method_entry': {'target': 11, 'invalidation': 10}}}
        self.assertEqual(self.record()['new'], 1)
        self.assertEqual(self.summary()['events'][0]['route'], 'REPAIR_520')
        self.signal['timing_v2']['repair_shadow']['method_entry']['target'] = None
        self.assertEqual(self.record()['new'], 0)

    def test_success_ack_dedup_and_card_has_no_order_instruction(self):
        self.record()
        sent = []
        def sender(card):
            sent.append(card)
            return {'code': 0}
        result = reporting.notify(self.base, NOW, sender)
        self.assertEqual(result['sent'], 1)
        self.assertEqual(reporting.notify(self.base, NOW+timedelta(seconds=40), sender)['sent'], 0)
        text = json.dumps(sent[0], ensure_ascii=False)
        for word in ('禁止下单', '不是绿色买入信号', '不创建模拟订单', '观察基准价', 'T+1'):
            self.assertIn(word, text)
        self.assertTrue(self.summary()['events'][0]['sent_at'])

    def test_failed_ack_retry_bound_cooldown_and_no_expired_backfill(self):
        self.record()
        for seconds in (0, 30, 60):
            result = reporting.notify(self.base, NOW+timedelta(seconds=seconds), lambda _: {'code': 99})
            self.assertEqual(result['status'], 'delivery_failed')
            self.assertEqual(reporting.notify(self.base, NOW+timedelta(seconds=seconds+1), lambda _: {'code': 0})['status'], 'cooldown')
        self.assertEqual(reporting.notify(self.base, NOW+timedelta(seconds=95), lambda _: {'code': 0})['sent'], 0)
        self.assertFalse(self.summary()['events'][0]['sent_at'])
        self.assertEqual(self.summary()['events'][0]['attempts'], 3)

    def test_expiry_never_delivered_after_restart(self):
        self.record()
        sender = unittest.mock.Mock(return_value={'code': 0})
        self.assertEqual(reporting.notify(self.base, NOW+timedelta(minutes=3), sender)['sent'], 0)
        sender.assert_not_called()

    def test_missing_ack_and_network_failure_are_not_success(self):
        self.record()
        self.assertEqual(reporting.notify(self.base, NOW, lambda _: {})['status'], 'delivery_failed')
        def fail(_):
            raise TimeoutError('sensitive URL must not be stored')
        self.assertEqual(reporting.notify(self.base, NOW+timedelta(seconds=30), fail)['error'], 'TimeoutError')
        self.assertNotIn('sensitive', json.dumps(self.summary()))

    def test_forward_prices_no_pretrigger_daily_extremes_and_report_does_not_write(self):
        self.record()
        for minute, price in ((34, 10.6), (35, 9.8), (36, 10.4)):
            self.quotes['600001'] = {'code': '600001', 'datetime': f'2026091509{minute}15',
                                    'close': price, 'high': 100, 'low': 1}
            reporting.record_tick(self.base, [], self.quotes, NOW.replace(minute=minute))
        close = {'600001': {'code': '600001', 'datetime': '20260915150000', 'close': 10.5, 'high': 100, 'low': 1}}
        before = reporting.db_path(self.base).read_bytes()
        result = self.summary(NOW.replace(hour=15, minute=20), close)
        event = result['events'][0]
        self.assertAlmostEqual(event['close_return_pct'], (10.5/10.2-1)*100)
        self.assertAlmostEqual(event['sampled_mfe_pct'], (10.6/10.2-1)*100)
        self.assertAlmostEqual(event['sampled_mae_pct'], (9.8/10.2-1)*100)
        self.assertTrue(event['invalidation_observed'])
        self.assertFalse(event['target_observed'])
        self.assertEqual(reporting.db_path(self.base).read_bytes(), before)
        self.assertIn('非组合收益', '\n'.join(reporting.text_lines(result)))

    def test_no_close_for_wrong_day_stale_intraday_future_or_symbol(self):
        self.record()
        quote = {'code': '600001', 'datetime': '20260915150000', 'close': 11}
        for diff in ({'datetime': '20260914150000'}, {'datetime': '20260915145900'},
                     {'_stale': True}, {'code': '600002'}, {'close': float('nan')},
                     {'datetime': '20260915152100'}):
            self.assertIsNone(self.summary(NOW.replace(hour=15, minute=20),
                              {'600001': {**quote, **diff}})['events'][0]['close_return_pct'])

    def test_missing_vs_zero_evaluations_and_delivery_card_cap(self):
        self.assertEqual(self.summary()['status'], 'not_recorded')
        reporting.record_tick(self.base, [], {}, NOW)
        self.assertEqual(self.summary()['status'], 'recorded')
        self.assertEqual(len(self.summary()['events']), 0)
        self.assertIn('有效评估 0', '\n'.join(reporting.text_lines(self.summary())))

    def test_engine_sender_disabled_and_error_isolated(self):
        import realtime_signal_engine as engine
        with patch.dict('os.environ', {'A_SHARE_SKIP_FEISHU': '1'}), patch.object(reporting, 'notify') as notify:
            self.assertEqual(engine.send_feishu_shadow_research(NOW)['status'], 'disabled')
            notify.assert_not_called()
        with patch.dict('os.environ', {'A_SHARE_SKIP_FEISHU': '0'}), patch.object(reporting, 'notify', side_effect=OSError()):
            self.assertFalse(engine.send_feishu_shadow_research(NOW)['execution_affected'])

    def test_afterclose_markdown_and_feishu_include_separate_summary(self):
        import after_close_report as close
        self.record()
        review = {'summary': [], 'shadow_research': self.summary()}
        with patch.object(close, 'load_quote_cache', return_value={}), patch.object(close, 'market_review_lines', return_value=[]), \
             patch.object(close.mira_research_gate, 'markdown_section_from_text', return_value=[]):
            text = close.make_report([], [], [], {}, signal_review=review)
        self.assertIn('影子研究信号表现（禁止下单，不计入账户）', text)
        self.assertIn('样本股票', text)
        with patch.dict('os.environ', {'A_SHARE_SKIP_FEISHU': '0'}), patch.object(close.urllib.request, 'urlopen') as urlopen:
            urlopen.return_value.__enter__.return_value.read.return_value = b'{"code":0}'
            close.send_feishu([], [], [], signal_review=review)
        card = json.loads(urlopen.call_args.args[0].data)
        self.assertIn('影子研究表现', json.dumps(card, ensure_ascii=False))


if __name__ == '__main__':
    unittest.main()
