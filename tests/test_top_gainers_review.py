import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from research import review_watchlist_top_gainers as audit


DAY = '2026-09-15'


def quote(code, pct=1):
    return {'code': code, 'name': code, 'close': 100 + pct, 'prev_close': 100,
            'pct': pct, 'datetime': '20260915150031', '_stale': False}


class TopGainersReviewTests(unittest.TestCase):
    def test_union_excludes_references_and_deduplicates_core(self):
        snapshots = [{'report_date': DAY, 'rows': [{'code': c} for c in
                      ('002487', '002487', '510300', '160644', '881156', '920808')]},
                     {'report_date': DAY, 'rows': [{'code': '002487'}, {'code': '600001'}]}]
        codes, excluded = audit.universe(snapshots, DAY)
        self.assertEqual(codes, ['002487', '600001', '920808'])
        self.assertEqual(excluded, ['160644', '510300', '881156'])

    def test_wrong_day_universe_rejected(self):
        with self.assertRaises(ValueError):
            audit.universe([{'report_date': '2026-09-14', 'rows': []}], DAY)

    def test_ranking_zero_negative_and_ties(self):
        q = {c: quote(c, p) for c, p in [('600001', 0), ('600002', -1), ('600003', 2), ('600004', 2)]}
        ranked, missing, count = audit.rank_closing_quotes(list(q), q, DAY)
        self.assertEqual([r['symbol'] for r in ranked], ['600003', '600004', '600001', '600002'])
        self.assertEqual(count, 4)
        self.assertEqual(missing, [])

    def test_rejects_stale_intraday_missing_inconsistent_and_nan_quotes(self):
        q = {f'60000{i}': quote(f'60000{i}') for i in range(1, 8)}
        q['600001']['datetime'] = '20260914150031'
        q['600002']['datetime'] = '20260915145959'
        q['600003']['_stale'] = True
        q['600004']['pct'] = float('nan')
        q['600005']['close'] = 110
        q['600006']['code'] = '600007'
        ranked, missing, count = audit.rank_closing_quotes(list(q) + ['600008'], q, DAY)
        self.assertEqual(count, 1)
        self.assertEqual(ranked[0]['symbol'], '600007')
        self.assertEqual(len(missing), 7)

    def test_winner_outside_pool_not_included(self):
        top, _, _ = audit.rank_closing_quotes(['600001'], {'600001': quote('600001'), '600002': quote('600002', 20)}, DAY)
        self.assertEqual([r['symbol'] for r in top], ['600001'])

    def test_node_counts_not_conflated_with_buy_events(self):
        node = {'daily_qualified': True, 'plan_allowed': True, 'sector_ok': True,
                'candidate_pattern': None, 'at': DAY + ' 09:45:00'}
        result = audit.summarize([node] * 3, [], [{'scenario': 'V2_WAIT'}])
        self.assertEqual(result['counts']['observed'], 3)
        self.assertEqual(result['buy_signal_event_count'], 0)
        self.assertIn('未形成策略买点', result['stage'])

    def test_candidate_does_not_mean_simulated_fill(self):
        node = {'daily_qualified': True, 'plan_allowed': True, 'sector_ok': True,
                'candidate_pattern': 'E5', 'at': DAY + ' 09:45:00'}
        result = audit.summarize([node], [], [])
        self.assertEqual(result['filled_buy_count'], 0)
        self.assertIn('剩余执行门控', result['stage'])

    def test_no_coverage_and_no_daily_differ(self):
        self.assertIn('缺少盘中审计覆盖', audit.summarize([], [], [])['stage'])
        self.assertIn('日线资格未通过', audit.summarize([{'at': DAY}], [], [])['stage'])

    def test_corrupt_audit_excluded_not_treated_as_valid_decision(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'audit.jsonl'
            row = {'symbol': '600001', 'price': 1}
            sample = {'timestamp': DAY + ' 09:35:00', 'rows': [row], 'input_sha256': audit.digest([row])}
            bad = {**sample, 'input_sha256': 'tampered'}
            with path.open('x') as stream:
                stream.write(json.dumps(sample) + '\n' + json.dumps(bad) + '\n')
            rows, _, metadata = audit.read_audit(path, DAY, ['600001', '600002'])
            self.assertEqual(len(rows['600001']), 1)
            self.assertEqual(rows['600002'], [])
            self.assertEqual(len(metadata['issues']), 1)

    def test_sql_reader_cannot_write_or_create_database(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'orders.sqlite'
            with sqlite3.connect(path) as con:
                con.execute('CREATE TABLE t (day TEXT)')
            with self.assertRaises(sqlite3.OperationalError):
                audit.query_readonly(path, 'INSERT INTO t VALUES (?)', DAY)
            with self.assertRaises(sqlite3.OperationalError):
                audit.query_readonly(Path(folder) / 'missing.sqlite', 'SELECT ?', DAY)
            self.assertFalse((Path(folder) / 'missing.sqlite').exists())

    def test_report_output_never_overwrites_prior_run(self):
        report = {'date': DAY, 'scope': 'test', 'universe_count': 0, 'quotes_valid': 0,
                  'ranking_complete': False, 'status': 'incomplete', 'top5': [],
                  'limits': [], 'audit': {'sha256': 'test', 'valid_snapshots': 0, 'issues': []}}
        with tempfile.TemporaryDirectory() as folder:
            one = audit.write_report(report, Path(folder))
            before = (one / 'report.md').read_bytes()
            two = audit.write_report(report, Path(folder))
            self.assertNotEqual(one, two)
            self.assertEqual((one / 'report.md').read_bytes(), before)


if __name__ == '__main__':
    unittest.main()
