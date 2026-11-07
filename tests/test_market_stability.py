import unittest

from utils.market_stability import evaluate_market_stability


class MarketStabilityTests(unittest.TestCase):
    def history(self, start=100.0, end=110.0, first=100.0, last=100.02):
        return [(start, first), (start + 2, first + 0.005), (start + 5, (first + last) / 2), (end, last)]

    def test_stable_fresh_market_passes(self):
        result = evaluate_market_stability(
            self.history(), now=110.0,
            stabilization_window_seconds=10,
            stabilization_tolerance_percent=0.05,
            short_window_seconds=5,
            max_short_window_move_percent=0.15,
        )
        self.assertTrue(result.allowed)
        self.assertEqual(result.reason, "stable")

    def test_wide_range_is_blocked(self):
        history = [(100, 100.0), (103, 100.2), (106, 100.3), (110, 100.4)]
        result = evaluate_market_stability(
            history, now=110,
            stabilization_window_seconds=10,
            stabilization_tolerance_percent=0.05,
            short_window_seconds=5,
            max_short_window_move_percent=0.15,
        )
        self.assertFalse(result.allowed)
        self.assertEqual(result.reason, "underlying_market_unstable")

    def test_sudden_move_is_blocked(self):
        history = [(100, 100.0), (103, 100.0), (105.5, 100.0), (108, 100.2), (110, 100.2)]
        result = evaluate_market_stability(
            history, now=110,
            stabilization_window_seconds=10,
            stabilization_tolerance_percent=0.5,
            short_window_seconds=5,
            max_short_window_move_percent=0.15,
        )
        self.assertFalse(result.allowed)
        self.assertEqual(result.reason, "sudden_underlying_price_move")

    def test_stale_or_missing_data_fails_closed(self):
        result = evaluate_market_stability(
            [(100, 100.0), (101, 100.01)], now=110,
            stabilization_window_seconds=10,
            stabilization_tolerance_percent=0.05,
            short_window_seconds=5,
            max_short_window_move_percent=0.15,
        )
        self.assertFalse(result.allowed)


if __name__ == "__main__":
    unittest.main()
