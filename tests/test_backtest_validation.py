"""백테스트 검증 프레임워크 단위 테스트.

고정 seed 사용으로 결정론적 테스트를 유지한다.
"""
import unittest

import numpy as np
import pandas as pd

from src import costs, metrics, slippage


class CostTests(unittest.TestCase):
    def test_kosdaq_buy_has_no_tax(self):
        result = costs.compute_trade_cost(10000, 10, "buy", "kosdaq")

        self.assertEqual(result.tax, 0.0)
        self.assertGreater(result.commission, 0)

    def test_kosdaq_sell_has_tax(self):
        result = costs.compute_trade_cost(10000, 10, "sell", "kosdaq")

        self.assertAlmostEqual(result.tax, 10000 * 10 * 0.002, places=4)

    def test_kospi_sell_same_tax_as_kosdaq_2026(self):
        kospi = costs.compute_trade_cost(10000, 10, "sell", "kospi")
        kosdaq = costs.compute_trade_cost(10000, 10, "sell", "kosdaq")

        self.assertAlmostEqual(kospi.tax, kosdaq.tax, places=4)

    def test_overseas_no_tax_no_exchange_fee(self):
        result = costs.compute_trade_cost(100, 1, "sell", "overseas")

        self.assertEqual(result.tax, 0.0)
        self.assertEqual(result.exchange_fee, 0.0)
        self.assertGreater(result.commission, 0)

    def test_roundtrip_kosdaq_approx_0_24pct(self):
        """왕복 비용은 세금 0.20%와 매수/매도 수수료 및 유관비용을 포함한다."""
        pct = costs.roundtrip_cost_pct(10000, "kosdaq", slippage_pct_one_way=0)

        self.assertGreater(pct, 0.22)
        self.assertLess(pct, 0.26)


class MetricsTests(unittest.TestCase):
    def _daily_returns(self, annual_return=0.10, n=252, seed=42):
        np.random.seed(seed)
        return np.random.normal(annual_return / 252, 0.01, n)

    def test_sharpe_positive_strategy(self):
        returns = self._daily_returns(0.20)
        sharpe = metrics.sharpe(returns, rf=0.025)

        self.assertGreater(sharpe, 0)

    def test_psr_high_for_good_strategy(self):
        returns = self._daily_returns(0.30, n=500)
        psr = metrics.psr(returns)

        self.assertGreater(psr, 0.90)

    def test_psr_low_for_poor_strategy(self):
        np.random.seed(1)
        returns = np.random.normal(-0.001, 0.01, 50)
        psr = metrics.psr(returns)

        self.assertLess(psr, 0.50)

    def test_omega_above_one_for_positive(self):
        returns = self._daily_returns(0.15)

        self.assertGreater(metrics.omega(returns), 1.0)

    def test_mdd_negative(self):
        equity = np.cumprod(1 + self._daily_returns(0.10))

        self.assertLess(metrics.max_drawdown(equity), 0)

    def test_dsr_requires_multiple_trials(self):
        returns = self._daily_returns(0.20, n=252)
        trials = [0.05, 0.08, 0.12, 0.15]
        dsr = metrics.deflated_sharpe(returns, trials)

        self.assertGreater(dsr, 0)
        self.assertLessEqual(dsr, 1.0)


class SlippageTests(unittest.TestCase):
    def _make_df(self, n=30, seed=1):
        np.random.seed(seed)
        close = 10000 * np.cumprod(1 + np.random.normal(0, 0.01, n))
        high = close * (1 + np.abs(np.random.normal(0, 0.005, n)))
        low = close * (1 - np.abs(np.random.normal(0, 0.005, n)))
        return pd.DataFrame({"open": close * 0.999, "high": high, "low": low, "close": close})

    def test_cs_spread_non_negative(self):
        frame = self._make_df()
        spread = slippage.corwin_schultz_spread(frame)

        self.assertTrue((spread.dropna() >= 0).all())

    def test_one_way_slippage_above_min(self):
        frame = self._make_df()
        estimated = slippage.one_way_slippage_pct(frame, min_bps=5.0)

        self.assertTrue((estimated.dropna() >= 5.0 / 10000).all())

    def test_estimate_from_candles_returns_float(self):
        candles = self._make_df().to_dict("records")
        value = slippage.estimate_from_candles(candles, min_bps=5.0)

        self.assertIsInstance(value, float)
        self.assertGreater(value, 0)


if __name__ == "__main__":
    unittest.main()
