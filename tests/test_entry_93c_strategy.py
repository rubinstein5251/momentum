"""93¢ entry strategy: config, entry window, stop loss from avg fill, stopped-out persistence."""

from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from config import load_worker_configs
from strategies.entry_93c_strategy import Entry93cStrategy
from utils.momentum_inventory import PositionInventory
from utils.momentum_risk import stop_loss_price
from utils.stopped_out_store import StoppedOutStore


class ConfigDefaultsTests(unittest.TestCase):
    def test_defaults_from_trading_config(self):
        wc = load_worker_configs()[0]
        self.assertAlmostEqual(wc.entry_price, 0.93)
        self.assertEqual(wc.entry_window_seconds, 90)
        self.assertAlmostEqual(wc.stop_loss_pct, 0.39)
        self.assertFalse(wc.inventory_accumulation_enabled)


class StopLossFormulaTests(unittest.TestCase):
    def test_stop_from_actual_entry(self):
        entry = 0.93
        self.assertAlmostEqual(stop_loss_price(entry, 0.39), 0.5673, places=4)


class StoppedOutStoreTests(unittest.TestCase):
    def test_persist_across_store_instance(self):
        import tempfile
        import os

        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        try:
            a = StoppedOutStore(path)
            b = StoppedOutStore(path)
            a.mark_stopped_out("btc:5m", "cond-1")
            self.assertTrue(b.is_stopped_out("btc:5m", "cond-1"))
            a.close()
            b.close()
        finally:
            os.unlink(path)


class EntryWindowTests(unittest.IsolatedAsyncioTestCase):
    async def test_no_entry_outside_final_window(self):
        inv = PositionInventory()
        cfg = SimpleNamespace(
            key="btc:5m",
            entry_price=0.93,
            entry_window_seconds=90,
            stop_loss_pct=0.39,
            live_trading_enabled=False,
            min_order_size=5.0,
            order_size_max=5.0,
            order_type="FAK",
            max_quote_age_seconds=3.0,
            estimated_fee_rate=0.0,
        )
        worker = SimpleNamespace(
            worker_config=cfg,
            inventory=inv,
            dashboard={},
            order_state=type("S", (), {"name": "IDLE"})(),
            _order_lock=asyncio.Lock(),
            _stop_loss_lock=asyncio.Lock(),
            _stop_loss_exit_in_flight=False,
            _market_entry_guard=set(),
            entry_count=0,
            asset_type="btc",
            window_slug="5m",
            active_market={"condition_id": "m1"},
            _ws_market_data_stale=False,
            prices={"YES": 0.95, "NO": 0.05},
            bids={"YES": 0.94, "NO": 0.04},
            _book_cache={},
        )
        worker.canonical_market_id = lambda: "m1"
        worker.market_seconds_left = lambda: 120
        worker.in_late_entry_window = lambda: False
        worker.best_ask = lambda s: worker.prices[s]
        worker.best_bid = lambda s: worker.bids[s]
        worker.max_quote_age_seconds = lambda: 0.0
        worker.entry_order_size = lambda legs: 5.0
        worker.is_dry_run = lambda: True
        worker.record_fill_idempotent = lambda *a, **k: True
        worker.log_trade = lambda *a, **k: None

        strat = Entry93cStrategy()
        with patch(
            "strategies.entry_93c_strategy.fak_buy_limit",
            new_callable=AsyncMock,
        ) as mock_buy:
            await strat._try_entry(worker, "m1")
            mock_buy.assert_not_called()


if __name__ == "__main__":
    unittest.main()
