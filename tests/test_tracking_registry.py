import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import after_close_report as base
from core import tracking_registry as registry
from research.import_tracking_inventory import merge, parse_inventory


def record(code='600000', name='测试股票', kind='equity', groups=None):
    return {'code': code, 'name': name, 'kind': kind, 'groups': groups or ['自选股'], 'entry_authorized': False}


class TrackingRegistryTests(unittest.TestCase):
    def test_etf_is_excluded_but_lof_and_index_are_reference_only(self):
        self.assertEqual(registry.instrument_kind('518880', '黄金ETF华安'), 'etf')
        self.assertEqual(registry.instrument_kind('501026', '财通福享混合LOF'), 'reference_fund')
        self.assertEqual(registry.instrument_kind('881157', '证券'), 'reference_index')
        self.assertEqual(registry.instrument_kind('920808', '曙光数创'), 'equity')

    def test_duplicates_etfs_and_authorization_are_rejected(self):
        for rows in [[record(), record()], [record('518880', '黄金ETF', 'etf')],
                     [{**record(), 'entry_authorized': True}],
                     [record('881157', '证券', 'equity')]]:
            with self.assertRaises(ValueError):
                registry.validate({'version': 1, 'verified_at': '2026-09-14', 'records': rows})

    def test_merge_is_additive_and_reports_missing(self):
        result = merge({'records': [record()]}, [record('000001')], 'checked')
        self.assertEqual(len(result['records']), 2)
        self.assertEqual(result['missing_from_latest_inventory'], ['600000'])
        self.assertTrue(all(r['entry_authorized'] is False for r in result['records']))

    def test_invalid_registry_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root/'configs').mkdir()
            (root/'configs/tracking_watchlist.json').write_text('{}')
            result = registry.read(root)
            self.assertEqual(result['records'], [])
            self.assertTrue(result['errors'])

    def test_reference_instruments_never_enter_plan_rows(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root/'configs').mkdir()
            records = [record(), record('501026', '财通福享混合LOF', 'reference_fund'),
                       record('881157', '证券', 'reference_index')]
            (root/'configs/tracking_watchlist.json').write_text(json.dumps(merge({}, records, 'checked')))
            with patch.object(base, 'BASE_DIR', root), patch.object(base, 'THS_WATCHLIST_DIR', str(root/'missing')), \
                 patch.object(base, 'DEFAULT_OBSERVATION_GROUP_SUFFIXES', ()), patch.object(base, 'read_watchlist_details', return_value={'rows': []}), \
                 patch.object(base, 'PREMARKET_PLAN_OBSERVATION_GROUPS', ('ALL',)):
                details = base.read_observation_watchlist_details()
                plan = base.premarket_plan_observation_details(details)
            self.assertEqual(plan['rows'], [('600000', '17')])
            self.assertEqual(len(details['reference_instruments']), 2)
            self.assertEqual(details['memberships']['600000'], ['自选股'])
            self.assertEqual(len(base.observation_group_summary(details, {})), 2)

    def test_inventory_import_excludes_only_etf(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/'list.md'
            path.write_text('|518880|黄金ETF|基金|自选股|无|无|无|\n|501026|财通福享混合LOF|基金|自选股|无|无|无|\n|600000|股票|A股|自选股|无|无|无|')
            self.assertEqual([r['code'] for r in parse_inventory(path)], ['501026', '600000'])


if __name__ == '__main__':
    unittest.main()
