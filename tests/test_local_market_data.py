import json
import unittest
from datetime import datetime, timedelta
from tempfile import TemporaryDirectory

from core import local_market_data as market_data


def sixty_minute_rows(days, source, start=10.0):
    rows = []
    day = datetime(2026, 1, 2, 10, 30)
    close = start
    for index in range(days):
        current = day + timedelta(days=index)
        for slot in ("10:30", "11:30", "14:00", "15:00"):
            close += 0.01
            point = datetime.strptime(f"{current:%Y-%m-%d} {slot}", "%Y-%m-%d %H:%M")
            rows.append({
                "source": source,
                "bar_end": point.strftime("%Y-%m-%d %H:%M:%S"),
                "open": close - 0.01,
                "high": close + 0.02,
                "low": close - 0.02,
                "close": close,
                "volume": 1000,
                "amount": 10000,
            })
    return rows


def fifteen_minute_rows(count, source, start=10.0):
    rows = []
    point = datetime(2026, 7, 1, 9, 45)
    close = start
    for index in range(count):
        close += 0.01
        rows.append({
            "source": source,
            "bar_end": (point + timedelta(minutes=15 * index)).strftime("%Y-%m-%d %H:%M:%S"),
            "open": close - 0.01,
            "high": close + 0.02,
            "low": close - 0.02,
            "close": close,
            "volume": 1000,
            "amount": 10000,
        })
    return rows


class LocalMarketDataTests(unittest.TestCase):
    def test_build_120m_keeps_lunch_as_two_independent_bars(self):
        rows = sixty_minute_rows(1, "tencent")
        bars = market_data.build_120m_bars(rows, datetime(2026, 1, 3))
        self.assertEqual(len(bars), 2)
        self.assertEqual(bars[0]["bar_end"][-8:], "11:30:00")
        self.assertEqual(bars[1]["bar_end"][-8:], "15:00:00")

    def test_fallback_is_a_whole_history_segment_when_primary_is_short(self):
        with TemporaryDirectory() as tmp:
            market_data.upsert_bars(tmp, "000001", "60m", sixty_minute_rows(20, "tencent"))
            market_data.upsert_bars(tmp, "000001", "60m", sixty_minute_rows(120, "sina"))
            bars, quality = market_data.history_120m(tmp, "000001", datetime(2026, 7, 1))
        self.assertEqual(len(bars), 240)
        self.assertEqual(quality["history_source"], "sina")
        self.assertTrue(quality["entry_ready"], quality)

    def test_cross_source_divergence_blocks_new_exposure(self):
        primary = sixty_minute_rows(120, "tencent")
        fallback = sixty_minute_rows(120, "sina")
        fallback[-1]["close"] += 5
        fallback[-2]["close"] += 5
        with TemporaryDirectory() as tmp:
            market_data.upsert_bars(tmp, "000001", "60m", primary)
            market_data.upsert_bars(tmp, "000001", "60m", fallback)
            _bars, quality = market_data.history_120m(tmp, "000001", datetime(2026, 7, 1))
        self.assertFalse(quality["entry_ready"])
        self.assertEqual(quality["cross_check"]["status"], "mismatch")

    def test_isolated_cross_source_disagreement_is_auditable_but_not_a_hard_block(self):
        primary = sixty_minute_rows(120, "tencent")
        fallback = sixty_minute_rows(120, "sina")
        fallback[-1]["close"] += 5
        with TemporaryDirectory() as tmp:
            market_data.upsert_bars(tmp, "000001", "60m", primary)
            market_data.upsert_bars(tmp, "000001", "60m", fallback)
            _bars, quality = market_data.history_120m(tmp, "000001", datetime(2026, 7, 1))
        self.assertTrue(quality["entry_ready"], quality)
        self.assertEqual(quality["cross_check"]["status"], "isolated_mismatch")

    def test_beijing_exchange_symbol_uses_bj_prefix(self):
        self.assertEqual(market_data.provider_symbol("920808"), "bj920808")

    def test_new_listing_history_is_not_a_runtime_source_failure(self):
        with TemporaryDirectory() as tmp:
            market_data.upsert_bars(tmp, "688825", "60m", sixty_minute_rows(20, "tencent"))
            market_data.upsert_bars(tmp, "688825", "60m", sixty_minute_rows(20, "sina"))
            _bars, quality = market_data.history_120m(tmp, "688825", datetime(2026, 7, 1))
        self.assertFalse(quality["entry_ready"])
        self.assertEqual(quality["status"], "STRUCTURAL_HISTORY_PENDING")
        self.assertTrue(quality["structural_history_pending"])

    def test_tencent_parser_reads_minute_bars(self):
        payload = {"data": {"sz000001": {"m1": [["202608130930", "10", "10.1", "10.2", "9.9", "1200"]]}}}
        rows = market_data.parse_tencent_bars(json.dumps(payload), "000001", "1m")
        self.assertEqual(rows[0]["bar_end"], "2026-08-13 09:30:00")
        self.assertEqual(rows[0]["close"], 10.1)

    def test_native_15m_history_is_available_before_one_day_minute_cache_is_complete(self):
        with TemporaryDirectory() as tmp:
            market_data.upsert_bars(tmp, "000001", "15m", fifteen_minute_rows(40, "tencent"))
            market_data.upsert_bars(tmp, "000001", "15m", fifteen_minute_rows(40, "sina"))
            bars, quality = market_data.history_15m(tmp, "000001", datetime(2026, 7, 5))
        self.assertEqual(len(bars), 40)
        self.assertTrue(quality["entry_ready"], quality)
        self.assertEqual(quality["history_source"], "tencent")

    def test_tencent_parser_reads_15m_bars(self):
        payload = {"data": {"sz000001": {"m15": [["202608130945", "10", "10.1", "10.2", "9.9", "1200"]]}}}
        rows = market_data.parse_tencent_bars(json.dumps(payload), "000001", "15m")
        self.assertEqual(rows[0]["bar_end"], "2026-08-13 09:45:00")
        self.assertEqual(rows[0]["close"], 10.1)

    def test_minute_rows_keep_ohlc_and_compute_cumulative_vwap(self):
        rows = [
            {"source": "tencent", "bar_end": "2026-08-25 09:30:00", "open": 10, "high": 12, "low": 9, "close": 11, "volume": 100},
            {"source": "tencent", "bar_end": "2026-08-25 09:31:00", "open": 11, "high": 11, "low": 10, "close": 10, "volume": 300},
        ]
        minute_rows = market_data._as_minute_rows(rows)
        expected = (((12 + 9 + 11) / 3) * 100 + ((11 + 10 + 10) / 3) * 300) / 400
        self.assertAlmostEqual(minute_rows[-1]["avg_p"], expected)
        self.assertNotEqual(minute_rows[-1]["avg_p"], minute_rows[-1]["p"])
        self.assertEqual(minute_rows[0]["h"], 12)

    def test_live_15m_replaces_provider_current_day_snapshot(self):
        native = [
            {"bar_end": "2026-08-24 15:00:00", "close": 10},
            {"bar_end": "2026-08-25 09:45:00", "close": 99},
        ]
        live = [{"bar_end": "2026-08-25 09:44:00", "close": 11}]
        merged = market_data.merge_live_15m_history(native, live, datetime(2026, 8, 25, 10, 0))
        self.assertEqual([row["close"] for row in merged], [10, 11])

    def test_build_15m_requires_every_unique_minute_and_bar_close(self):
        rows = []
        start = datetime(2026, 8, 25, 9, 31)
        for index in range(15):
            if index == 13:
                continue
            point = start + timedelta(minutes=index)
            rows.append({
                "source": "tencent", "bar_end": point.strftime("%Y-%m-%d %H:%M:%S"),
                "open": 10, "high": 10, "low": 10, "close": 10, "volume": 1,
            })
        rows.append(dict(rows[0]))
        self.assertEqual(market_data.build_15m_bars(rows, datetime(2026, 8, 25, 9, 45)), [])

        complete = []
        for index in range(15):
            point = start + timedelta(minutes=index)
            complete.append({
                "source": "tencent", "bar_end": point.strftime("%Y-%m-%d %H:%M:%S"),
                "open": 10, "high": 10, "low": 10, "close": 10, "volume": 1,
            })
        self.assertEqual(market_data.build_15m_bars(complete, datetime(2026, 8, 25, 9, 44)), [])
        self.assertEqual(len(market_data.build_15m_bars(complete, datetime(2026, 8, 25, 9, 45))), 1)

    def test_tencent_end_stamped_minutes_build_all_afternoon_bars(self):
        rows = [{
            "source": "tencent", "bar_end": "2026-08-28 09:30:00",
            "open": 4.12, "high": 4.12, "low": 4.12, "close": 4.12, "volume": 1,
        }]
        for start in (datetime(2026, 8, 28, 9, 31), datetime(2026, 8, 28, 13, 1)):
            for index in range(120):
                point = start + timedelta(minutes=index)
                rows.append({
                    "source": "tencent", "bar_end": point.strftime("%Y-%m-%d %H:%M:%S"),
                    "open": 4.2, "high": 4.21, "low": 4.19, "close": 4.2, "volume": 1,
                })
        bars = market_data.build_15m_bars(rows, datetime(2026, 8, 28, 15, 1))
        quality = market_data.minute_completeness(rows, datetime(2026, 8, 28, 15, 1))
        self.assertEqual(len(bars), 16)
        self.assertEqual(bars[-1]["bar_end"], "2026-08-28 15:00:00")
        self.assertEqual(quality["expected_minutes"], 240)
        self.assertEqual(quality["missing_minutes"], 0)
        self.assertEqual(quality["timestamp_convention"], "end")

    def test_older_minute_sequence_cannot_overwrite_newer_cache(self):
        def bar(at, close):
            return [{
                "source": "tencent", "bar_end": at, "open": close, "high": close,
                "low": close, "close": close, "volume": 1,
            }]

        with TemporaryDirectory() as tmp:
            self.assertEqual(market_data.upsert_bars(tmp, "000001", "1m", bar("2026-08-25 10:01:00", 11)), 1)
            self.assertEqual(market_data.upsert_bars(tmp, "000001", "1m", bar("2026-08-25 10:00:00", 9)), 0)
            stored = market_data.load_bars(tmp, "000001", "1m", "tencent")
        self.assertEqual(len(stored), 1)
        self.assertEqual(stored[0]["close"], 11)

    def test_minute_completeness_reports_gap_and_staleness(self):
        rows = []
        start = datetime(2026, 8, 25, 9, 30)
        for index in range(28):
            if index == 10:
                continue
            point = start + timedelta(minutes=index)
            rows.append({"bar_end": point.strftime("%Y-%m-%d %H:%M:%S")})
        quality = market_data.minute_completeness(rows, datetime(2026, 8, 25, 10, 0))
        self.assertEqual(quality["expected_minutes"], 30)
        self.assertEqual(quality["missing_minutes"], 3)
        self.assertTrue(quality["stale"])
        self.assertFalse(quality["entry_ready"])


if __name__ == "__main__":
    unittest.main()
