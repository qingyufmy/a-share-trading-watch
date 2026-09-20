import json
import unittest
from copy import deepcopy
from datetime import datetime
from unittest.mock import patch

import realtime_signal_engine as engine
from core import market_breadth_data as data


NOW = datetime(2026, 9, 8, 13, 30)


class BreadthTransportTests(unittest.TestCase):
    def universe(self):
        return {"date": "2026-09-08", "symbols": ["sz000001", "sh600000"],
                "source": "test_verified", "hash": "test", "excluded_count": 0}

    def quotes(self):
        return {symbol[2:]: {"source_symbol": symbol, "code": symbol[2:], "name": "test",
                            "datetime": "20260908133000", "close": 10, "prev_close": 10,
                            "pct": 1 if symbol.startswith("sz") else -1, "amount_wan": 2}
                for symbol in self.universe()["symbols"]}

    def snapshot(self, quotes=None, cache=None):
        quotes = self.quotes() if quotes is None else quotes
        cache = {"breadth_verified_universe": self.universe()} if cache is None else cache
        with patch.object(data, "_read", return_value="fixture"), patch.object(data, "fetch_universe") as universe:
            result = data.fetch_snapshot(cache, NOW, lambda _: quotes)
        return result, universe

    def test_fresh_prices_units_and_scope(self):
        result, universe = self.snapshot()
        universe.assert_not_called()
        self.assertEqual(result["reported_total"], 2)
        self.assertEqual(len(result["rows"]), 2)
        self.assertEqual(result["rows"][0]["f6"], 20000)
        self.assertEqual(result["scope"], "SH_SZ_A")
        self.assertEqual(result["failed_pages"], [])

    def test_missing_symbol_or_wrong_exchange_rejects_batch(self):
        for quotes in ({}, {**self.quotes(), "000001": {**self.quotes()["000001"], "source_symbol": "sh000001"}}):
            result, _ = self.snapshot(quotes)
            self.assertEqual(result["page_errors"], {1: "quote_batch_incomplete"})
            self.assertEqual(result["rows"], [])

    def test_stale_future_previous_day_and_bad_timestamp_excluded(self):
        for stamp in ("20260908132700", "20260908133100", "20260907133000", "invalid"):
            quotes = self.quotes()
            for quote in quotes.values():
                quote["datetime"] = stamp
            result, _ = self.snapshot(quotes)
            self.assertEqual(result["rows"], [])
            self.assertEqual(sum(result["rejected_quotes"].values()), 2)

    def test_nonfinite_invalid_prices_and_amount_excluded(self):
        for key, value in (("pct", float("nan")), ("close", 0), ("prev_close", -1),
                           ("amount_wan", float("inf")), ("amount_wan", -1)):
            quotes = self.quotes()
            for quote in quotes.values():
                quote[key] = value
            result, _ = self.snapshot(quotes)
            self.assertEqual(result["rejected_quotes"]["invalid_values"], 2)

    def test_previous_day_universe_must_refresh_and_cannot_fall_back(self):
        cache = {"breadth_verified_universe": {**self.universe(), "date": "2026-09-07"}}
        with patch.object(data, "fetch_universe", side_effect=ConnectionError("secret URL")) as fetch:
            result = data.fetch_snapshot(cache, NOW, lambda _: self.quotes())
        fetch.assert_called_once()
        self.assertFalse(result["rows"])
        self.assertNotIn("secret", str(result))

    def test_same_day_universe_reused_but_prices_refetched(self):
        cache = {"breadth_verified_universe": self.universe()}
        with patch.object(data, "_read", return_value="fixture") as read:
            for _ in range(2):
                data.fetch_snapshot(cache, NOW, lambda _: self.quotes())
        self.assertEqual(read.call_count, 2)

    def test_universe_count_pages_duplicates_and_scope(self):
        rows = [{"symbol": s, "code": s[2:]} for s in ("bj920001", "sh600000", "sz000001")]
        with patch.object(data, "MIN_UNIVERSE_SIZE", 2), patch.object(data, "_read", side_effect=[
                '"3"', json.dumps(rows), '"3"']):
            universe = data.fetch_universe(NOW, float("inf"))
        self.assertEqual(universe["symbols"], ["sh600000", "sz000001"])
        self.assertEqual(universe["excluded_count"], 1)
        for page, end in ((rows[:2], '"3"'), ([rows[0], rows[0], rows[2]], '"3"'), (rows, '"4"')):
            with patch.object(data, "MIN_UNIVERSE_SIZE", 2), patch.object(data, "_read", side_effect=[
                    '"3"', json.dumps(page), end]):
                with self.assertRaises(ValueError):
                    data.fetch_universe(NOW, float("inf"))

    def test_suspicious_universe_count_rejected_before_prices(self):
        with patch.object(data, "_read", return_value='"100"'):
            with self.assertRaises(ValueError):
                data.fetch_universe(NOW, float("inf"))

    def test_budget_stops_network(self):
        with patch.object(data.time, "monotonic", side_effect=[0, 21, 22]), patch.object(data, "_read") as read:
            result = data.fetch_snapshot({}, NOW, lambda _: {})
        read.assert_not_called()
        self.assertEqual(result["page_errors"], {"universe": "TimeoutError"})


class BreadthFailoverTests(unittest.TestCase):
    def good(self, source="tencent_full_universe"):
        return {"rows": [{"f12": "000001", "f3": 1, "f6": 100}],
                "reported_total": 1, "failed_pages": [], "source": source}

    def bad(self):
        return {"rows": [], "failed_pages": [1], "page_errors": {1: "RemoteDisconnected"}}

    def test_primary_failed_fallback_success_and_ten_minute_cooldown(self):
        cache = {}
        with patch.object(engine, "fetch_market_breadth_snapshot_fast", side_effect=self.bad) as primary, \
                patch.object(data, "fetch_snapshot", side_effect=lambda *args: self.good()) as fallback, \
                patch.object(engine.time, "time", return_value=1000):
            for _ in range(2):
                result = engine.fetch_resilient_market_breadth(cache, NOW)
        self.assertEqual(primary.call_count, 1)
        self.assertEqual(fallback.call_count, 2)
        self.assertEqual(cache["breadth_primary_retry_at"], 1600)
        self.assertEqual(result["primary_failure"]["failed_pages"], [1])

    def test_primary_success_does_not_fetch_fallback(self):
        with patch.object(engine, "fetch_market_breadth_snapshot_fast", return_value=self.good("eastmoney_full_market")), \
                patch.object(data, "fetch_snapshot") as fallback:
            result = engine.fetch_resilient_market_breadth({}, NOW)
        fallback.assert_not_called()
        self.assertEqual(result["source"], "eastmoney_full_market")

    def test_fallback_clock_accounts_for_primary_wait(self):
        with patch.object(engine, "fetch_market_breadth_snapshot_fast", return_value=self.bad()), \
                patch.object(data, "fetch_snapshot", return_value=self.good()) as fallback, \
                patch.object(engine.time, "monotonic", side_effect=[0, 20]):
            engine.fetch_resilient_market_breadth({}, NOW)
        self.assertEqual((fallback.call_args.args[1] - NOW).total_seconds(), 20)

    def test_fallback_failure_bypasses_primary_cooldown(self):
        cache = {"breadth_primary_retry_at": 2000}
        with patch.object(engine.time, "time", return_value=1000), \
                patch.object(data, "fetch_snapshot", return_value=self.bad()), \
                patch.object(engine, "fetch_market_breadth_snapshot_fast", return_value=self.good()) as primary:
            engine.fetch_resilient_market_breadth(cache, NOW)
        primary.assert_called_once()
        self.assertEqual(cache["breadth_primary_retry_at"], 0)

    def test_both_fail_stays_blocked_and_retains_diagnostic_last_good_only(self):
        cache = {"market_breadth_last_good": {"updated_at": "old", "up_ratio": .9}}
        with patch.object(engine, "fetch_market_breadth_snapshot_fast", return_value=self.bad()), \
                patch.object(data, "fetch_snapshot", return_value=self.bad()):
            result = engine.refresh_market_breadth(cache, NOW)
        self.assertFalse(result["coverage_ready"])
        self.assertIsNone(result["up_ratio"])
        self.assertEqual(result["last_good_at"], "old")
        self.assertIn("fallback_failure", result)

    def test_partial_and_stale_fallback_cannot_unlock(self):
        for bad in ({**self.good(), "failed_pages": [2]}, {**self.good(), "reported_total": 5000}):
            with patch.object(engine, "fetch_market_breadth_snapshot_fast", return_value=self.bad()), \
                    patch.object(data, "fetch_snapshot", return_value=bad):
                self.assertFalse(engine.refresh_market_breadth({}, NOW)["coverage_ready"])

    def test_source_change_resets_turnover_and_preserves_industry(self):
        cache = {"market_breadth_samples": [{"amount_yuan": 1000}],
                 "market_stock_profiles": {"000001": {"industry": "bank"}}}
        before = deepcopy(cache["market_stock_profiles"])
        with patch.object(engine, "fetch_resilient_market_breadth", return_value=self.good()), \
                patch.object(engine, "write_market_profile_cache") as write:
            first = engine.refresh_market_breadth(cache, NOW)
            cache["market_breadth_ts"] = 0
            second = engine.refresh_market_breadth(cache, NOW)
        self.assertTrue(first["coverage_ready"])
        self.assertIsNone(first["amount_ratio"])
        self.assertIsNone(second["amount_ratio"])  # Duplicate time is not a rate baseline.
        self.assertEqual(cache["market_stock_profiles"], before)
        write.assert_not_called()


if __name__ == "__main__":
    unittest.main()
