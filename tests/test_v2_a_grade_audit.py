import sqlite3
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from research import v2_a_grade_audit as audit


class V2AGradeAuditTests(unittest.TestCase):
    def test_audit_opens_databases_read_only_and_counts_funnel(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            runtime = root / "data" / "runtime"
            runtime.mkdir(parents=True)
            signal_db = runtime / "signal_state.sqlite"
            con = sqlite3.connect(signal_db)
            con.executescript("""
                CREATE TABLE signals (
                  trading_date TEXT, symbol TEXT, scenario TEXT, external_status TEXT,
                  internal_state TEXT, current_price REAL, reason_json TEXT, last_update_ts TEXT
                );
                CREATE TABLE signal_events (
                  trading_date TEXT, symbol TEXT, scenario TEXT, event_type TEXT,
                  from_state TEXT, to_state TEXT, price REAL, payload_json TEXT, created_at TEXT
                );
            """)
            con.execute(
                "INSERT INTO signals VALUES (?,?,?,?,?,?,?,?)",
                ("2026-08-25", "002821", "V2_WAIT", "观察", "WAIT_MULTI_PERIOD", 172.23, '["等待回踩"]', "2026-08-25 15:00:00"),
            )
            con.execute(
                "INSERT INTO signal_events VALUES (?,?,?,?,?,?,?,?,?)",
                ("2026-08-25", "002821", "V2_WAIT", "state_change", "IDLE", "WAIT_MULTI_PERIOD", 156.48, "{}", "2026-08-25 09:30:00"),
            )
            con.commit()
            con.close()

            result = audit.build_audit(root, "2026-08-14", "2026-08-26")

            self.assertEqual(result["signals"]["snapshot_count"], 1)
            self.assertEqual(result["signals"]["event_count"], 1)
            self.assertEqual(result["signals"]["transitions"]["IDLE -> WAIT_MULTI_PERIOD"], 1)
            self.assertIn("凯莱英", audit.render_markdown(result))


if __name__ == "__main__":
    unittest.main()
