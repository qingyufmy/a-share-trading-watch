import json
import unittest

import premarket_report as report


class PremarketCardSizeTests(unittest.TestCase):
    def test_encoder_keeps_card_below_soft_limit(self):
        card = {
            "msg_type": "interactive",
            "card": {
                "header": {"title": {"tag": "plain_text", "content": "测试"}},
                "elements": [
                    report.lark_text(f"**分区{index}**\n" + "很长的观察池内容" * 6000)
                    for index in range(12)
                ],
            },
        }
        payload = report.encode_lark_card(card)
        self.assertLessEqual(len(payload), report.FEISHU_CARD_MAX_BYTES)
        decoded = json.loads(payload.decode("utf-8"))
        self.assertEqual(len(decoded["card"]["elements"]), 12)
        self.assertIn("完整清单见本地仪表盘", payload.decode("utf-8"))


if __name__ == "__main__":
    unittest.main()
