"""Regression checks that external exchange market-data feeds are absent."""

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class ExternalExchangeFeedRemovalTests(unittest.TestCase):
    def test_no_exchange_vendor_references(self):
        for name in ("bot.py", "config.py", "trading_config.json"):
            source = (ROOT / name).read_text(encoding="utf-8").lower()
            self.assertNotIn("bin" + "ance", source, name)

    def test_exchange_signal_module_removed(self):
        self.assertFalse((ROOT / "utils/btc_signal.py").exists())


if __name__ == "__main__":
    unittest.main()
