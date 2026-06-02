import unittest
from unittest.mock import patch

import pandas as pd

from src.strategy import CompositeStrategy


def make_candles(count=70, close=100, high=105, low=95, open_price=100):
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


class StrategyAlignmentTests(unittest.TestCase):
    def test_new_high_breakout_adds_buy_signal(self):
        candles = make_candles(count=70, close=100, high=105)
        strategy = CompositeStrategy(
            {
                "buy_threshold": 1,
                "sell_threshold": 2,
                "indicators": {"new_high": {"enabled": True, "period": 60, "weight": 1}},
            }
        )

        signal = strategy.evaluate(candles, current_price=106)

        self.assertEqual(signal.action, "buy")
        self.assertEqual(signal.score_buy, 1)
        self.assertTrue(any("60일 신고가 돌파" in reason for reason in signal.reasons))

    def test_adx_penalty_blocks_weak_trend_buy(self):
        candles = make_candles(count=70, close=100, high=105, low=95, open_price=100)
        strategy = CompositeStrategy(
            {
                "buy_threshold": 1,
                "sell_threshold": 2,
                "indicators": {
                    "vol_breakout": {"enabled": True, "k": 0.5, "weight": 1},
                    "adx": {"enabled": True, "period": 14, "min": 20, "penalty": 1},
                },
            }
        )

        with patch("src.strategy.ind.adx", return_value=pd.Series([10.0] * len(candles))):
            signal = strategy.evaluate(candles, current_price=106, current_open=100)

        self.assertEqual(signal.action, "hold")
        self.assertEqual(signal.score_buy, 0)
        self.assertEqual(signal.adx, 10.0)
        self.assertTrue(any("추세약함" in reason for reason in signal.reasons))

    def test_regime_filter_holds_buy_below_long_ma(self):
        candles = make_candles(count=70, close=100, high=90, low=80, open_price=85)
        strategy = CompositeStrategy(
            {
                "buy_threshold": 1,
                "sell_threshold": 2,
                "indicators": {
                    "vol_breakout": {"enabled": True, "k": 0.5, "weight": 1},
                    "regime": {"enabled": True, "ma": 60},
                },
            }
        )

        signal = strategy.evaluate(candles, current_price=91, current_open=85)

        self.assertEqual(signal.action, "hold")
        self.assertFalse(signal.trend_ok)
        self.assertTrue(any("레짐 필터" in reason for reason in signal.reasons))

    def test_volatility_breakout_uses_current_open_when_provided(self):
        candles = make_candles(count=70, close=100, high=110, low=100, open_price=200)
        strategy = CompositeStrategy(
            {
                "buy_threshold": 1,
                "sell_threshold": 2,
                "indicators": {"vol_breakout": {"enabled": True, "k": 0.5, "weight": 1}},
            }
        )

        signal = strategy.evaluate(candles, current_price=106, current_open=100)

        self.assertEqual(signal.action, "buy")
        self.assertEqual(signal.vol_target, 105)


if __name__ == "__main__":
    unittest.main()
