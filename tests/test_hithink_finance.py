import json
import unittest
from unittest.mock import patch

import after_close_report
from core import hithink_finance as api


class _Response:
    def __init__(self, payload):
        self.payload = payload

    def read(self):
        return json.dumps(self.payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


class HithinkFinanceTests(unittest.TestCase):
    def setUp(self):
        api._KEY_CACHE.update({"checked_at": None, "key": None, "source": "missing"})
        api._SNAPSHOT_CACHE.clear()
        api._LAST_STATUS.update({"state": "not_configured", "source": "none", "updated_at": None})

    def test_configured_key_is_not_reported_as_not_configured_before_first_request(self):
        with patch.object(api, "_read_keychain", return_value="test-key"):
            status = api.api_status()
        self.assertTrue(status["configured"])
        self.assertEqual(status["state"], "configured_idle")

    def test_snapshot_converts_official_schema_without_key_in_url(self):
        payload = {
            "code": 0,
            "request_id": "request-1",
            "data": {"timestamp": 1784275991000, "item": [{
                "ticker": "600519", "last_price": 1277.8, "prev_price": 1256,
                "price_change": 21.8, "price_change_ratio_pct": 1.735669,
                "open_price": 1252.08, "high_price": 1282, "low_price": 1250.21,
                "volume": 3098875, "turnover": 3937375200,
            }]},
        }
        with patch.object(api, "_read_keychain", return_value="test-key"), patch.object(
            api.urllib.request, "urlopen", return_value=_Response(payload)
        ) as urlopen:
            quote = api.fetch_price_snapshots(["600519"])["600519"]
        self.assertEqual(quote["close"], 1277.8)
        self.assertEqual(quote["amount_wan"], 393737.52)
        self.assertEqual(quote["quote_source"], "hithink_finance_api")
        request = urlopen.call_args.args[0]
        self.assertNotIn("test-key", request.full_url)
        self.assertEqual(request.headers["X-api-key"], "test-key")

    def test_daily_endpoint_only_requests_daily_interval(self):
        payload = {"code": 0, "data": {"item": [{
            "date_ms": 1784246400000, "open_price": 10, "high_price": 11,
            "low_price": 9, "close_price": 10.5, "volume": 1000, "turnover": 10500,
        }]}}
        with patch.object(api, "_read_keychain", return_value="test-key"), patch.object(
            api.urllib.request, "urlopen", return_value=_Response(payload)
        ) as urlopen:
            rows = api.fetch_daily_bars("000001", 1784160000000, 1784246400000)
        self.assertEqual(rows[0]["source"], "hithink_finance_api")
        self.assertIn("interval=1d", urlopen.call_args.args[0].full_url)

    def test_missing_key_never_attempts_request(self):
        with patch.object(api, "_read_keychain", return_value=None), patch.object(
            api.urllib.request, "urlopen"
        ) as urlopen:
            self.assertEqual(api.fetch_price_snapshots(["600519"]), {})
        urlopen.assert_not_called()

    def test_official_snapshot_keeps_legacy_turnover_enrichment(self):
        parts = [""] * 40
        parts[1] = "飞龙股份"
        parts[2] = "002536"
        parts[3] = "58.07"
        parts[4] = "52.79"
        parts[5] = "53.00"
        parts[6] = "900000"
        parts[30] = "20260902150000"
        parts[31] = "5.28"
        parts[32] = "10.00"
        parts[33] = "58.07"
        parts[34] = "52.50"
        parts[37] = "540000"
        parts[38] = "15.60"
        legacy = f'v_sz002536="{"~".join(parts)}";'.encode("gbk")
        official = {
            "002536": {
                "name": "002536", "code": "002536", "close": 58.07,
                "prev_close": 52.79, "open": 53.0, "volume_lot": 900000.0,
                "datetime": "20260902150000", "change": 5.28, "pct": 10.0,
                "high": 58.07, "low": 52.5, "amount_wan": 540000.0,
                "turnover": None, "quote_source": "hithink_finance_api",
            }
        }
        with patch.object(api, "fetch_price_snapshots", return_value=official), patch.object(
            after_close_report, "fetch_bytes", return_value=legacy
        ):
            quote = after_close_report.parse_tencent_quotes([("002536", "33")])["002536"]
        self.assertEqual(quote["name"], "飞龙股份")
        self.assertEqual(quote["turnover"], 15.6)
        self.assertEqual(quote["quote_source"], "hithink_finance_api")
