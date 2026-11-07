"""Stop-loss while initial opposite hedge is incomplete (inventory accumulation)."""

from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from utils.momentum_inventory import PositionInventory
from utils.momentum_risk import stop_loss_price, stop_loss_triggered


class UnhedgedStopLossEligibilityTests(unittest.TestCase):
    def test_trigger_at_20pct_exit_limit_at_21pct(self):
        entry = 0.80
        self.assertAlmostEqual(stop_loss_price(entry, 0.20), 0.64)
        self.assertAlmostEqual(stop_loss_price(entry, 0.21), 0.632)
        self.assertFalse(stop_loss_triggered(0.65, entry, 0.20))
        self.assertTrue(stop_loss_triggered(0.64, entry, 0.20))


class UnhedgedStopLossExecutionTests(unittest.IsolatedAsyncioTestCase):
    def _worker(self, *, bid: float):
        inv = PositionInventory()
        inv.record_buy("YES", 5.0, 0.80)
        cfg = SimpleNamespace(
            key="btc:5m",
            estimated_fee_rate=0.0,
            stop_loss_pct=0.21,
            stop_loss_trigger_pct=0.20,
            stop_loss_exit_max_retries=1,
            stop_loss_exit_retry_ms=10,
            spread_capture_fee_rate=0.0,
        )
        worker = SimpleNamespace(
            worker_config=cfg,
            inventory=inv,
            dashboard={"bought_side": "YES"},
            _stop_loss_exit_in_flight=False,
            _stop_loss_lock=asyncio.Lock(),
            _order_lock=asyncio.Lock(),
            _book_cache={"YES": (0.0, {"bids": [(bid, 100.0)]})},
            position_side="YES",
            position_size=5.0,
            entry_price=0.80,
            entry_count=1,
        )
        worker.canonical_market_id = lambda: "m1"
        worker.best_bid = lambda s: bid
        worker.refresh_order_book = lambda s: None
        worker.record_fill_idempotent = lambda *a, **k: True
        worker.log_trade = lambda *a, **k: None
        return worker

    async def test_unhedged_stop_sells_and_blocks_market(self):
        from bot import OrderState
        from strategies.inventory_pair_accumulation import InventoryPairAccumulationStrategy
        from utils.inventory_cost import InventorySnapshot
        from utils.inventory_strategy_store import MarketCycleRecord, AccumulationPhase

        trigger_bid = stop_loss_price(0.80, 0.20) - 0.01
        exit_floor = stop_loss_price(0.80, 0.21)
        w = self._worker(bid=max(trigger_bid, exit_floor))
        w.order_state = OrderState.IDLE
        rec = MarketCycleRecord(
            cycle_id="c1",
            worker_key="btc:5m",
            condition_id="m1",
            phase=AccumulationPhase.WAITING_FOR_OPPOSITE_ENTRY,
            first_leg_side="YES",
            first_fill_at=1.0,
            hedge_deadline_at=100.0,
            initial_hedge_complete=False,
            target_achieved=False,
            hedge_retries=0,
        )
        strat = InventoryPairAccumulationStrategy()
        snap = InventorySnapshot.from_ledger(w.inventory, fee_rate=0.0)
        with patch(
            "strategies.inventory_pair_accumulation.stopped_out_store.mark_stopped_out",
        ) as mark_stop, patch(
            "strategies.inventory_pair_accumulation.stopped_out_store.is_stopped_out",
            return_value=False,
        ), patch(
            "strategies.inventory_pair_accumulation.fok_sell_with_retries",
            new_callable=AsyncMock,
            return_value=(5.0, 0.63, "dry-run"),
        ) as mock_sell:
            await strat._handle_unhedged_stop_loss(w, "m1", rec, snap)
            mock_sell.assert_called_once()
            limit = mock_sell.call_args.kwargs.get("limit_price") or mock_sell.call_args[1].get(
                "limit_price",
            )
            if limit is None:
                limit = mock_sell.call_args[0][3] if len(mock_sell.call_args[0]) > 3 else None
            self.assertAlmostEqual(limit, stop_loss_price(0.80, 0.21))
            mark_stop.assert_called_once_with("btc:5m", "m1")

    async def test_hedge_complete_skips_stop(self):
        from bot import OrderState
        from strategies.inventory_pair_accumulation import InventoryPairAccumulationStrategy
        from utils.inventory_cost import InventorySnapshot
        from utils.inventory_strategy_store import MarketCycleRecord, AccumulationPhase

        w = self._worker(bid=0.40)
        w.inventory.record_buy("NO", 5.0, 0.15)
        w.order_state = OrderState.IDLE
        rec = MarketCycleRecord(
            cycle_id="c1",
            worker_key="btc:5m",
            condition_id="m1",
            phase=AccumulationPhase.PAIR_COMPLETED,
            first_leg_side="YES",
            first_fill_at=1.0,
            hedge_deadline_at=100.0,
            initial_hedge_complete=True,
            target_achieved=False,
            hedge_retries=0,
        )
        strat = InventoryPairAccumulationStrategy()
        snap = InventorySnapshot.from_ledger(w.inventory, fee_rate=0.0)
        with patch(
            "strategies.inventory_pair_accumulation.fok_sell_with_retries",
            new_callable=AsyncMock,
        ) as mock_sell:
            await strat._handle_unhedged_stop_loss(w, "m1", rec, snap)
            mock_sell.assert_not_called()


if __name__ == "__main__":
    unittest.main()
