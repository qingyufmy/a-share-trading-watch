from datetime import datetime, timedelta
import unittest

import realtime_signal_engine as engine


class EngineHealthAlertTests(unittest.TestCase):
    def test_alerts_only_after_persistent_same_error_then_recovers_once(self):
        now = datetime(2026, 9, 3, 9, 31, 0)
        state = {}
        for offset in range(engine.ENGINE_ERROR_ALERT_MIN_CONSECUTIVE - 1):
            action, state = engine.engine_health_alert_transition(
                state, now=now + timedelta(seconds=offset * 5), error="missing pressure"
            )
            self.assertIsNone(action)

        action, state = engine.engine_health_alert_transition(
            state,
            now=now + timedelta(seconds=30),
            error="missing pressure",
        )
        self.assertEqual("error", action)
        self.assertTrue(state["error_alert_sent"])

        action, state = engine.engine_health_alert_transition(
            state,
            now=now + timedelta(seconds=35),
            error="missing pressure",
        )
        self.assertIsNone(action)

        action, state = engine.engine_health_alert_transition(
            state, now=now + timedelta(seconds=40)
        )
        self.assertEqual("recovery", action)

        action, _ = engine.engine_health_alert_transition(
            state, now=now + timedelta(seconds=45)
        )
        self.assertIsNone(action)

    def test_new_error_fingerprint_requires_its_own_persistence_window(self):
        now = datetime(2026, 9, 3, 10, 0, 0)
        state = {
            "trading_date": "2026-09-03",
            "active": True,
            "error_fingerprint": "old",
            "consecutive_errors": 9,
        }
        action, state = engine.engine_health_alert_transition(state, now=now, error="new")
        self.assertIsNone(action)
        self.assertEqual(1, state["consecutive_errors"])


if __name__ == "__main__":
    unittest.main()
