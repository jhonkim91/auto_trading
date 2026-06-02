"""백테스트 검증 프레임워크 단위 테스트.

고정 seed 사용으로 결정론적 테스트를 유지한다.
"""
import unittest

import numpy as np
import pandas as pd

from src import costs


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


if __name__ == "__main__":
    unittest.main()
