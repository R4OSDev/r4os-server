import unittest

from r4os_server.common.rate_limit import PerIpRateLimiter


class PerIpRateLimiterTest(unittest.TestCase):
    def test_limit_is_per_ip_rolling_and_dynamic(self) -> None:
        current = [10.0]
        limit = [2]
        limiter = PerIpRateLimiter(lambda: limit[0], lambda: current[0])

        self.assertTrue(limiter.allow("192.0.2.1"))
        self.assertTrue(limiter.allow("192.0.2.1"))
        self.assertFalse(limiter.allow("192.0.2.1"))
        self.assertTrue(limiter.allow("192.0.2.2"))

        current[0] = 11.0
        self.assertTrue(limiter.allow("192.0.2.1"))
        limit[0] = 1
        self.assertFalse(limiter.allow("192.0.2.1"))


if __name__ == "__main__":
    unittest.main()
