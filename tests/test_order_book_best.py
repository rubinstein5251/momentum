"""Top-of-book extraction from CLOB snapshots."""

from __future__ import annotations

import unittest

from utils.order_book_math import (
    best_ask_from_levels, best_bid_from_levels, best_level_fallback,
    sell_limit_for_full_size_from_bids,
)


class OrderBookBestTests(unittest.TestCase):
    def test_best_ask_is_minimum_not_first_row(self):
        asks = [
            {"price": "0.99", "size": "100"},
            {"price": "0.52", "size": "50"},
            {"price": "0.55", "size": "25"},
        ]
        self.assertAlmostEqual(best_ask_from_levels(asks), 0.52)

    def test_best_bid_is_maximum(self):
        bids = [
            {"price": "0.01", "size": "10"},
            {"price": "0.48", "size": "20"},
        ]
        self.assertAlmostEqual(best_bid_from_levels(bids), 0.48)

    def test_missing_depth_is_not_assumed_liquid(self):
        self.assertIsNone(best_level_fallback("YES", 0.94, 5.0, 0.0))
        self.assertIsNone(best_level_fallback("YES", 0.94, 5.0, 4.0))
        self.assertEqual(best_level_fallback("YES", 0.94, 5.0, 5.0), (0.94, 5.0))

    def test_stop_loss_fok_limit_covers_full_bid_depth(self):
        bids = [
            {"price": "0.60", "size": "2"},
            {"price": "0.58", "size": "4"},
            {"price": "0.55", "size": "10"},
        ]
        self.assertAlmostEqual(sell_limit_for_full_size_from_bids(bids, 5), 0.58)
        self.assertIsNone(sell_limit_for_full_size_from_bids(bids, 20))


if __name__ == "__main__":
    unittest.main()
