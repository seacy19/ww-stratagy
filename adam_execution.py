"""Deterministic execution-layer primitives for WW × Adam gate v0.3.

This module deliberately contains only point-in-time, rule-based calculations.
It is not a portfolio backtester and it does not choose WW candidates.
"""

from __future__ import annotations

from dataclasses import dataclass
from math import floor, sqrt
from statistics import median
from typing import Sequence

from adam_gate import AdamGateConfig, Bar, evaluate_entry_gate


def true_ranges(bars: Sequence[Bar]) -> list[float]:
    """Return daily True Range values, using high-low for the first bar."""
    if not bars:
        return []
    result = [bars[0].high - bars[0].low]
    for previous, current in zip(bars, bars[1:]):
        result.append(
            max(
                current.high - current.low,
                abs(current.high - previous.close),
                abs(current.low - previous.close),
            )
        )
    return result


def wilder_atr(bars: Sequence[Bar], period: int = 14) -> list[float | None]:
    """Return point-in-time Wilder ATR values.

    Values before the first complete period are None. The first valid value is
    the simple average of the first ``period`` TR values; subsequent values use
    Wilder's recursive smoothing.
    """
    if period < 1:
        raise ValueError("period must be positive")
    ranges = true_ranges(bars)
    values: list[float | None] = [None] * len(ranges)
    if len(ranges) < period:
        return values
    initial = sum(ranges[:period]) / period
    values[period - 1] = initial
    previous = initial
    for index in range(period, len(ranges)):
        previous = ((period - 1) * previous + ranges[index]) / period
        values[index] = previous
    return values


@dataclass(frozen=True)
class ZigZagPivot:
    kind: str
    pivot_index: int
    confirmation_index: int
    price: float
    atr_at_pivot: float


def confirmed_zigzag_pivots(
    bars: Sequence[Bar],
    atr_values: Sequence[float | None],
    reversal_atr: float = 2.0,
) -> list[ZigZagPivot]:
    """Find confirmed alternating ZigZag pivots without look-ahead.

    A candidate low is confirmed on the first later bar whose high rises by
    ``reversal_atr * ATR_at_candidate``. A candidate high is confirmed on the
    first later bar whose low falls by the same fixed-at-candidate threshold.
    Confirmation is timestamped on the later bar, never on the pivot bar.
    """
    if len(bars) != len(atr_values):
        raise ValueError("bars and atr_values must have equal length")
    if reversal_atr <= 0:
        raise ValueError("reversal_atr must be positive")

    first_valid = next(
        (index for index, value in enumerate(atr_values) if value is not None),
        None,
    )
    if first_valid is None:
        return []

    candidate_low_index = first_valid
    candidate_low_price = bars[first_valid].low
    candidate_low_atr = float(atr_values[first_valid])
    state = "seek_high"
    candidate_high_index: int | None = None
    candidate_high_price = 0.0
    candidate_high_atr = 0.0
    pivots: list[ZigZagPivot] = []

    for index in range(first_valid + 1, len(bars)):
        atr = atr_values[index]
        if atr is None:
            continue
        bar = bars[index]
        if state == "seek_high":
            if bar.low < candidate_low_price:
                candidate_low_index = index
                candidate_low_price = bar.low
                candidate_low_atr = float(atr)
            if bar.high >= candidate_low_price + reversal_atr * candidate_low_atr:
                pivots.append(
                    ZigZagPivot(
                        kind="low",
                        pivot_index=candidate_low_index,
                        confirmation_index=index,
                        price=candidate_low_price,
                        atr_at_pivot=candidate_low_atr,
                    )
                )
                state = "seek_low"
                candidate_high_index = index
                candidate_high_price = bar.high
                candidate_high_atr = float(atr)
        else:
            if bar.high > candidate_high_price:
                candidate_high_index = index
                candidate_high_price = bar.high
                candidate_high_atr = float(atr)
            if bar.low <= candidate_high_price - reversal_atr * candidate_high_atr:
                assert candidate_high_index is not None
                pivots.append(
                    ZigZagPivot(
                        kind="high",
                        pivot_index=candidate_high_index,
                        confirmation_index=index,
                        price=candidate_high_price,
                        atr_at_pivot=candidate_high_atr,
                    )
                )
                state = "seek_high"
                candidate_low_index = index
                candidate_low_price = bar.low
                candidate_low_atr = float(atr)
    return pivots


def latest_confirmed_pivot(
    pivots: Sequence[ZigZagPivot], kind: str, asof_index: int
) -> ZigZagPivot | None:
    """Return the latest pivot of ``kind`` known by ``asof_index``."""
    candidates = [
        pivot
        for pivot in pivots
        if pivot.kind == kind and pivot.confirmation_index <= asof_index
    ]
    return max(candidates, key=lambda pivot: pivot.confirmation_index, default=None)


@dataclass(frozen=True)
class PullbackResult:
    detected: bool
    meaningful: bool
    peak: float | None
    minimum: float | None
    depth: float | None
    depth_atr: float | None
    start_h: int | None
    end_h: int | None
    incomplete: bool



def first_pullback(
    projection: Sequence[float], atr: float, meaningful_atr: float = 1.0
) -> PullbackResult:
    """Find the first projected reverse segment and classify its depth.

    The first down-close starts the segment. A subsequent up-close ends it;
    if the projection ends while prices are still falling, the segment is
    marked incomplete and its minimum is the lowest point available in H.
    """
    if atr <= 0:
        raise ValueError("atr must be positive")
    if meaningful_atr <= 0:
        raise ValueError("meaningful_atr must be positive")
    if len(projection) < 2:
        return PullbackResult(False, False, None, None, None, None, None, None, False)

    start: int | None = None
    for index in range(1, len(projection)):
        if projection[index] < projection[index - 1]:
            start = index
            break
    if start is None:
        return PullbackResult(False, False, None, None, None, None, None, None, False)

    peak = max(projection[:start])
    end: int | None = None
    for index in range(start + 1, len(projection)):
        if projection[index] > projection[index - 1]:
            end = index - 1
            break
    last = end if end is not None else len(projection) - 1
    minimum = min(projection[start : last + 1])
    depth = peak - minimum
    depth_atr = depth / atr
    return PullbackResult(
        detected=True,
        meaningful=depth_atr >= meaningful_atr,
        peak=peak,
        minimum=minimum,
        depth=depth,
        depth_atr=depth_atr,
        start_h=start + 1,
        end_h=(end + 1) if end is not None else None,
        incomplete=end is None,
    )


@dataclass(frozen=True)
class EntryPlan:
    approved: bool
    direction_ok: bool
    now_price: float
    first_projected: float
    atr: float
    lower: float
    upper: float
    planned_price: float
    pullback: PullbackResult
    reason: str


def build_entry_plan(
    now_price: float,
    atr: float,
    projection: Sequence[float],
    *,
    alpha: float = 0.0,
    beta: float = 0.25,
    safety_atr: float = 0.0,
    meaningful_pullback_atr: float = 1.0,
    require_meaningful_pullback: bool = False,
) -> EntryPlan:
    """Build and freeze the T+1 price plan for an upward candidate.

    ``require_meaningful_pullback=True`` is the formal Adam strategy mode:
    the first projected reverse segment must reach the configured threshold
    (1 ATR for the main hypothesis; 2 ATR only for a robustness control).
    """
    if now_price <= 0 or atr <= 0:
        raise ValueError("now_price and atr must be positive")
    if not projection:
        raise ValueError("projection cannot be empty")
    if min(alpha, beta, safety_atr, meaningful_pullback_atr) < 0:
        raise ValueError("parameters cannot be negative")
    first_projected = float(projection[0])
    lower = now_price - beta * atr
    upper = first_projected + alpha * atr
    planned = upper
    direction_ok = first_projected > now_price
    pullback = first_pullback(projection, atr, meaningful_pullback_atr)
    if not direction_ok:
        return EntryPlan(
            False,
            False,
            now_price,
            first_projected,
            atr,
            lower,
            upper,
            planned,
            pullback,
            "first projected price does not continue upward",
        )
    if require_meaningful_pullback and not pullback.meaningful:
        return EntryPlan(
            False,
            True,
            now_price,
            first_projected,
            atr,
            lower,
            upper,
            planned,
            pullback,
            "no projected pullback of at least the meaningful ATR threshold",
        )
    if pullback.meaningful and (pullback.minimum is None or pullback.minimum < planned + safety_atr * atr):
        return EntryPlan(
            False,
            True,
            now_price,
            first_projected,
            atr,
            lower,
            upper,
            planned,
            pullback,
            "meaningful projected pullback is below the planned entry plus safety buffer",
        )
    return EntryPlan(
        True,
        True,
        now_price,
        first_projected,
        atr,
        lower,
        upper,
        planned,
        pullback,
        "approved for T+1 execution",
    )


@dataclass(frozen=True)
class PullbackReclaimConfig:
    """Parameters for the experimental breakout-pullback-reclaim state machine.

    This is deliberately separate from the second-reflection hypothesis.  A
    breakout first arms an episode; a later *observed* pullback can create an
    entry plan only after the close proves that demand absorbed it.  It must be
    evaluated out of sample before becoming part of the core WW × Adam rules.
    """

    max_wait_sessions: int = 5
    min_pullback_atr: float = 1.0
    max_pullback_atr: float = 2.0
    min_close_location: float = 0.70
    min_volume_ratio: float = 1.5
    beta_atr: float = 0.25
    alpha_atr: float = 0.25
    stop_buffer: float = 0.01
    max_stop_distance: float = 0.20


@dataclass(frozen=True)
class PullbackReclaimPlan:
    approved: bool
    reason: str
    breakout_index: int | None
    breakout_level: float | None
    anchor_high: float | None
    pullback_low: float | None
    pullback_low_index: int | None
    pullback_atr: float | None
    close_location: float | None
    volume_ratio: float | None
    lower: float | None
    upper: float | None
    stop: float | None
    upper_stop_distance: float | None


def breakout_pullback_reclaim_plan(
    bars: Sequence[Bar], config: PullbackReclaimConfig = PullbackReclaimConfig(),
    *, gate_config: AdamGateConfig = AdamGateConfig(), atr_period: int = 14,
) -> PullbackReclaimPlan:
    """Generate a T+1 plan after a confirmed breakout is actually retested.

    A prior 60-day breakout is searched only within ``max_wait_sessions``.
    The current bar must absorb a 1--2 ATR pullback while holding above the
    original breakout level, closing in its upper 30%, and retaining at least
    1.5x of normal volume.  No bar after the current one is inspected.
    """
    if config.max_wait_sessions < 1:
        raise ValueError("max_wait_sessions must be positive")
    if not 0 < config.min_pullback_atr <= config.max_pullback_atr:
        raise ValueError("pullback ATR bounds are invalid")
    if not 0 < config.min_close_location <= 1:
        raise ValueError("min_close_location must be in (0, 1]")
    if config.min_volume_ratio <= 0 or config.beta_atr < 0 or config.alpha_atr < 0:
        raise ValueError("entry parameters are invalid")

    atr_values = wilder_atr(bars, atr_period)
    current_index = len(bars) - 1
    current_atr = atr_values[current_index] if current_index >= 0 else None
    if current_atr is None:
        return PullbackReclaimPlan(False, "insufficient ATR history", None, None, None, None, None, None, None, None, None, None, None, None)

    # A pullback plan can only follow an earlier daily breakout.  Scanning the
    # short, fixed window makes the state explicit and prevents stale episodes.
    minimum_index = max(gate_config.breakout_lookback, gate_config.slow_ema,
                        gate_config.trend_breakout_lookback + 1,
                        gate_config.atr_lookback + 1, gate_config.volume_lookback,
                        atr_period - 1)
    start = max(minimum_index, current_index - config.max_wait_sessions)
    trigger_index: int | None = None
    trigger_gate: dict[str, object] | None = None
    for index in range(current_index - 1, start - 1, -1):
        gate = evaluate_entry_gate(bars[: index + 1], gate_config)
        if gate["triggers"]["breakout"]:
            trigger_index = index
            trigger_gate = gate
            break
    if trigger_index is None or trigger_gate is None:
        return PullbackReclaimPlan(False, "no recent 60-day breakout episode", None, None, None, None, None, None, None, None, None, None, None, None)

    breakout_level = float(trigger_gate["metrics"]["breakout_level"])
    anchor_high = max(bar.high for bar in bars[trigger_index : current_index + 1])
    pullback_low = min(bar.low for bar in bars[trigger_index + 1 : current_index + 1])
    pullback_low_index = next(
        index
        for index in range(trigger_index + 1, current_index + 1)
        if bars[index].low == pullback_low
    )
    trigger_atr = atr_values[trigger_index]
    assert trigger_atr is not None
    pullback_atr = (anchor_high - pullback_low) / float(trigger_atr)
    current = bars[-1]
    daily_range = current.high - current.low
    close_location = (current.close - current.low) / daily_range if daily_range > 0 else 0.5
    volume_base = median(bar.volume for bar in bars[-1 - gate_config.volume_lookback:-1])
    volume_ratio = current.volume / volume_base if volume_base > 0 else 0.0

    if not config.min_pullback_atr <= pullback_atr <= config.max_pullback_atr:
        return PullbackReclaimPlan(False, "observed pullback is outside the 1--2 ATR window", trigger_index, breakout_level, anchor_high, pullback_low, pullback_low_index, pullback_atr, close_location, volume_ratio, None, None, None, None)
    if current.close < breakout_level:
        return PullbackReclaimPlan(False, "close did not hold the original breakout level", trigger_index, breakout_level, anchor_high, pullback_low, pullback_low_index, pullback_atr, close_location, volume_ratio, None, None, None, None)
    if close_location < config.min_close_location:
        return PullbackReclaimPlan(False, "pullback day did not close with acceptance", trigger_index, breakout_level, anchor_high, pullback_low, pullback_low_index, pullback_atr, close_location, volume_ratio, None, None, None, None)
    if volume_ratio < config.min_volume_ratio:
        return PullbackReclaimPlan(False, "pullback acceptance volume is insufficient", trigger_index, breakout_level, anchor_high, pullback_low, pullback_low_index, pullback_atr, close_location, volume_ratio, None, None, None, None)

    lower = current.close - config.beta_atr * float(current_atr)
    upper = current.close + config.alpha_atr * float(current_atr)
    stop = pullback_low - config.stop_buffer
    risk = (upper - stop) / upper
    if not 0 < risk <= config.max_stop_distance:
        return PullbackReclaimPlan(False, "stop distance exceeds the configured maximum", trigger_index, breakout_level, anchor_high, pullback_low, pullback_low_index, pullback_atr, close_location, volume_ratio, lower, upper, stop, risk)
    return PullbackReclaimPlan(True, "approved for T+1 after observed pullback acceptance", trigger_index, breakout_level, anchor_high, pullback_low, pullback_low_index, pullback_atr, close_location, volume_ratio, lower, upper, stop, risk)


@dataclass(frozen=True)
class MinuteBar:
    time: int
    open: float
    high: float
    low: float
    close: float


@dataclass(frozen=True)
class ExecutionResult:
    status: str
    entry_type: str | None
    execution_time: int | None
    execution_price: float | None
    reason: str


def simulate_t1_entry(
    plan: EntryPlan,
    open_price: float,
    minute_bars: Sequence[MinuteBar],
    *,
    slippage: float = 0.0,
    cutoff_time: int = 1430,
) -> ExecutionResult:
    """Simulate the frozen T+1 entry plan using one-minute closes.

    A confirmation on minute j is filled at minute j+1's open. If the next
    open has left the frozen entry zone, the planned fill is cancelled.
    """
    if not plan.approved:
        return ExecutionResult("rejected", None, None, None, "plan was not approved")
    if slippage < 0:
        raise ValueError("slippage cannot be negative")
    if plan.lower <= open_price <= plan.upper:
        return ExecutionResult(
            "filled", "open_direct", None, open_price + slippage, "open is in entry zone"
        )

    bars = [bar for bar in minute_bars if bar.time <= cutoff_time]
    if not bars:
        return ExecutionResult("expired", None, None, None, "no eligible minute data before cutoff")

    previous_close = open_price
    returned_from_above = False
    for index, bar in enumerate(bars):
        if index + 1 >= len(bars):
            break
        next_bar = bars[index + 1]
        if next_bar.time > cutoff_time:
            break

        in_zone = plan.lower <= bar.close <= plan.upper
        if open_price < plan.lower:
            if previous_close < plan.lower and in_zone and bar.close > previous_close:
                if plan.lower <= next_bar.open <= plan.upper:
                    return ExecutionResult(
                        "filled",
                        "reentry_from_below",
                        next_bar.time,
                        next_bar.open + slippage,
                        "minute close re-entered upward from below",
                    )
                return ExecutionResult(
                    "expired", None, None, None, "next-minute open left the frozen entry zone"
                )
        elif open_price > plan.upper:
            if not returned_from_above:
                if previous_close > plan.upper and in_zone:
                    returned_from_above = True
            elif in_zone and bar.close > previous_close:
                if plan.lower <= next_bar.open <= plan.upper:
                    return ExecutionResult(
                        "filled",
                        "reentry_from_above",
                        next_bar.time,
                        next_bar.open + slippage,
                        "minute close re-entered upward after returning from above",
                    )
                return ExecutionResult(
                    "expired", None, None, None, "next-minute open left the frozen entry zone"
                )
        previous_close = bar.close

    return ExecutionResult("expired", None, None, None, "no qualifying re-entry before cutoff")


def confirmed_swing_lows(bars: Sequence[Bar]) -> list[tuple[int, int, float]]:
    """Return (pivot_index, confirmation_index, low) three-bar swing lows."""
    result: list[tuple[int, int, float]] = []
    for index in range(1, len(bars) - 1):
        if bars[index].low < bars[index - 1].low and bars[index].low <= bars[index + 1].low:
            result.append((index, index + 1, bars[index].low))
    return result


def initial_stop(
    bars: Sequence[Bar], asof_index: int, buffer: float = 0.01
) -> float | None:
    """Return the stop below the latest confirmed swing low known at as-of."""
    if buffer < 0:
        raise ValueError("buffer cannot be negative")
    candidates = [
        item for item in confirmed_swing_lows(bars) if item[1] <= asof_index
    ]
    if not candidates:
        return None
    return candidates[-1][2] - buffer


def move_stop_up(
    bars: Sequence[Bar], asof_index: int, current_stop: float, buffer: float = 0.01
) -> float:
    """Move a stop only upward when a newly confirmed higher low permits it."""
    candidate = initial_stop(bars, asof_index, buffer)
    return current_stop if candidate is None else max(current_stop, candidate)


def normalized_path_errors(
    actual: Sequence[float], projected: Sequence[float], atr: float
) -> list[float]:
    """Return per-step errors in ATR units, using a fixed point-in-time ATR."""
    if len(actual) != len(projected):
        raise ValueError("actual and projected paths must have equal length")
    if atr <= 0:
        raise ValueError("atr must be positive")
    return [(a - p) / atr for a, p in zip(actual, projected)]


def atr_rmse(errors: Sequence[float]) -> float:
    """Aggregate normalized path errors with RMSE."""
    if not errors:
        raise ValueError("errors cannot be empty")
    return sqrt(sum(error * error for error in errors) / len(errors))


def risk_position_size(
    risk_budget: float,
    entry_price: float,
    stop_price: float,
    estimated_cost: float = 0.0,
    lot_size: int = 100,
) -> int:
    """Return an A-share lot-rounded position size from a fixed risk budget."""
    if risk_budget < 0 or estimated_cost < 0 or lot_size < 1:
        raise ValueError("risk and lot parameters must be valid")
    risk_per_share = entry_price - stop_price + estimated_cost
    if risk_per_share <= 0:
        raise ValueError("entry price must exceed stop price plus costs")
    raw = floor(risk_budget / risk_per_share)
    return (raw // lot_size) * lot_size
