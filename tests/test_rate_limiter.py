import time
import unittest

from src.kis_api import KIS_INTERVAL_PAPER
from src.kis_api import KIS_INTERVAL_REAL
from src.kis_api import KIS_RATE_PAPER
from src.kis_api import KIS_RATE_REAL
from src.kis_api import RateLimiter


class RateLimiterTests(unittest.TestCase):
    def test_shared_kis_rate_policy_constants(self):
        self.assertEqual(KIS_RATE_PAPER, 3)
        self.assertEqual(KIS_RATE_REAL, 15)
        self.assertEqual(KIS_INTERVAL_PAPER, 0.4)
        self.assertEqual(KIS_INTERVAL_REAL, 0.07)

    def test_min_interval_is_applied_between_calls(self):
        limiter = RateLimiter(max_per_sec=10, min_interval=0.01)

        limiter.acquire()
        start = time.perf_counter()
        limiter.acquire()

        self.assertGreaterEqual(time.perf_counter() - start, 0.008)


if __name__ == "__main__":
    unittest.main()
