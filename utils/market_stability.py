"""Fail-closed short-window stability gate for the directional entry strategy."""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Iterable, Optional, Tuple


@dataclass(frozen=True)
class StabilityResult:
    allowed: bool
    reason: str
    range_percent: Optional[float] = None
    short_move_percent: Optional[float] = None
    sample_count: int = 0


def evaluate_market_stability(
    history: Iterable[Tuple[float, float]],
    *,
    now: float,
    stabilization_window_seconds: float = 10.0,
    stabilization_tolerance_percent: float = 0.05,
    short_window_seconds: float = 5.0,
    max_short_window_move_percent: float = 0.15,
    max_data_age_seconds: float = 2.0,
) -> StabilityResult:
    """Require enough fresh underlying-price samples and bounded movement.

    All timestamps are Unix seconds. Missing/stale/incomplete history blocks a
    new entry; it never affects management or exit of an existing position.
    """
    try:
        now = float(now)
        full_window = float(stabilization_window_seconds)
        tolerance = float(stabilization_tolerance_percent)
        short_window = float(short_window_seconds)
        max_short_move = float(max_short_window_move_percent)
        max_age = float(max_data_age_seconds)
    except (TypeError, ValueError):
        return StabilityResult(False, "invalid_stability_configuration")
    values = (now, full_window, tolerance, short_window, max_short_move, max_age)
    if not all(math.isfinite(v) for v in values) or min(full_window, tolerance, short_window, max_short_move, max_age) <= 0:
        return StabilityResult(False, "invalid_stability_configuration")
    if short_window > full_window:
        return StabilityResult(False, "invalid_stability_configuration")

    samples = []
    try:
        for ts, price in history:
            ts, price = float(ts), float(price)
            if math.isfinite(ts) and math.isfinite(price) and price > 0 and now - full_window <= ts <= now:
                samples.append((ts, price))
    except (TypeError, ValueError):
        return StabilityResult(False, "invalid_price_history")
    samples.sort(key=lambda item: item[0])
    if len(samples) < 3:
        return StabilityResult(False, "insufficient_stability_samples", sample_count=len(samples))
    if now - samples[-1][0] > max_age:
        return StabilityResult(False, "underlying_price_stale", sample_count=len(samples))
    if samples[0][0] > now - full_window + 0.75:
        return StabilityResult(False, "insufficient_stability_window", sample_count=len(samples))

    base = samples[0][1]
    range_pct = ((max(px for _, px in samples) - min(px for _, px in samples)) / base) * 100.0
    if range_pct > tolerance + 1e-12:
        return StabilityResult(False, "underlying_market_unstable", range_pct, None, len(samples))

    short_samples = [(ts, px) for ts, px in samples if ts >= now - short_window]
    if len(short_samples) < 2 or short_samples[0][0] > now - short_window + 0.75:
        return StabilityResult(False, "insufficient_short_window_samples", range_pct, None, len(samples))
    short_base = short_samples[0][1]
    short_move = abs((short_samples[-1][1] - short_base) / short_base) * 100.0
    if short_move > max_short_move + 1e-12:
        return StabilityResult(False, "sudden_underlying_price_move", range_pct, short_move, len(samples))
    return StabilityResult(True, "stable", range_pct, short_move, len(samples))
