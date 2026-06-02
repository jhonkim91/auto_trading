import unittest
from unittest.mock import patch

import pandas as pd

from autotrader import CompositeStrategy as GuiStrategy
from src.config import _DEFAULT_CONFIG
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
                "confirm_threshold": 0,
                "indicators": {
                    "vol_breakout": {"enabled": False},
                    "new_high": {"enabled": True, "period": 60, "weight": 1},
                },
            }
        )

        signal = strategy.evaluate(candles, current_price=106)

        self.assertEqual(signal.action, "buy")
        self.assertEqual(signal.primary_trigger, "new_high")
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
        self.assertEqual(signal.gate_reason, "adx_weak")

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
                "confirm_threshold": 0,
                "indicators": {"vol_breakout": {"enabled": True, "k": 0.5, "weight": 1}},
            }
        )

        signal = strategy.evaluate(candles, current_price=106, current_open=100)

        self.assertEqual(signal.action, "buy")
        self.assertEqual(signal.vol_target, 105)

    def test_no_primary_trigger_gives_hold(self):
        """1차 트리거가 없으면 보조 점수가 높아도 hold를 반환한다."""
        candles = make_candles(70, close=100, high=98, low=95)
        strategy = CompositeStrategy(
            {
                "buy_threshold": 1,
                "confirm_threshold": 0,
                "indicators": {
                    "vol_breakout": {"enabled": False},
                    "new_high": {"enabled": False},
                    "ma_cross": {"enabled": True, "short": 5, "long": 20, "weight": 3},
                    "regime": {"enabled": False},
                    "adx": {"enabled": False},
                },
            }
        )

        signal = strategy.evaluate(candles, current_price=101)

        self.assertEqual(signal.action, "hold")
        self.assertEqual(signal.gate_reason, "no_primary_trigger")

    def test_src_and_autotrader_strategy_parity(self):
        """동일 candles에서 src와 GUI 전략 래퍼가 같은 action을 반환한다."""
        config = _DEFAULT_CONFIG["strategy"]
        candles = make_candles(70)
        module_signal = CompositeStrategy(config).evaluate(candles, current_price=101, current_open=100)
        gui_signal = GuiStrategy(config).evaluate(candles, price=101, current_open=100)

        self.assertEqual(module_signal.action, gui_signal.action)


if __name__ == "__main__":
    unittest.main()
