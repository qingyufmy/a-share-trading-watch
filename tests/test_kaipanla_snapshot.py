import json
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from core import kaipanla_snapshot as kpl, emotion_leader_pool as pool


class KaipanlaSnapshotTests(unittest.TestCase):
    now = datetime(2026, 9, 8, 16, 49, 10)

    def text(self, date='09-08', end=True):
        return (f'榜单：盘中人气榜\n最后更新时间为{date} 15:00\n'
                '1, 测试一, 600001, 消费, 农业, +3.0%\n'
                '2, 测试二, 600002, PCB, -1.1%\n' + ('已加载完所有数据' if end else ''))

    def test_complete_means_displayed_list_not_top100(self):
        s = kpl.parse_capture(self.text(), 'intraday', self.now)
        self.assertEqual(s['count'], 2)
        self.assertTrue(s['complete'])
        self.assertEqual(s['source_date'], '2026-09-08')
        self.assertEqual(s['rows'][0]['theme'], '消费, 农业')

    def test_partial_and_missing_rank_never_complete(self):
        self.assertFalse(kpl.parse_capture(self.text(end=False), 'intraday', self.now)['complete'])
        self.assertFalse(kpl.parse_capture(self.text().replace('2, 测试二', '3, 测试二'), 'intraday', self.now)['complete'])

    def test_conflicting_rank_rejected(self):
        with self.assertRaises(ValueError):
            kpl.parse_capture(self.text() + '\n1, 冲突, 600003, 农业, +2%', 'intraday', self.now)

    def test_future_or_stale_date_rejected(self):
        for date in ('09-09', '08-31'):
            with self.assertRaises(ValueError):
                kpl.parse_capture(self.text(date), 'intraday', self.now)

    def test_review_without_own_date_cannot_authorize(self):
        text = '榜单：复盘人气榜\n1, 测试, 600001, 农业, +3%\n已加载完所有数据'
        s = kpl.parse_capture(text, 'review', self.now)
        self.assertFalse(s['date_verified'])
        with TemporaryDirectory() as root:
            kpl.archive(root, s)
            self.assertEqual(kpl.load(root, now=self.now), {})

    def test_selected_tab_enforced(self):
        with self.assertRaises(ValueError):
            kpl.parse_capture(self.text(), 'review', self.now)

    def test_newer_capture_does_not_erase_t_minus_one(self):
        with TemporaryDirectory() as root:
            first = kpl.parse_capture(self.text(), 'intraday', self.now)
            old_path = kpl.archive(root, first)
            original = old_path.read_bytes()
            tomorrow = self.now + timedelta(days=1)
            kpl.archive(root, kpl.parse_capture(self.text('09-09'), 'intraday', tomorrow))
            self.assertEqual(kpl.load(root, '2026-09-08', now=tomorrow)['source_date'], '2026-09-08')
            self.assertEqual(kpl.load(root, now=self.now)['source_date'], '2026-09-08')
            self.assertEqual(old_path.read_bytes(), original)
            self.assertEqual(kpl.archive(root, first), old_path)

    def test_corrupt_archive_ignored(self):
        with TemporaryDirectory() as root:
            s = kpl.parse_capture(self.text(), 'intraday', self.now)
            path = kpl.archive(root, s)
            s['rows'][0]['rank'] = 100
            path.write_text(json.dumps(s))
            self.assertFalse(kpl.load(root, now=self.now))

    def test_arriving_crosscheck_rebuilds_previously_excluded_candidate(self):
        with TemporaryDirectory() as root:
            root = Path(root)
            kpl.archive(root, kpl.parse_capture(self.text(), 'intraday', self.now))
            row = {'code': '600001', 'name': '测试', 'rank': 5, 'pct': 3.0, 'turnover': 20,
                   'amount_yi': 12, 'float_market_cap_yi': 100, 'touched_limit': True,
                   'closed_limit': False, 'volume_ratio': 2}
            raw = {'source_date': '2026-09-08', 'fetched_at': str(self.now), 'rows': [row], 'candidates': []}
            (root / 'data/runtime/emotion_leader_pool_latest.json').write_text(json.dumps(raw))
            self.assertEqual(pool.select_leader_candidates(raw, {}), [])
            with patch.object(pool, 'datetime') as clock:
                clock.now.return_value = self.now
                clock.strptime.side_effect = datetime.strptime
                result = pool.load_latest_snapshot(root)
            self.assertEqual(result['candidate_count'], 1)
            self.assertEqual(result['candidates'][0]['leader_profile'], 'FAILED_LIMIT_REVERSAL')
            self.assertTrue(result['candidates'][0]['cross_verified'])
            self.assertTrue(result['kaipanla']['available'])
            self.assertEqual(result['kaipanla']['source_date'], '2026-09-08')
            self.assertEqual(result['kaipanla']['count'], 2)

    def test_observed_evidence_counts_and_date(self):
        directory = Path(__file__).resolve().parents[1] / 'output/kaipanla_iteration_20260908'
        if not directory.exists():
            self.skipTest('Source UI evidence fixture is not installed with runtime tests')
        intra = kpl.parse_capture((directory / '盘中榜界面摘录.txt').read_text(), 'intraday', self.now)
        review = kpl.parse_capture((directory / '复盘榜界面摘录.txt').read_text(), 'review', self.now)
        self.assertEqual(intra['count'], 31)
        self.assertEqual(review['count'], 31)
        self.assertTrue(intra['complete'] and review['complete'])
        self.assertTrue(intra['date_verified'])
        self.assertFalse(review['date_verified'])


if __name__ == '__main__':
    unittest.main()
