import unittest

from src.risk import RiskManager


class RiskTests(unittest.TestCase):
    def test_trailing_stop_only_exits_profitable_position(self):
        risk = RiskManager(
            {
                "stop_loss_pct": 5.0,
                "take_profit_pct": 10.0,
                "trailing_stop_pct": 4.0,
            }
        )

        self.assertEqual(risk.should_exit(100, 104, peak_price=110), "trailing_stop")
        self.assertEqual(risk.should_exit(100, 98, peak_price=110), "")


if __name__ == "__main__":
    unittest.main()
