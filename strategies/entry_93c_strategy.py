"""93¢ late-window entry: single leg per market, percentage stop-loss exit (FAK)."""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Optional

from strategies.fok_execution import fak_buy_limit, fok_sell_with_retries
from utils.clob_helpers import clamp_buy_price
from utils.graceful_shutdown import shutdown_requested
from utils.momentum_risk import stop_loss_price, stop_loss_triggered
from utils.order_book_math import executable_sell_from_bids_at_min
from utils.stopped_out_store import stopped_out_store

if TYPE_CHECKING:
    from bot import MarketWorker

_STRATEGY_NAME = "93¢ Entry Strategy"
_IDLE_LOG_INTERVAL_SEC = 8.0


class Entry93cStrategy:
    def __init__(self) -> None:
        self._position_opened_at: dict[str, float] = {}
        self._exit_submit_guard: set[str] = set()
        self._last_idle_log: dict[str, float] = {}

    def _key(self, worker: "MarketWorker") -> str:
        return worker.worker_config.key

    @staticmethod
    def _valid_trade_side(raw) -> Optional[str]:
        side = str(raw or "").upper().strip()
        if side in ("YES", "NO"):
            return side
        return None

    def _position_side(self, worker: "MarketWorker") -> Optional[str]:
        from bot import MIN_SHARES

        inv = worker.inventory
        has_yes = inv.yes_shares >= MIN_SHARES - 1e-9
        has_no = inv.no_shares >= MIN_SHARES - 1e-9
        if has_yes and has_no:
            bought = self._valid_trade_side(
                worker.dashboard.get("bought_side") or getattr(worker, "position_side", None)
            )
            if bought:
                return bought
            return "YES" if inv.yes_shares >= inv.no_shares else "NO"
        if has_yes:
            return "YES"
        if has_no:
            return "NO"
        return None

    def _entry_threshold(self, worker: "MarketWorker") -> float:
        from utils.entry_limit import resolve_minimum_entry_price

        return resolve_minimum_entry_price(worker.worker_config.entry_price).minimum_price

    def _publish_dashboard(self, worker: "MarketWorker") -> None:
        cfg = worker.worker_config
        cid = worker.canonical_market_id() or ""
        wkey = self._key(worker)
        stopped = stopped_out_store.is_stopped_out(wkey, cid) if cid else False
        side = self._position_side(worker)
        inv = worker.inventory
        threshold = self._entry_threshold(worker)
        left = worker.market_seconds_left()

        worker.dashboard["strategy_name"] = _STRATEGY_NAME
        worker.dashboard["strategy_label"] = _STRATEGY_NAME
        worker.dashboard["entry_threshold"] = threshold
        worker.dashboard["entry_threshold_c"] = round(threshold * 100, 2)
        worker.dashboard["entry_window_seconds"] = cfg.entry_window_seconds
        worker.dashboard["stop_loss_pct"] = cfg.stop_loss_pct
        worker.dashboard["stop_loss_percent"] = round(cfg.stop_loss_pct * 100, 1)
        worker.dashboard["execution_type"] = str(cfg.order_type or "FAK").upper()
        worker.dashboard["market_id"] = cid
        worker.dashboard["market_stopped_out"] = stopped
        worker.dashboard["position_side"] = side or ""
        worker.dashboard["seconds_remaining"] = left
        worker.dashboard["inventory_accumulation_enabled"] = False

        yes_ask = float(worker.best_ask("YES"))
        no_ask = float(worker.best_ask("NO"))
        worker.dashboard["yes_ask"] = round(yes_ask, 4)
        worker.dashboard["no_ask"] = round(no_ask, 4)
        worker.dashboard["yes_ask_c"] = round(yes_ask * 100, 2)
        worker.dashboard["no_ask_c"] = round(no_ask * 100, 2)

        if side:
            entry = inv.avg_cost(side)
            worker.dashboard["entry_fill_price"] = round(entry, 4)
            worker.dashboard["entry_fill_price_c"] = round(entry * 100, 2)
            worker.dashboard["position_size"] = round(inv.shares(side), 4)
            bid = float(worker.best_bid(side))
            worker.dashboard["executable_bid"] = round(bid, 4)
            worker.dashboard["executable_bid_c"] = round(bid * 100, 2) if bid > 0 else 0
            stop_px = stop_loss_price(entry, cfg.stop_loss_pct)
            worker.dashboard["stop_loss_trigger_price"] = round(stop_px, 4)
            worker.dashboard["stop_loss_trigger_c"] = round(stop_px * 100, 2)
            if bid > 0 and stop_loss_triggered(bid, entry, cfg.stop_loss_pct):
                worker.dashboard.setdefault("strategy_state", "stop_loss_triggered")
            else:
                worker.dashboard.setdefault("strategy_state", "position_open")
        else:
            worker.dashboard["entry_fill_price"] = 0.0
            worker.dashboard["entry_fill_price_c"] = 0.0
            worker.dashboard["position_size"] = 0.0
            worker.dashboard["stop_loss_trigger_price"] = 0.0
            worker.dashboard["stop_loss_trigger_c"] = 0.0
            if stopped:
                worker.dashboard["strategy_state"] = "market_blocked_after_stop_loss"
            elif worker.in_late_entry_window():
                worker.dashboard.setdefault("strategy_state", "entry_eligible")
            else:
                worker.dashboard.setdefault("strategy_state", "idle")

        if hasattr(worker, "sync_accounting_dashboard"):
            worker.sync_accounting_dashboard()

    def _market_ready(self, worker: "MarketWorker") -> Optional[str]:
        from bot import is_locked_price

        if getattr(worker, "_ws_market_data_stale", False):
            return "stale_orderbook"
        if not worker.active_market or not worker.canonical_market_id():
            return "no_active_market"
        if worker.market_seconds_left() <= 0:
            return "market_closed"
        cfg = worker.worker_config
        age = worker.max_quote_age_seconds()
        if age is not None and age > cfg.max_quote_age_seconds:
            return "stale_orderbook"
        for side in ("YES", "NO"):
            ask = float(worker.best_ask(side))
            if ask > 0 and is_locked_price(ask):
                return "locked_outcome_price"
        return None

    def _pick_qualifying_side(
        self, worker: "MarketWorker", size: float, threshold: float,
    ) -> Optional[tuple[str, float, float]]:
        best: Optional[tuple[str, float, float]] = None
        for side in ("YES", "NO"):
            ask = float(worker.best_ask(side))
            if ask < threshold - 1e-9:
                continue
            exe = worker.executable_buy(side, float(size))
            if not exe or exe[0] < threshold - 1e-9:
                continue
            if best is None or exe[0] > best[1]:
                best = (side, float(exe[0]), float(exe[1]))
        return best

    async def _record_buy(
        self,
        worker: "MarketWorker",
        side: str,
        qty: float,
        fill_px: float,
        limit_px: float,
        oid: Optional[str],
    ) -> None:
        if oid and not worker.record_fill_idempotent(oid, side, qty, fill_px):
            return
        worker.inventory.record_buy(side, qty, fill_px)
        worker.entry_count = 1
        worker.position_side = side
        worker.position_size = qty
        worker.entry_price = fill_px
        from bot import TradeState

        worker.trade_state = TradeState.FILLED
        worker.log_trade(side, fill_px, "BUY", size=qty, order_id=oid)
        notify = getattr(worker, "notify_confirmed_fill", None)
        if callable(notify):
            notify(side=side, action="BUY", quantity=qty, price=fill_px, order_id=oid)
        cid = worker.canonical_market_id() or ""
        wkey = self._key(worker)
        self._position_opened_at[f"{wkey}:{cid}"] = time.time()
        worker.dashboard["buy_limit_price"] = limit_px
        worker.dashboard["buy_limit_price_c"] = round(limit_px * 100, 2)
        worker.dashboard["entry_fill_price"] = round(fill_px, 4)
        worker.dashboard["entry_fill_price_c"] = round(fill_px * 100, 2)
        worker.dashboard["filled_size"] = round(qty, 4)
        worker.dashboard["entry_order_status"] = "FILLED"
        worker.dashboard["strategy_state"] = "position_open"
        worker.dashboard["bought_side"] = side
        label = f"{worker.asset_type.upper()} {worker.window_slug}"
        print(
            f"[93C ENTRY] {label} | BUY {side} | Fill={fill_px:.4f} | Shares={qty:.0f}"
        )

    async def _record_exit_sell(
        self,
        worker: "MarketWorker",
        side: str,
        qty: float,
        px: float,
        oid: Optional[str],
        *,
        entry: float,
        target_price: float,
    ) -> None:
        from config import MIN_SHARES

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
        worker.dashboard["exit_order_type"] = str(worker.worker_config.order_type or "FAK").upper()
        worker.dashboard["exit_fill_price"] = round(px, 4)
        worker.dashboard["exit_fill_price_c"] = round(px * 100, 2)
        worker.dashboard["exit_avg_fill_price"] = round(px, 4)
        worker.dashboard["exit_target_price"] = round(target_price, 4)
        worker.dashboard["exit_gross_pnl"] = round(gross_pnl, 4)
        worker.dashboard["exit_fees"] = round(fees, 4)
        worker.dashboard["exit_net_pnl"] = round(net_pnl, 4)
        worker.dashboard["closed_quantity"] = round(qty, 4)
        worker.dashboard["entry_avg_fill_price"] = round(entry, 4)

        remaining = worker.inventory.shares(side)
        if remaining >= MIN_SHARES - 1e-9:
            worker.position_size = remaining
            worker.dashboard["position_size"] = round(remaining, 4)
            worker.dashboard["strategy_state"] = "exit_retry"
            worker.dashboard["exit_reason"] = "STOP_LOSS"
            worker.dashboard["stop_loss_event"] = "STOP_LOSS_PARTIAL_FILL"
            log_pnl = getattr(worker, "log_pnl", None)
            if callable(log_pnl):
                log_pnl(
                    "STOP_LOSS",
                    net_pnl,
                    {
                        "order_id": oid,
                        "side": side,
                        "quantity": qty,
                        "exit_price": px,
                        "partial": True,
                    },
                )
            if hasattr(worker, "sync_accounting_dashboard"):
                worker.sync_accounting_dashboard()
            return

        worker.dashboard["exit_reason"] = "STOP_LOSS"
        worker.dashboard["strategy_state"] = "position_closed_stop_loss"
        worker.dashboard["stop_loss_event"] = "STOP_LOSS_FILLED"
        log_pnl = getattr(worker, "log_pnl", None)
        if callable(log_pnl):
            log_pnl(
                "STOP_LOSS",
                net_pnl,
                {"order_id": oid, "side": side, "quantity": qty, "exit_price": px, "partial": False},
            )
        if hasattr(worker, "sync_accounting_dashboard"):
            worker.sync_accounting_dashboard()
        worker.position_side = None
        worker.position_size = 0.0
        worker.entry_price = 0.0
        worker.entry_count = 0
        self._exit_submit_guard.discard(f"{wkey}:{cid}")

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
            bids, size, min_price, require_full_size=False,
        )
        return bool(exe and exe[0] >= min_price - 1e-9 and exe[1] > MIN_FILL_DELTA)

    async def _handle_stop_loss(self, worker: "MarketWorker", cid: str) -> None:
        from bot import MIN_SHARES, OrderState

        cfg = worker.worker_config
        side = self._position_side(worker)
        if not side:
            return
        inv = worker.inventory
        shares = inv.shares(side)
        if shares < MIN_SHARES - 1e-9:
            return
        entry = inv.avg_cost(side)
        bid = float(worker.best_bid(side))
        stop_limit = stop_loss_price(entry, cfg.stop_loss_pct)
        if bid <= 0 or not stop_loss_triggered(bid, entry, cfg.stop_loss_pct):
            return

        wkey = self._key(worker)
        label = f"{worker.asset_type.upper()} {worker.window_slug}"
        print(
            f"[STOP LOSS] {label} | {side} | Entry={entry:.4f} | "
            f"Trigger={stop_limit:.4f} | Current bid={bid:.4f}"
        )
        stopped_out_store.mark_stopped_out(wkey, cid)
        worker.dashboard["market_stopped_out"] = True
        worker.dashboard["strategy_state"] = "stop_loss_triggered"
        worker.dashboard["exit_reason"] = "STOP_LOSS_TRIGGERED"
        worker.dashboard["stop_loss_event"] = "STOP_LOSS_TRIGGERED"
        print(f"[STOPPED OUT] {label} | Market locked against further entries")

        guard_key = f"{wkey}:{cid}"
        if worker._stop_loss_exit_in_flight or guard_key in self._exit_submit_guard:
            return

        async with worker._stop_loss_lock:
            if worker._stop_loss_exit_in_flight:
                return
            worker._stop_loss_exit_in_flight = True
            self._exit_submit_guard.add(guard_key)
            worker.dashboard["exit_order_status"] = "SUBMITTED"
            worker.dashboard["exit_order_type"] = str(cfg.order_type or "FAK").upper()
            worker.dashboard["strategy_state"] = "stop_loss_fak_submitted"
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
                    remaining_after = inv.shares(side)
                    if qty > MIN_SHARES - 1e-9 and px > 0:
                        await self._record_exit_sell(
                            worker,
                            side,
                            qty,
                            px,
                            oid,
                            entry=entry,
                            target_price=stop_limit,
                        )
                    if remaining_after >= MIN_SHARES - 1e-9:
                        worker.dashboard["strategy_state"] = "exit_retry"
                        worker.dashboard["exit_order_status"] = "RETRY"
                        self._exit_submit_guard.discard(guard_key)
                    elif qty <= MIN_SHARES - 1e-9 or px <= 0:
                        worker.dashboard["exit_order_status"] = "FAILED"
                        worker.dashboard["stop_loss_event"] = "STOP_LOSS_ORDER_FAILED"
                        self._exit_submit_guard.discard(guard_key)
            finally:
                worker._stop_loss_exit_in_flight = False

    async def _try_entry(self, worker: "MarketWorker", cid: str) -> None:
        from bot import MIN_SHARES, OrderState, is_trading_allowed

        if shutdown_requested():
            return

        cfg = worker.worker_config
        wkey = self._key(worker)
        label = f"{worker.asset_type.upper()} {worker.window_slug}"

        if stopped_out_store.is_stopped_out(wkey, cid):
            worker.dashboard["strategy_state"] = "market_blocked_after_stop_loss"
            worker.dashboard["entry_block_reason"] = "stopped_out_market"
            worker.dashboard["market_stopped_out"] = True
            return

        if self._position_side(worker) or worker.entry_count >= 1:
            return
        if cid in worker._market_entry_guard:
            return

        block = self._market_ready(worker)
        if block:
            worker.dashboard["entry_block_reason"] = block
            return

        if not worker.in_late_entry_window():
            worker.dashboard["entry_block_reason"] = "outside_entry_window"
            left = worker.market_seconds_left()
            last = self._last_idle_log.get(cid, 0.0)
            if time.time() - last >= _IDLE_LOG_INTERVAL_SEC:
                self._last_idle_log[cid] = time.time()
                print(
                    f"[93C STRATEGY IDLE] {label} | No qualifying entry | "
                    f"Time remaining={left}s"
                )
            return

        if not is_trading_allowed():
            worker.dashboard["entry_block_reason"] = "trading_schedule_blocked"
            return
        if not cfg.live_trading_enabled and not worker.is_dry_run():
            worker.dashboard["entry_block_reason"] = "live_trading_disabled_use_dry_run"
            return

        size = worker.entry_order_size(["YES", "NO"])
        if not size or size < cfg.min_order_size:
            worker.dashboard["entry_block_reason"] = "invalid_order_size"
            return

        threshold = self._entry_threshold(worker)
        pick = self._pick_qualifying_side(worker, size, threshold)
        yes_ask = float(worker.best_ask("YES"))
        no_ask = float(worker.best_ask("NO"))
        left = worker.market_seconds_left()

        if not pick:
            worker.dashboard["entry_block_reason"] = "below_entry_threshold"
            last = self._last_idle_log.get(f"{cid}:scan", 0.0)
            if time.time() - last >= _IDLE_LOG_INTERVAL_SEC:
                self._last_idle_log[f"{cid}:scan"] = time.time()
                print(
                    f"[93C STRATEGY] {label} | UP ask={yes_ask:.4f} DOWN ask={no_ask:.4f} | "
                    f"Entry threshold={threshold:.4f} | Time remaining={left}s"
                )
            return

        side, exe_px, _ = pick
        limit_px = clamp_buy_price(exe_px)
        worker.dashboard["strategy_state"] = "entry_eligible"
        print(
            f"[93C STRATEGY] {label} | {side} ask={exe_px:.4f} | "
            f"Entry threshold={threshold:.4f} | Time remaining={left}s"
        )

        worker._market_entry_guard.add(cid)
        try:
            async with worker._order_lock:
                if stopped_out_store.is_stopped_out(wkey, cid):
                    return
                if worker.order_state != OrderState.IDLE:
                    return
                if self._position_side(worker):
                    return

                worker.order_state = OrderState.PENDING
                worker.dashboard["entry_order_status"] = "SUBMITTED"
                qty, fill_px, oid = await fak_buy_limit(
                    worker, side, limit_px, float(size), purpose="93c_entry",
                )
                worker.order_state = OrderState.IDLE
                if qty < MIN_SHARES - 1e-9:
                    worker.dashboard["entry_order_status"] = "FAILED"
                    worker.dashboard["entry_block_reason"] = "entry_fak_failed"
                    return
                await self._record_buy(worker, side, qty, fill_px, limit_px, oid)
        finally:
            worker._market_entry_guard.discard(cid)

    async def on_market_tick(self, worker: "MarketWorker") -> None:
        if shutdown_requested():
            return
        self._publish_dashboard(worker)
        cid = worker.canonical_market_id()
        if not cid:
            return

        wkey = self._key(worker)
        await self._handle_stop_loss(worker, cid)

        if self._position_side(worker) or worker._stop_loss_exit_in_flight:
            self._publish_dashboard(worker)
            return

        if stopped_out_store.is_stopped_out(wkey, cid):
            worker.dashboard["entry_block_reason"] = "stopped_out_market"
            worker.dashboard["strategy_state"] = "market_blocked_after_stop_loss"
            return

        if worker.order_state.name == "PENDING" or worker._order_lock.locked():
            return

        await self._try_entry(worker, cid)
        self._publish_dashboard(worker)

    async def on_binance_tick(self, worker: "MarketWorker") -> None:
        await self.on_market_tick(worker)

    async def evaluate(self, worker: "MarketWorker"):
        return None

    async def execute(self, worker: "MarketWorker", decision) -> None:
        return None
