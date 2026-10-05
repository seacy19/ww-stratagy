"""WW strategy Adam gate: deterministic prototype, not a validated trading model."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from math import exp, log
from statistics import median
from typing import Sequence


@dataclass(frozen=True)
class Bar:
    open: float
    high: float
    low: float
    close: float
    volume: float


@dataclass(frozen=True)
class AdamGateConfig:
    breakout_lookback: int = 60
    volume_lookback: int = 20
    min_volume_ratio: float = 1.5
    fast_ema: int = 20
    slow_ema: int = 60
    trend_breakout_lookback: int = 20
    atr_lookback: int = 20
    min_range_atr: float = 1.8
    min_gap_atr: float = 0.5
    min_close_location: float = 0.70


def _ema(values: Sequence[float], period: int) -> float:
    if not values:
        raise ValueError("values cannot be empty")
    alpha = 2.0 / (period + 1.0)
    result = float(values[0])
    for value in values[1:]:
        result = alpha * float(value) + (1.0 - alpha) * result
    return result


def _true_ranges(bars: Sequence[Bar]) -> list[float]:
    ranges: list[float] = []
    for index, bar in enumerate(bars):
        if index == 0:
            ranges.append(bar.high - bar.low)
            continue
        previous_close = bars[index - 1].close
        ranges.append(
            max(
                bar.high - bar.low,
                abs(bar.high - previous_close),
                abs(bar.low - previous_close),
            )
        )
    return ranges


def second_reflection(
    prices: Sequence[float], horizon: int | None = None, *, log_price: bool = False
) -> list[float]:
    """Project the past path around NOW using a double reflection.

    For h=1..H, linear projection is P_hat[n+h] = 2*P[n] - P[n-h].
    The log-price variant repeats percentage rather than absolute changes.
    """
    if len(prices) < 2:
        raise ValueError("at least two prices are required")
    if any(price <= 0 for price in prices):
        raise ValueError("prices must be positive")
    max_horizon = len(prices) - 1
    selected_horizon = max_horizon if horizon is None else horizon
    if selected_horizon < 1 or selected_horizon > max_horizon:
        raise ValueError("horizon must be between 1 and len(prices)-1")

    now = float(prices[-1])
    projected: list[float] = []
    for h in range(1, selected_horizon + 1):
        past = float(prices[-1 - h])
        if log_price:
            projected.append(exp(2.0 * log(now) - log(past)))
        else:
            projected.append(2.0 * now - past)
    return projected


def evaluate_entry_gate(
    bars: Sequence[Bar], config: AdamGateConfig = AdamGateConfig()
) -> dict[str, object]:
    """Evaluate whether a WW-selected candidate has price approval today."""
    minimum_bars = max(
        config.breakout_lookback + 1,
        config.slow_ema + 1,
        config.trend_breakout_lookback + 2,
        config.atr_lookback + 2,
        config.volume_lookback + 1,
    )
    if len(bars) < minimum_bars:
        raise ValueError(f"at least {minimum_bars} bars are required")

    current = bars[-1]
    previous = bars[-2]
    closes = [bar.close for bar in bars]
    true_ranges = _true_ranges(bars)
    atr = sum(true_ranges[-1 - config.atr_lookback : -1]) / config.atr_lookback
    volume_base = median(
        bar.volume for bar in bars[-1 - config.volume_lookback : -1]
    )
    volume_ratio = current.volume / volume_base if volume_base > 0 else 0.0

    breakout_level = max(
        bar.high for bar in bars[-1 - config.breakout_lookback : -1]
    )
    breakout = (
        current.close > breakout_level
        and volume_ratio >= config.min_volume_ratio
    )

    trend_level = max(
        bar.high for bar in bars[-1 - config.trend_breakout_lookback : -1]
    )
    prior_trend_level = max(
        bar.high for bar in bars[-2 - config.trend_breakout_lookback : -2]
    )
    fast_ema = _ema(closes, config.fast_ema)
    slow_ema = _ema(closes, config.slow_ema)
    confirmed_trend_change = (
        current.close > fast_ema > slow_ema
        and current.close > trend_level
        and previous.close <= prior_trend_level
    )

    gap = max(0.0, current.open - previous.close)
    range_atr = true_ranges[-1] / atr if atr > 0 else 0.0
    gap_atr = gap / atr if atr > 0 else 0.0
    daily_range = current.high - current.low
    close_location = (
        (current.close - current.low) / daily_range if daily_range > 0 else 0.5
    )
    momentum_wakeup = (
        (range_atr >= config.min_range_atr or gap_atr >= config.min_gap_atr)
        and close_location >= config.min_close_location
        and volume_ratio >= config.min_volume_ratio
    )

    triggers = {
        "breakout": breakout,
        "confirmed_trend_change": confirmed_trend_change,
        "momentum_wakeup": momentum_wakeup,
    }
    return {
        "qualified": any(triggers.values()),
        "triggers": triggers,
        "metrics": {
            "close": current.close,
            "breakout_level": breakout_level,
            "trend_level": trend_level,
            "fast_ema": fast_ema,
            "slow_ema": slow_ema,
            "atr": atr,
            "volume_ratio": volume_ratio,
            "range_atr": range_atr,
            "gap_atr": gap_atr,
            "close_location": close_location,
        },
        "config": asdict(config),
    }
