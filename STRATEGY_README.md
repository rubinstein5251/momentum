# 93¢ Directional Entry Strategy

This project keeps the existing FastAPI dashboard, account service, market discovery,
Polymarket CLOB client, WebSocket market listener and per-asset worker framework.
The active strategy is a single-position directional strategy.

## Trading rules

- Buy either UP (internally YES) or DOWN (internally NO).
- Entry threshold defaults to 0.93 and is configurable with `entry_price` in
  `trading_config.json` or `ENTRY_PRICE` in `.env`.
- Entries are allowed only during the final 90 seconds by default. Configure with
  `entry_window_seconds` or `ENTRY_WINDOW_SECONDS`.
- The market-data WebSocket starts 5 seconds before the entry window by default
  (`listener_activate_secs: 95` for a 90-second entry window).
- Stop loss defaults to 39% below the confirmed average fill price. For a 0.93 fill,
  the trigger is 0.5673.
- Stop-loss exits retain the project's FOK execution type. The sell limit is derived
  from the displayed bid levels needed to cover the full remaining position; the
  stop price is a trigger, not an incorrectly priced sell limit.
- A market is marked stopped out as soon as the stop-loss trigger is reached. Its
  condition ID is persisted in `market_inventory_store.db`, and the entire market
  is blocked from further entries, including after a process restart.
- Stop-loss P&L is recorded when the position is fully exited, using actual average
  fill prices. Partial exits remain open and are retried; they are not counted as
  completed trades until the position is closed.

## Defaults

`trading_config.json` is the source of truth for strategy defaults:

- `entry_price`: `0.93`
- `entry_window_seconds`: `90`
- `stop_loss_pct`: `0.39`
- `listener_activate_secs`: `95`

## Setup

1. Install dependencies: `pip install -r requirements.txt`.
2. Copy `.env.example` to `.env` and fill in your own credentials locally.
3. Keep `DRY_RUN_DEFAULT=true` while validating behavior.
4. Ensure `market_inventory_store.db` is on persistent storage in hosted deployments.
   A local SQLite file on an ephemeral deployment filesystem will not survive a redeploy.
5. Run the existing application entry point as configured by `Procfile`.

Never include `.env`, private keys, or SSH private-key files in source archives or Git.


## Entry filter simplification

The directional 93c strategy now uses only the configured minimum outcome ask
price and configured final-market entry window for strategy eligibility. It no
longer applies underlying-price stability, volatility, sudden-move, momentum,
order-book imbalance, or pre-trade liquidity-depth filters. The FOK order is
submitted at the selected side's current best ask; if the requested quantity
cannot be filled, the exchange may reject/cancel the FOK order. Core operational
safeguards remain: valid active market and quotes, market not closed, no open
position or duplicate order, trading enabled, and permanent stopped-out-market
protection. The 39% stop-loss path is unchanged.

## External market-data integrations

The Binance market-data feed, depth-imbalance signal, exchange-triggered callbacks,
preemptive-cancel integration, and related configuration have been removed. The bot
uses Polymarket market data for its configured entry and stop-loss decisions; no
Binance WebSocket or REST market-data connection is started.

A Polymarket CLOB response of HTTP 403 with `Trading restricted in your region` is
an access restriction imposed by Polymarket. Removing the Binance integration does
not change that restriction or make order submission available in a blocked region.
Check Polymarket's current supported-region information and do not attempt to
circumvent the restriction.
