import sqlite3
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

import paper_trading
from research import v2_replay_acceptance as replay


class V2ReplayAcceptanceTests(unittest.TestCase):
    def test_intent_marks_preserve_nonfill_opportunity_cost(self):
        with TemporaryDirectory() as tmp:
            con = paper_trading.init_db(tmp)
            con.execute(
                """INSERT INTO paper_orders
                   (order_id,trading_date,created_at,symbol,name,scenario,side,qty,
                    signal_price,limit_price,fill_price,status,reason,signal_fingerprint)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    "o1", "2026-08-26", "2026-08-26 10:00:00", "600000", "测试",
                    "V2_E1_TREND_PULLBACK_RECLAIM", "BUY", 100, 10.0, 10.05,
                    None, "UNFILLED", "滑点后超出", "fp",
                ),
            )
            con.commit()
            count = paper_trading.record_execution_intent_marks(
                con,
                "2026-08-26",
                price_map={"600000": {"last_price": 10.30}},
                now=__import__("datetime").datetime(2026, 8, 26, 10, 5),
            )
            row = con.execute("SELECT order_status,return_from_signal_pct FROM paper_intent_marks").fetchone()
        self.assertEqual(count, 1)
        self.assertEqual(row[0], "UNFILLED")
        self.assertAlmostEqual(row[1], 3.0)

    def test_replay_builds_unique_track_funnel_and_labeled_ablation(self):
        with TemporaryDirectory() as tmp:
            runtime = Path(tmp) / "data" / "runtime"
            runtime.mkdir(parents=True)
            signal_con = sqlite3.connect(runtime / "signal_state.sqlite")
            signal_con.executescript("""
                CREATE TABLE signal_track_events (
                  event_id TEXT, track_id TEXT, trading_date TEXT, symbol TEXT,
                  old_state TEXT, new_state TEXT, event_type TEXT, grade TEXT,
                  score INTEGER, scenario TEXT, price REAL, trigger_distance_pct REAL,
                  distance_bucket TEXT, hard_veto_json TEXT, gaps_json TEXT,
                  data_timestamp TEXT, config_hash TEXT, payload_json TEXT, created_at TEXT
                );
            """)
            rows = [
                ("e1", "t1", "IDLE", "DISCOVERED", "C", '[{"code":"DATA"}]'),
                ("e2", "t1", "DISCOVERED", "NEAR_TRIGGER", "B", "[]"),
                ("e3", "t1", "NEAR_TRIGGER", "TRIGGERED", "A", "[]"),
                ("e4", "t1", "TRIGGERED", "ORDER_PENDING", "A", "[]"),
                ("e5", "t1", "ORDER_PENDING", "FILLED", "A", "[]"),
            ]
            for event_id, track_id, old, new, grade, veto in rows:
                signal_con.execute(
                    "INSERT INTO signal_track_events VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (event_id, track_id, "2026-08-26", "600000", old, new, "state_changed", grade, 85,
                     "V2_E1_TREND_PULLBACK_RECLAIM", 10, 0, "AT_TRIGGER", veto, "[]", "09:59:00", "cfg", "{}", f"2026-08-26 10:0{len(event_id)}:00"),
                )
            signal_con.commit()
            signal_con.close()

            paper_con = sqlite3.connect(runtime / "paper_trading.sqlite")
            paper_con.executescript("""
                CREATE TABLE paper_orders (
                  order_id TEXT,trading_date TEXT,symbol TEXT,scenario TEXT,side TEXT,status TEXT,reason TEXT,created_at TEXT
                );
                CREATE TABLE paper_intent_marks (
                  order_id TEXT,mark_at TEXT,trading_date TEXT,symbol TEXT,order_status TEXT,
                  signal_price REAL,last_price REAL,return_from_signal_pct REAL,source TEXT
                );
            """)
            paper_con.execute("INSERT INTO paper_orders VALUES (?,?,?,?,?,?,?,?)", ("o1", "2026-08-26", "600000", "V2_E1", "BUY", "FILLED", "-", "2026-08-26 10:04:00"))
            paper_con.execute("INSERT INTO paper_intent_marks VALUES (?,?,?,?,?,?,?,?,?)", ("o1", "2026-08-26 10:10:00", "2026-08-26", "600000", "FILLED", 10, 10.2, 2.0, "test"))
            paper_con.commit()
            paper_con.close()

            result = replay.replay(Path(tmp), "2026-08-26", "2026-08-26")
        self.assertEqual(result["tracks"], 1)
        self.assertEqual(result["funnel"]["TRIGGERED"], 1)
        self.assertEqual(result["fills"], 1)
        self.assertEqual(result["admission_only_ablation"]["DATA"], 1)
        self.assertIn("不代表可成交或可盈利", replay.render_markdown(result))


if __name__ == "__main__":
    unittest.main()
