"""Momentum-first dynamic 10/10 inventory accumulation (FAK only)."""

from __future__ import annotations

import json
import logging
import time
from typing import TYPE_CHECKING, Optional

from strategies.fok_execution import fak_buy_limit, fok_sell_with_retries
from utils.clob_helpers import clamp_buy_price
from utils.exit_performance import exit_performance_store
from utils.graceful_shutdown import shutdown_requested
from utils.inventory_balance import BuyApproval, log_buy_plan, plan_single_leg_buy
from utils.inventory_cost import InventorySnapshot, matched_combined_average
from utils.inventory_strategy_store import (
    AccumulationPhase,
    inventory_strategy_store,
)
from utils.matched_stop_loss import compute_stop_loss_scope
from utils.momentum_risk import stop_loss_price, stop_loss_triggered
from utils.order_book_math import executable_sell_from_bids_at_min
from utils.stopped_out_store import stopped_out_store
from utils.momentum_inventory_engine import (
    EntryStatus,
    InventoryBuyAction,
    InventoryPhase,
    config_from_worker,
    evaluate_momentum_inventory_tick,
)

if TYPE_CHECKING:
    from bot import MarketWorker

_LOG = logging.getLogger("inventory_pair_accumulation")


class InventoryPairAccumulationStrategy:
    def __init__(self) -> None:
        self._exit_submit_guard: set[str] = set()
        self._position_opened_at: dict[str, float] = {}

    def _key(self, worker: "MarketWorker") -> str:
        return worker.worker_config.key

    def _max_shares_per_side(self, worker: "MarketWorker") -> float:
        wc = worker.worker_config
        return float(
            getattr(wc, "target_shares_per_side", 0)
            or getattr(wc, "max_shares_per_side", 0)
            or wc.max_shares
            or wc.max_position_size
        )

    def _purpose_for_action(self, action: InventoryBuyAction) -> str:
        if action == InventoryBuyAction.MOMENTUM_ENTRY:
            return "momentum_entry"
        if action == InventoryBuyAction.ACCUMULATE_OPPOSITE:
            return "initial_hedge"
        if action == InventoryBuyAction.COST_OPTIMIZE:
            return "inventory_dca"
        if action in (InventoryBuyAction.BALANCE, InventoryBuyAction.ACCUMULATE_PRIMARY):
            return "inventory_rebalance"
        return "pair_accumulation"

    async def _record_buy(
        self,
        worker: "MarketWorker",
        side: str,
        qty: float,
        fill_px: float,
        limit_px: float,
        oid: Optional[str],
        *,
        action: InventoryBuyAction,
    ) -> None:
        if oid and not worker.record_fill_idempotent(oid, side, qty, fill_px):
            return
        worker.inventory.record_buy(side, qty, fill_px)
        worker.log_trade(side, fill_px, "BUY", size=qty, order_id=oid)
        notify = getattr(worker, "notify_confirmed_fill", None)
        if callable(notify):
            notify(side=side, action="BUY", quantity=qty, price=fill_px, order_id=oid)
        cid = worker.canonical_market_id() or ""
        wkey = self._key(worker)
        rec = inventory_strategy_store.cycle_for_market(wkey, cid)
        if action == InventoryBuyAction.MOMENTUM_ENTRY:
            wc = worker.worker_config
            inventory_strategy_store.start_cycle(
                wkey,
                cid,
                first_leg_side=side,
                hedge_delay_seconds=float(wc.initial_hedge_delay_seconds),
                hedge_timeout_seconds=float(
                    getattr(wc, "hedge_pending_timeout_seconds", 0)
                    or wc.initial_hedge_timeout_seconds
                ),
            )
            self._position_opened_at[f"{wkey}:{cid}"] = time.time()
        elif rec and action in (
            InventoryBuyAction.ACCUMULATE_OPPOSITE,
            InventoryBuyAction.ACCUMULATE_PRIMARY,
        ):
            inventory_strategy_store.record_cycle_leg_fill(
                rec,
                leg="hedge",
                side=side,
                qty=qty,
                price=fill_px,
                kind=action.value,
            )
            if worker.inventory.yes_shares > 0 and worker.inventory.no_shares > 0:
                rec.initial_hedge_complete = True
                rec.phase = AccumulationPhase.PAIR_COMPLETED
            inventory_strategy_store.upsert(rec)
        elif rec and action in (InventoryBuyAction.COST_OPTIMIZE, InventoryBuyAction.BALANCE):
            inventory_strategy_store.record_cycle_leg_fill(
                rec,
                leg="dca",
                side=side,
                qty=qty,
                price=fill_px,
                kind=action.value,
            )
            inventory_strategy_store.upsert(rec)

        cfg = config_from_worker(worker.worker_config)
        target = cfg.target_shares_per_side
        if (
            worker.inventory.yes_shares >= target - 1e-9
            and worker.inventory.no_shares >= target - 1e-9
            and rec
        ):
            rec.target_achieved = True
            rec.phase = AccumulationPhase.PAIR_COMPLETED
            inventory_strategy_store.upsert(rec)

        worker.dashboard["entry_fill_price"] = round(fill_px, 4)
        worker.dashboard["strategy_state"] = action.value
        worker.dashboard["last_fak_fill_qty"] = round(qty, 4)
        if action == InventoryBuyAction.MOMENTUM_ENTRY:
            worker.dashboard["bought_side"] = side
            worker.dashboard["initial_entry_side"] = side
        if hasattr(worker, "sync_accounting_dashboard"):
            worker.sync_accounting_dashboard()

    async def _execute_decision(
        self,
        worker: "MarketWorker",
        decision,
        *,
        cid: str,
    ) -> None:
        from bot import OrderState

        if decision.action == InventoryBuyAction.NONE or decision.qty <= 0:
            return
        purpose = self._purpose_for_action(decision.action)
        allow_first = decision.action == InventoryBuyAction.MOMENTUM_ENTRY
        wc = worker.worker_config
        snap = InventorySnapshot.from_ledger(worker.inventory, fee_rate=wc.spread_capture_fee_rate)
        market = worker.active_market.get("question", "") if worker.active_market else ""
        plan = plan_single_leg_buy(
            snap=snap,
            eff=worker.effective_inventory(),
            side=decision.side,
            requested_qty=decision.qty,
            min_order_size=float(wc.min_order_size),
            max_share_imbalance=float(wc.max_share_imbalance),
            max_unmatched_shares=max(
                float(wc.max_unmatched_shares),
                float(wc.gabagool_max_unpaired_shares),
                self._max_shares_per_side(worker),
            ),
            balance_inventory_before_buy=bool(wc.balance_inventory_before_buy),
            allow_unmatched_accumulation=True,
            purpose=purpose,
            allow_first_leg_entry=allow_first,
            max_shares_per_side=self._max_shares_per_side(worker),
        )
        log_buy_plan(plan, market=market, purpose=purpose)
        if plan.approval != BuyApproval.APPROVED or plan.approved_qty <= 0:
            worker.dashboard["entry_block_reason"] = plan.reason
            return
        qty = plan.approved_qty
        limit_px = clamp_buy_price(decision.limit_price)
        async with worker._order_lock:
            if worker.order_state != OrderState.IDLE:
                return
            worker.order_state = OrderState.PENDING
            worker.dashboard["fak_order_status"] = "submitted"
            worker.dashboard["inventory_entry_status"] = EntryStatus.EXECUTING.value
            qty_f, fill_px, oid = await fak_buy_limit(
                worker, decision.side, limit_px, qty, purpose="momentum_inventory",
            )
            worker.order_state = OrderState.IDLE
            worker.dashboard["fak_order_status"] = "filled" if qty_f > 0 else "no_fill"
            if qty_f <= 0:
                worker.dashboard["entry_block_reason"] = f"{decision.action.value}_fak_no_fill"
                worker.dashboard["inventory_entry_status"] = EntryStatus.WAITING.value
                return
            worker.dashboard["inventory_entry_status"] = EntryStatus.FILLED.value
            await self._record_buy(
                worker, decision.side, qty_f, fill_px, limit_px, oid, action=decision.action,
            )
            _LOG.info(
                json.dumps(
                    {
                        "event": "inventory_buy_filled",
                        "market_id": cid,
                        "action": decision.action.value,
                        "side": decision.side,
                        "qty": qty_f,
                        "requested_qty": qty,
                        "fill_px": fill_px,
                        "projected_combined": decision.projected_combined,
                        "ts": time.time(),
                    }
                )
            )

    def _publish_dashboard(self, worker: "MarketWorker", decision) -> None:
        wc = worker.worker_config
        cfg = config_from_worker(wc)
        snap = InventorySnapshot.from_ledger(
            worker.inventory,
            fee_rate=wc.spread_capture_fee_rate,
        )
        combined = matched_combined_average(snap)
        target = cfg.target_shares_per_side
        worker.dashboard["strategy_mode"] = "momentum_inventory_accumulation"
        worker.dashboard["strategy_name"] = "Momentum inventory 5/5"
        worker.dashboard["strategy_label"] = "Momentum-first inventory"
        worker.dashboard["execution_type"] = str(
            getattr(wc, "inventory_execution_type", "FAK") or "FAK"
        ).upper()
        worker.dashboard["pair_avg_price_c"] = round((combined or 0) * 100, 2) if combined else 0.0
        worker.dashboard["matched_combined_cost_c"] = worker.dashboard["pair_avg_price_c"]
        worker.dashboard["yes_shares"] = round(snap.yes_shares, 2)
        worker.dashboard["no_shares"] = round(snap.no_shares, 2)
        worker.dashboard["up_shares"] = worker.dashboard["yes_shares"]
        worker.dashboard["down_shares"] = worker.dashboard["no_shares"]
        worker.dashboard["target_shares_per_side"] = target
        worker.dashboard["remaining_up_shares"] = round(max(0.0, target - snap.yes_shares), 2)
        worker.dashboard["remaining_down_shares"] = round(max(0.0, target - snap.no_shares), 2)
        worker.dashboard["yes_avg_price_c"] = round(snap.yes_avg() * 100, 2) if snap.yes_shares > 0 else 0.0
        worker.dashboard["no_avg_price_c"] = round(snap.no_avg() * 100, 2) if snap.no_shares > 0 else 0.0
        worker.dashboard["yes_spent_usd"] = round(snap.yes_cost, 4)
        worker.dashboard["no_spent_usd"] = round(snap.no_cost, 4)
        worker.dashboard["combined_acquisition_cost_usd"] = round(snap.total_exposure_usd(), 4)
        worker.dashboard["target_reached"] = (
            snap.yes_shares >= target - 1e-9 and snap.no_shares >= target - 1e-9
        )
        worker.dashboard["accumulation_phase"] = decision.phase.value
        worker.dashboard["inventory_next_action"] = decision.action.value
        worker.dashboard["inventory_action_reason"] = decision.reason
        worker.dashboard["hedge_pending_elapsed_s"] = round(decision.hedge_pending_elapsed, 1)
        worker.dashboard["hedge_timeout_reassess"] = decision.hedge_timeout_reassess
        worker.dashboard["target_combined_pair_cost_c"] = round(
            cfg.target_combined_pair_cost * 100, 2,
        )
        if decision.max_opposite_price is not None:
            worker.dashboard["max_opposite_price_c"] = round(
                decision.max_opposite_price * 100, 2,
            )

        min_entry_c = round(cfg.momentum_entry_min_price * 100, 1)
        worker.dashboard["momentum_entry_min_price_c"] = min_entry_c
        mom_side = decision.momentum_side or (
            decision.momentum.momentum_side if decision.momentum else None
        )
        worker.dashboard["momentum_side"] = mom_side or "—"
        worker.dashboard["momentum_confirmed_side"] = worker.dashboard["momentum_side"]
        ask = decision.momentum_side_ask
        if ask <= 0 and mom_side:
            ask = float(worker.best_ask(mom_side))
        worker.dashboard["momentum_side_ask_c"] = round(ask * 100, 2) if ask > 0 else 0.0
        if worker.order_state.name == "PENDING":
            entry_status = EntryStatus.EXECUTING.value
        elif worker.dashboard.get("inventory_entry_status") == EntryStatus.FILLED.value:
            entry_status = EntryStatus.FILLED.value
        else:
            entry_status = decision.entry_status
        worker.dashboard["inventory_entry_status"] = entry_status

        cid = worker.canonical_market_id() or ""
        rec = inventory_strategy_store.cycle_for_market(self._key(worker), cid)
        hedge_fill, dca_fill = inventory_strategy_store.dashboard_fill_buckets(rec)
        worker.dashboard["first_leg_fill"] = hedge_fill
        worker.dashboard["second_leg_fill"] = dca_fill
        worker.dashboard["hedge_fills"] = hedge_fill
        worker.dashboard["dca_fills"] = dca_fill
        if rec:
            worker.dashboard["hedge_deadline_at"] = rec.hedge_deadline_at
            worker.dashboard["first_fill_at"] = rec.first_fill_at

        if combined is not None and snap.matched_pairs > 0:
            gross = snap.matched_pairs - snap.matched_pairs * combined
            worker.dashboard["expected_gross_settlement_surplus"] = round(gross, 4)
            worker.dashboard["expected_net_settlement_surplus"] = round(gross, 4)
        worker.dashboard["unmatched_exposure_shares"] = round(
            max(snap.unpaired_yes, snap.unpaired_no), 2,
        )

        wc = worker.worker_config
        worker.dashboard["stop_loss_pct"] = wc.stop_loss_pct
        worker.dashboard["stop_loss_trigger_pct"] = wc.stop_loss_trigger_pct
        first_side = (rec.first_leg_side if rec else "") or worker.dashboard.get("bought_side") or ""
        first_side = str(first_side).upper()
        if (
            rec
            and not rec.initial_hedge_complete
            and first_side in ("YES", "NO")
            and worker.inventory.shares(first_side) > 0
        ):
            entry = worker.inventory.avg_cost(first_side)
            trigger_pct = float(wc.stop_loss_trigger_pct)
            stop_px = stop_loss_price(entry, trigger_pct)
            worker.dashboard["stop_loss_trigger_price"] = round(stop_px, 4)
            worker.dashboard["stop_loss_trigger_c"] = round(stop_px * 100, 2)
            worker.dashboard["initial_entry_fill_c"] = round(entry * 100, 2)
            bid = float(worker.best_bid(first_side))
            if bid > 0 and stop_loss_triggered(bid, entry, trigger_pct):
                worker.dashboard.setdefault("strategy_state", "stop_loss_triggered")
        wkey = self._key(worker)
        stopped = stopped_out_store.is_stopped_out(wkey, cid) if cid else False
        worker.dashboard["market_stopped_out"] = stopped

    def _unhedged_first_leg_side(self, worker: "MarketWorker", rec, snap: InventorySnapshot) -> Optional[str]:
        from bot import MIN_SHARES

        if rec and rec.initial_hedge_complete:
            return None
        side = str(rec.first_leg_side if rec else "").upper()
        if side not in ("YES", "NO"):
            side = str(worker.dashboard.get("bought_side") or "").upper()
        if side in ("YES", "NO"):
            unpaired = snap.unpaired_yes if side == "YES" else snap.unpaired_no
            if unpaired >= MIN_SHARES - 1e-9:
                return side
        if snap.unpaired_yes >= MIN_SHARES - 1e-9 and snap.unpaired_no < MIN_SHARES - 1e-9:
            return "YES"
        if snap.unpaired_no >= MIN_SHARES - 1e-9 and snap.unpaired_yes < MIN_SHARES - 1e-9:
            return "NO"
        return None

    def _sell_liquidity_ok(
        self, worker: "MarketWorker", side: str, size: float, min_price: float,
    ) -> bool:
        worker.refresh_order_book(side)
        cached = worker._book_cache.get(side)
        if not cached:
            return False
        book = cached[1]
        bids = book.get("bids") if isinstance(book, dict) else getattr(book, "bids", None)
        from bot import MIN_FILL_DELTA

        exe = executable_sell_from_bids_at_min(
            bids, size, min_price, require_full_size=True,
        )
        return bool(exe and exe[0] >= min_price - 1e-9 and exe[1] >= size - MIN_FILL_DELTA)

    async def _record_exit_sell(
        self,
        worker: "MarketWorker",
        side: str,
        qty: float,
        px: float,
        oid: Optional[str],
        *,
        exit_reason: str,
        entry: float,
        target_price: float,
    ) -> None:
        from bot import MIN_SHARES

        if qty <= MIN_SHARES - 1e-9:
            return
        if oid and not worker.record_fill_idempotent(oid, side, qty, px):
            return

        fee_rate = worker.worker_config.estimated_fee_rate
        cost_basis = entry * qty
        proceeds = px * qty
        gross_pnl = proceeds - cost_basis
        exit_fees = fee_rate * proceeds
        entry_fees = fee_rate * cost_basis
        net_pnl = gross_pnl - entry_fees - exit_fees
        fees = entry_fees + exit_fees

        wkey = self._key(worker)
        cid = worker.canonical_market_id() or ""
        opened = self._position_opened_at.get(f"{wkey}:{cid}", time.time())
        holding = max(0.0, time.time() - opened)

        worker.inventory.record_sell(side, qty, px)
        worker.log_trade(side, px, "SELL", size=qty, order_id=oid)
        notify = getattr(worker, "notify_confirmed_fill", None)
        if callable(notify):
            notify(
                side=side,
                action="SELL",
                quantity=qty,
                price=px,
                order_id=oid,
                realized_pnl_delta=net_pnl,
                count_win_loss=True,
            )
        worker.dashboard["exit_order_status"] = "FILLED"
        worker.dashboard["exit_order_type"] = "FOK"
        worker.dashboard["exit_fill_price"] = round(px, 4)
        worker.dashboard["exit_reason"] = "STOP_LOSS" if exit_reason == "stop_loss" else exit_reason.upper()

        remaining = worker.inventory.shares(side)
        if remaining >= MIN_SHARES - 1e-9:
            worker.dashboard["strategy_state"] = "exit_partial_fill"
            worker.dashboard["stop_loss_event"] = "STOP_LOSS_PARTIAL_FILL"
            if hasattr(worker, "sync_accounting_dashboard"):
                worker.sync_accounting_dashboard()
            return

        worker.dashboard["strategy_state"] = "position_closed_stop_loss"
        worker.dashboard["stop_loss_event"] = "STOP_LOSS_FILLED"
        exit_performance_store.record_stop_loss(
            wkey, pnl=net_pnl, fees=fees, holding_sec=holding,
        )
        worker.position_side = None
        worker.position_size = 0.0
        worker.entry_price = 0.0
        worker.entry_count = 0
        self._exit_submit_guard.discard(f"{wkey}:{cid}")
        if hasattr(worker, "sync_accounting_dashboard"):
            worker.sync_accounting_dashboard()

    async def _handle_unhedged_stop_loss(
        self,
        worker: "MarketWorker",
        cid: str,
        rec,
        snap: InventorySnapshot,
    ) -> None:
        from bot import MIN_SHARES, OrderState

        if rec and rec.initial_hedge_complete:
            return

        cfg = worker.worker_config
        side = self._unhedged_first_leg_side(worker, rec, snap)
        if not side:
            return

        scope = compute_stop_loss_scope(
            worker.inventory,
            market_id=cid,
            primary_side=side,
            fee_rate=cfg.estimated_fee_rate,
        )
        worker.dashboard.update(scope.dashboard_fields())
        if not scope.directional_stop_eligible or scope.stop_loss_quantity < MIN_SHARES - 1e-9:
            return

        entry = worker.inventory.avg_cost(side)
        if entry <= 0:
            return
        bid = float(worker.best_bid(side))
        trigger_pct = float(cfg.stop_loss_trigger_pct)
        if bid <= 0 or not stop_loss_triggered(bid, entry, trigger_pct):
            return

        wkey = self._key(worker)
        stopped_out_store.mark_stopped_out(wkey, cid)
        worker.dashboard["market_stopped_out"] = True
        worker.dashboard["strategy_state"] = "stop_loss_triggered"
        worker.dashboard["exit_reason"] = "STOP_LOSS_TRIGGERED"
        worker.dashboard["stop_loss_event"] = "STOP_LOSS_TRIGGERED"

        guard_key = f"{wkey}:{cid}"
        if worker._stop_loss_exit_in_flight or guard_key in self._exit_submit_guard:
            return

        shares = scope.stop_loss_quantity
        stop_limit = stop_loss_price(entry, cfg.stop_loss_pct)
        if not self._sell_liquidity_ok(worker, side, shares, stop_limit):
            worker.dashboard["exit_order_status"] = "WAITING_LIQUIDITY"
            return

        async with worker._stop_loss_lock:
            if worker._stop_loss_exit_in_flight:
                return
            worker._stop_loss_exit_in_flight = True
            self._exit_submit_guard.add(guard_key)
            worker.dashboard["exit_order_status"] = "SUBMITTED"
            worker.dashboard["strategy_state"] = "stop_loss_fok_submitted"
            worker.dashboard["stop_loss_event"] = "STOP_LOSS_ORDER_SUBMITTED"
            try:
                if worker.order_state == OrderState.PENDING or worker._order_lock.locked():
                    self._exit_submit_guard.discard(guard_key)
                    return
                async with worker._order_lock:
                    worker.order_state = OrderState.PENDING
                    qty, px, oid = await fok_sell_with_retries(
                        worker,
                        side,
                        shares,
                        purpose="stop_loss",
                        limit_price=stop_limit,
                        max_retries=cfg.stop_loss_exit_max_retries,
                        retry_delay_ms=cfg.stop_loss_exit_retry_ms,
                    )
                    worker.order_state = OrderState.IDLE
                    remaining_side = worker.inventory.shares(side)
                    if qty > MIN_SHARES - 1e-9 and px > 0:
                        await self._record_exit_sell(
                            worker,
                            side,
                            qty,
                            px,
                            oid,
                            exit_reason="stop_loss",
                            entry=entry,
                            target_price=stop_limit,
                        )
                    if qty <= MIN_SHARES - 1e-9 or px <= 0:
                        worker.dashboard["exit_order_status"] = "FAILED"
                        worker.dashboard["stop_loss_event"] = "STOP_LOSS_ORDER_FAILED"
                    if remaining_side >= MIN_SHARES - 1e-9:
                        worker.dashboard["strategy_state"] = "exit_retry"
                        worker.dashboard["exit_order_status"] = "RETRY"
                        worker.dashboard["entry_block_reason"] = "stop_loss_exit_incomplete"
                        self._exit_submit_guard.discard(guard_key)
                    else:
                        worker.dashboard["entry_block_reason"] = "stopped_out_market"
                        if qty > MIN_SHARES - 1e-9:
                            worker.dashboard["stop_loss_event"] = "POSITION_CLOSED"
            finally:
                worker._stop_loss_exit_in_flight = False

    async def on_market_tick(self, worker: "MarketWorker") -> None:
        if shutdown_requested():
            return
        if not worker.worker_config.inventory_accumulation_enabled:
            worker.dashboard["entry_block_reason"] = "inventory_accumulation_disabled"
            return
        cid = worker.canonical_market_id()
        if not cid or not worker.active_market:
            return
        wkey = self._key(worker)
        if stopped_out_store.is_stopped_out(wkey, cid):
            worker.dashboard["entry_block_reason"] = "stopped_out_market"
            worker.dashboard["market_stopped_out"] = True
            worker.dashboard["strategy_state"] = "market_blocked_after_stop_loss"
            return
        if worker.order_state.name == "PENDING" or worker._order_lock.locked():
            worker.dashboard["inventory_entry_status"] = EntryStatus.EXECUTING.value
            return
        if worker.market_seconds_left() <= worker.worker_config.min_time_before_close_seconds:
            worker.dashboard["entry_block_reason"] = "too_close_to_resolution"
            return

        wc = worker.worker_config
        cfg = config_from_worker(wc)
        now = time.time()
        snap = InventorySnapshot.from_ledger(worker.inventory, fee_rate=wc.spread_capture_fee_rate)
        rec = inventory_strategy_store.cycle_for_market(wkey, cid)
        first_fill_at = rec.first_fill_at if rec else 0.0

        await self._handle_unhedged_stop_loss(worker, cid, rec, snap)
        if worker._stop_loss_exit_in_flight:
            return

        if (
            snap.yes_shares >= cfg.target_shares_per_side - 1e-9
            and snap.no_shares >= cfg.target_shares_per_side - 1e-9
        ):
            idle_decision = evaluate_momentum_inventory_tick(
                snap,
                now=now,
                yes_history=list(worker._ask_history.get("YES", [])),
                no_history=list(worker._ask_history.get("NO", [])),
                yes_exe=worker.executable_buy_partial("YES", wc.min_order_size),
                no_exe=worker.executable_buy_partial("NO", wc.min_order_size),
                cfg=cfg,
                first_fill_at=first_fill_at,
            )
            self._publish_dashboard(worker, idle_decision)
            worker.dashboard["strategy_state"] = InventoryPhase.TARGET_REACHED.value
            return

        size_probe = min(cfg.max_purchase_size, cfg.target_shares_per_side)
        yes_exe = worker.executable_buy_partial("YES", size_probe, require_full_size=False)
        no_exe = worker.executable_buy_partial("NO", size_probe, require_full_size=False)

        yes_hist = list(worker._ask_history.get("YES", []))
        no_hist = list(worker._ask_history.get("NO", []))

        decision = evaluate_momentum_inventory_tick(
            snap,
            now=now,
            yes_history=yes_hist,
            no_history=no_hist,
            yes_exe=yes_exe,
            no_exe=no_exe,
            cfg=cfg,
            first_fill_at=first_fill_at,
        )

        self._publish_dashboard(worker, decision)

        if decision.action != InventoryBuyAction.NONE:
            await self._execute_decision(worker, decision, cid=cid)
            self._publish_dashboard(worker, decision)

    async def on_binance_tick(self, worker: "MarketWorker") -> None:
        await self.on_market_tick(worker)

    async def evaluate(self, worker: "MarketWorker"):
        return None

    async def execute(self, worker: "MarketWorker", decision) -> None:
        return None
