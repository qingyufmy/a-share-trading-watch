import unittest

from after_close_report import trend_text
from intraday_report import dynamic_levels
from render_report_dashboard import entry_alert_from_stock_row


def daily_row(close=100.0, high=150.0, low=90.0):
    return {
        "close": close,
        "high": high,
        "low": low,
        "amount_wan": 10000.0,
    }


class PriceLimitLevelTests(unittest.TestCase):
    def test_support_and_hard_defense_are_distinct_levels(self):
        quote = {
            "code": "000001",
            "name": "测试股份",
            "close": 100.0,
            "pct": 1.0,
            "amount_wan": 10000.0,
            "high": 101.0,
            "low": 99.0,
        }
        level = trend_text([daily_row(high=105.0) for _ in range(20)], quote)

        self.assertLess(level["defense"], level["support"])
        self.assertEqual(level["add_mode"], "突破确认")
        self.assertEqual(level["add_trigger"], level["pressure"])
        self.assertIn("情绪：", level["add_confirm"])
        self.assertIn("三周期：", level["add_confirm"])

    def test_dashboard_entry_card_preserves_structured_add_conditions(self):
        row = {
            "代码": "000001",
            "名称": "测试股份",
            "状态": "🟢 强趋势延续",
            "优先级": "P2",
            "收盘": "100.00",
            "支撑（回踩观察）": "97.00",
            "防守/止损（硬失效）": "95.00",
            "压力": "105.00",
            "加仓触发": "97.00",
            "加仓模式": "回踩承接",
            "加仓确认": "情绪、筹码、时间和三周期共同确认",
            "加仓取消": "跌破97.00取消",
        }
        alert = entry_alert_from_stock_row(row)

        self.assertIsNotNone(alert)
        self.assertEqual(alert["trigger_price"], "97.00")
        self.assertEqual(alert["invalid_price"], "95.00")
        self.assertEqual(alert["confirm"], "情绪、筹码、时间和三周期共同确认")
        self.assertEqual(alert["action"], "跌破97.00取消")

    def test_after_close_pressure_is_capped_at_next_session_limit(self):
        quote = {
            "code": "000001",
            "name": "测试股份",
            "close": 100.0,
            "pct": 1.0,
            "amount_wan": 10000.0,
            "high": 101.0,
            "low": 99.0,
        }
        level = trend_text([daily_row() for _ in range(20)], quote)

        self.assertEqual(level["trend_pressure"], 150.0)
        self.assertEqual(level["next_limit_up"], 110.0)
        self.assertEqual(level["pressure"], 110.0)
        self.assertEqual(level["pressure_kind"], "当日涨停边界")
        self.assertIn("跨日参考", level["reduce"])
        self.assertIn("不追", level["add"])

    def test_intraday_uses_execution_pressure_for_legacy_plan_levels(self):
        row = {"code": "000001", "pressure": 150.0, "repair": 103.0, "defense": 95.0}
        quote = {
            "code": "000001",
            "name": "测试股份",
            "close": 102.0,
            "prev_close": 100.0,
            "open": 101.0,
            "high": 102.0,
        }
        levels = dynamic_levels(row, quote, {"available": False})

        self.assertEqual(levels["trend_pressure"], 150.0)
        self.assertEqual(levels["limit_up"], 110.0)
        self.assertEqual(levels["execution_pressure"], 110.0)
        self.assertLessEqual(levels["first_bounce"], levels["limit_up"])


if __name__ == "__main__":
    unittest.main()
