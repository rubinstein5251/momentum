"""Regression checks for market discovery and worker lifecycle."""
import ast
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]


class MarketDiscoveryLifecycleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = (ROOT / "bot.py").read_text(encoding="utf-8")
        cls.tree = ast.parse(cls.source)
        cls.methods = {}
        for node in ast.walk(cls.tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                cls.methods[node.name] = ast.get_source_segment(cls.source, node) or ""

    def test_worker_start_is_not_a_stub(self):
        body = self.methods["start"]
        self.assertIn("get_candidate_markets", body)
        self.assertIn("fetch_target_market", body)
        self.assertIn("quote_poll_loop", body)
        self.assertIn("price_listener", body)
        self.assertIn("on_market_expired", body)

    def test_market_scan_uses_slug_query_and_timeout(self):
        body = self.methods["get_candidate_markets"]
        self.assertIn('params={"slug": slug}', body)
        self.assertIn("timeout=8", body)
        self.assertIn("current_start, next_start", body)

    def test_market_fetch_validates_token_ids_and_expiry(self):
        body = self.methods["fetch_target_market"]
        self.assertIn("timeout=10", body)
        self.assertIn("clobTokenIds", body)
        self.assertIn("endDate", body)
        self.assertIn("len(token_ids) < 2", body)

    def test_market_completion_does_not_reset_before_settlement(self):
        body = self.methods["start"]
        self.assertIn('!= "settled"', body)
        self.assertIn("WAITING FOR SETTLEMENT", body)
        self.assertIn("await self.reset_state()", body)


if __name__ == "__main__":
    unittest.main()
