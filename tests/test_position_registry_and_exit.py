import unittest
from datetime import datetime
from tempfile import TemporaryDirectory

from core import intraday_timing_v2 as timing
from core import position_registry
import paper_trading


class PositionRegistryAndExitTests(unittest.TestCase):
    def test_opening_soft_exit_waits_for_current_day_closed_bars(self):
        bars15 = [
            {"high": 11 - index * 0.04, "low": 10.8 - index * 0.04, "close": 10.9 - index * 0.04, "volume": 1000}
            for index in range(20)
        ]
        levels = {
            "structural_invalidation": 9.0,
            "ma5_15": 9.9,
            "tier2_ma20": 10.1,
            "structural_band_low": None,
        }
        early = timing._exit_state(
            9.8, 10.0, bars15, [], levels, "RALLY_FAILURE", "NORMAL", 500,
            bars5=[], intraday_bars15=[], now=datetime(2026, 8, 27, 9, 30),
            config={"exit_confirmation": {"soft_exit_not_before": "09:35", "min_closed_5m_bars": 1, "min_intraday_closed_15m_bars": 1}},
        )
        mature = timing._exit_state(
            9.8, 10.0, bars15, [], levels, "RALLY_FAILURE", "NORMAL", 500,
            bars5=[bars15[-1]], intraday_bars15=[bars15[-1]], now=datetime(2026, 8, 27, 9, 45),
            config={"exit_confirmation": {"soft_exit_not_before": "09:35", "min_closed_5m_bars": 1, "min_intraday_closed_15m_bars": 1}},
        )
        self.assertEqual(early[0], "NO_ADD")
        self.assertIn("开盘软退出待确认", early[1])
        self.assertEqual(mature[0], "REDUCE")

    def test_registry_preserves_sources_and_t1_locked_quantity(self):
        merged = position_registry.merge_positions(
            {"600000": {"quantity": 500, "sellable": 300, "cost": 10}},
            [{"symbol": "600000", "quantity": 700, "sellable": 600, "avg_cost": 10.2}],
        )["600000"]
        self.assertEqual(merged["quantity"], 700)
        self.assertEqual(merged["sellable"], 600)
        self.assertEqual(merged["locked_t1"], 100)
        self.assertEqual(merged["position_sources"], ["manual", "paper"])
        self.assertEqual(merged["source_inventory"]["manual"]["sellable"], 300)

    def test_degraded_data_blocks_add_but_does_not_hide_hard_exit(self):
        levels = {"structural_invalidation": 9.80}
        action = timing._exit_state(9.70, 9.90, [], [], levels, "DISTRIBUTION_SHOCK", "NORMAL", 500)
        self.assertEqual(action[0], "STRUCTURAL_EXIT")

        protected = timing._exit_state(9.90, 9.95, [], [], levels, "NORMAL", "NORMAL", 500)
        self.assertEqual(protected[0], "NO_ADD")

    def test_t1_lots_release_only_after_trade_date(self):
        with TemporaryDirectory() as tmp:
            con = paper_trading.init_db(tmp)
            con.execute(
                "INSERT INTO paper_positions VALUES (?,?,?,?,?,?,?)",
                ("600000", "测试", 200, 0, 10.0, "paper", "2026-08-26 10:00:00"),
            )
            con.execute(
                "INSERT INTO paper_buy_lots VALUES (?,?,?,?,?)",
                ("order-1", "2026-08-26", "600000", 200, 0),
            )
            con.commit()
            paper_trading.settle_t1_buys(con, "2026-08-26")
            self.assertEqual(paper_trading.get_position(con, "600000")["sellable"], 0)
            paper_trading.settle_t1_buys(con, "2026-08-27")
            self.assertEqual(paper_trading.get_position(con, "600000")["sellable"], 200)


if __name__ == "__main__":
    unittest.main()
