from strategies.entry_93c_strategy import Entry93cStrategy

# Legacy aliases — all workers use the 93¢ entry strategy.
BinanceFokStrategy = Entry93cStrategy
BinanceTriggeredFokStrategy = Entry93cStrategy
DirectionalFokStrategy = Entry93cStrategy
GabagoolStrategy = Entry93cStrategy
InventoryAccumulationStrategy = Entry93cStrategy
LateEntryStrategy = Entry93cStrategy
MomentumStrategy = Entry93cStrategy
SpreadCaptureStrategy = Entry93cStrategy

__all__ = [
    "Entry93cStrategy",
    "BinanceFokStrategy",
    "BinanceTriggeredFokStrategy",
    "DirectionalFokStrategy",
    "GabagoolStrategy",
    "InventoryAccumulationStrategy",
    "LateEntryStrategy",
    "MomentumStrategy",
    "SpreadCaptureStrategy",
]
