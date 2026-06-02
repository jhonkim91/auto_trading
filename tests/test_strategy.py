import unittest

from autotrader import CompositeStrategy as GuiCompositeStrategy
from src.strategy import CompositeStrategy


def make_candles(count=70, open_price=200, high=110, low=100, close=100):
    return [
        {
            "date": f"202601{i + 1:02d}",
            "open": open_price,
            "high": high,
            "low": low,
            "close": close,
            "volume": 1000,
        }
        for i in range(count)
    ]


class StrategyTests(unittest.TestCase):
    def test_module_volatility_breakout_uses_current_open(self):
        strategy = CompositeStrategy(
            {
                "buy_threshold": 1,
                "sell_threshold": 2,
                "indicators": {"vol_breakout": {"enabled": True, "k": 0.5, "weight": 1}},
            }
        )

        signal = strategy.evaluate(make_candles(), current_price=106, current_open=100)

        self.assertEqual(signal.action, "buy")
        self.assertEqual(signal.vol_target, 105)

    def test_gui_volatility_breakout_uses_current_open(self):
        strategy = GuiCompositeStrategy(
            {
                "buy_threshold": 1,
                "sell_threshold": 2,
                "indicators": {"vol_breakout": {"enabled": True, "k": 0.5, "weight": 1}},
            }
        )

        signal = strategy.evaluate(make_candles(), price=106, current_open=100)

        self.assertEqual(signal.action, "buy")
        self.assertEqual(signal.vol_target, 105)


if __name__ == "__main__":
    unittest.main()
