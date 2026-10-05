"""Event-level daily proxy backtest for the Adam gate.

Results from this module must be labelled as PIT price-proxy results, not as
WW fundamental-selection results. Minute re-entry is intentionally not
assumed; events that do not fill at the next open are recorded as unfilled.
"""

from __future__ import annotations

from dataclasses import dataclass
from statistics import mean, median
from typing import Mapping, Sequence

from adam_execution import (
    build_entry_plan,
    confirmed_zigzag_pivots,
    confirmed_swing_lows,
    wilder_atr,
)
from adam_gate import Bar, evaluate_entry_gate, second_reflection
from pit_accounting import (
    CorporateAction,
    adjusted_bar_asof,
    adjusted_bar_from_factor,
    adjusted_stop_asof,
    ledger_return,
)
from proxy_pool import ProxyCandidate
from tushare_data import DailyRecord


@dataclass(frozen=True)
class CostModel:
    commission_bps: float = 0.0
    stamp_tax_bps: float = 0.0
    slippage_bps: float = 0.0

    def __post_init__(self):
        if min(self.commission_bps, self.stamp_tax_bps, self.slippage_bps) < 0:
            raise ValueError("cost parameters cannot be negative")


@dataclass(frozen=True)
class ExitSnapshot:
    date: str
    price: float
    exit_type: str
    holding_days: int


@dataclass(frozen=True)
class EventResult:
    candidate: ProxyCandidate
    strategy: str
    status: str
    entry_date: str | None
    entry_price: float | None
    stop_price: float | None
    exit_reason: str | None
    returns: Mapping[int, float | None]
    exit_date: str | None = None
    exit_price: float | None = None
    holding_days: int | None = None
    horizon_exits: Mapping[int, ExitSnapshot | None] | None = None


@dataclass(frozen=True)
class _CodeState:
    """Point-in-time calculations cached once per security."""

    records: tuple[DailyRecord, ...]
    bars: tuple[Bar, ...]
    technical_bars: tuple[Bar, ...]
    date_to_index: Mapping[str, int]
    atr_values: tuple[float | None, ...]
    pivots: tuple
    latest_stop_series: tuple[float | None, ...]
    trailing_stop_series: tuple[float | None, ...]


def _buy_price(raw_price: float, costs: CostModel) -> float:
    return raw_price * (1.0 + costs.slippage_bps / 10000.0)


def _sell_price(raw_price: float, costs: CostModel) -> float:
    return raw_price * (1.0 - costs.slippage_bps / 10000.0)


def _net_return(entry: float, exit_price: float, costs: CostModel) -> float:
    buy_multiplier = 1.0 + costs.commission_bps / 10000.0
    sell_multiplier = 1.0 - (
        costs.commission_bps + costs.stamp_tax_bps
    ) / 10000.0
    return exit_price * sell_multiplier / (entry * buy_multiplier) - 1.0


def _simulate_holding(
    bars: Sequence[Bar],
    trade_dates: Sequence[str],
    trailing_stop_series: Sequence[float | None],
    entry_index: int,
    entry_price: float,
    stop_price: float,
    horizons: Sequence[int],
    costs: CostModel,
    actions: Sequence[CorporateAction] = (),
    state_provider=None,
) -> tuple[
    dict[int, float | None],
    str | None,
    ExitSnapshot | None,
    dict[int, ExitSnapshot | None],
]:
    """Simulate daily exits, respecting T+1 by skipping stop checks on entry day."""
    horizon_set = set(horizons)
    max_horizon = max(horizons)
    result: dict[int, float | None] = {horizon: None for horizon in horizons}
    horizon_exits: dict[int, ExitSnapshot | None] = {
        horizon: None for horizon in horizons
    }
    current_stop = stop_price
    stop_scale_date = trade_dates[entry_index - 1]
    # T+1 cannot be sold, but its closing confirmation may move the stop for
    # T+2. Apply that update before the first sellable-day check.
    if state_provider is not None:
        entry_state = state_provider(entry_index)
        candidate = (
            entry_state.latest_stop_series[entry_index]
            if entry_index < len(entry_state.latest_stop_series)
            else None
        )
    else:
        candidate = (
            trailing_stop_series[entry_index]
            if entry_index < len(trailing_stop_series)
            else None
        )
    if candidate is not None:
        current_stop = max(current_stop, candidate)
    current_stop = adjusted_stop_asof(
        current_stop,
        stop_scale_date,
        trade_dates[entry_index],
        actions,
    )
    stop_scale_date = trade_dates[entry_index]
    exit_price: float | None = None
    exit_reason: str | None = None
    exit_date: str | None = None
    exit_holding_days: int | None = None

    for offset in range(1, max_horizon + 1):
        index = entry_index + offset
        if index >= len(bars):
            break
        bar = bars[index]
        current_stop = adjusted_stop_asof(
            current_stop,
            stop_scale_date,
            trade_dates[index],
            actions,
        )
        stop_scale_date = trade_dates[index]
        if exit_price is None:
            if bar.open <= current_stop:
                exit_price = _sell_price(bar.open, costs)
                exit_reason = "stop_gap"
                exit_date = trade_dates[index]
                exit_holding_days = index - entry_index
            elif bar.low <= current_stop:
                exit_price = _sell_price(current_stop, costs)
                exit_reason = "stop_intraday_daily_proxy"
                exit_date = trade_dates[index]
                exit_holding_days = index - entry_index
        if exit_price is None:
            if state_provider is not None:
                current_state = state_provider(index)
                candidate = (
                    current_state.latest_stop_series[index]
                    if index < len(current_state.latest_stop_series)
                    else None
                )
            else:
                candidate = (
                    trailing_stop_series[index]
                    if index < len(trailing_stop_series)
                    else None
                )
            if candidate is not None:
                current_stop = max(current_stop, candidate)
        if offset in horizon_set:
            if exit_price is not None:
                final_price = exit_price
                final_date = exit_date
                final_type = exit_reason or "stop"
                final_holding_days = exit_holding_days or (index - entry_index)
            else:
                final_price = _sell_price(bar.close, costs)
                final_date = trade_dates[index]
                final_type = "horizon_close"
                final_holding_days = offset
            buy_cost = entry_price * costs.commission_bps / 10000.0
            sell_cost = final_price * (
                costs.commission_bps + costs.stamp_tax_bps
            ) / 10000.0
            result[offset] = ledger_return(
                10000,
                entry_price,
                trade_dates[entry_index],
                final_price,
                final_date,
                actions,
                buy_cost=buy_cost * 10000,
                sell_cost=sell_cost * 10000,
            )
            horizon_exits[offset] = ExitSnapshot(
                final_date,
                final_price,
                final_type,
                final_holding_days,
            )
    stop_exit = (
        ExitSnapshot(exit_date, exit_price, exit_reason, exit_holding_days)
        if exit_date is not None
        and exit_price is not None
        and exit_reason is not None
        and exit_holding_days is not None
        else None
    )
    return result, exit_reason, stop_exit, horizon_exits


def _event_result(
    candidate: ProxyCandidate,
    strategy: str,
    records: Sequence[DailyRecord],
    index: int,
    entry_price: float,
    stop: float | None,
    trailing_stop_series: Sequence[float | None],
    costs: CostModel,
    horizons: Sequence[int],
    actions: Sequence[CorporateAction] = (),
    state_provider=None,
) -> EventResult:
    bars = [record.bar for record in records]
    if stop is None or entry_price <= stop:
        return EventResult(
            candidate, strategy, "no_stop", records[index].trade_date, entry_price, stop, None,
            {horizon: None for horizon in horizons},
        )
    returns, reason, stop_exit, horizon_exits = _simulate_holding(
        bars,
        [record.trade_date for record in records],
        trailing_stop_series,
        index,
        entry_price,
        stop,
        horizons,
        costs,
        actions,
        state_provider,
    )
    return EventResult(
        candidate,
        strategy,
        "filled",
        records[index].trade_date,
        entry_price,
        stop,
        reason,
        returns,
        stop_exit.date if stop_exit else None,
        stop_exit.price if stop_exit else None,
        stop_exit.holding_days if stop_exit else None,
        horizon_exits,
    )


def _build_code_state(
    records: Sequence[DailyRecord],
    actions: Sequence[CorporateAction] = (),
    asof_date: str | None = None,
) -> _CodeState:
    ordered = tuple(sorted(records, key=lambda item: item.trade_date))
    bars = tuple(record.bar for record in ordered)
    date_to_index = {record.trade_date: index for index, record in enumerate(ordered)}
    if asof_date is None:
        technical_bars = tuple(
            adjusted_bar_from_factor(record.bar, record.adj_factor)
            if record.adj_factor is not None
            else record.bar
            for record in ordered
        )
    else:
        technical_bars = tuple(
            (
                adjusted_bar_from_factor(record.bar, record.adj_factor)
                if record.adj_factor is not None
                else adjusted_bar_asof(
                    record.bar, record.trade_date, asof_date, actions
                )
            )
            for record in ordered
            if record.trade_date <= asof_date
        )
    atr_values = tuple(wilder_atr(technical_bars, 14))
    pivots = tuple(
        confirmed_zigzag_pivots(technical_bars, atr_values, reversal_atr=2.0)
    )

    # ``latest_stop_series`` reproduces the initial-stop rule: use the most
    # recently confirmed swing low known at the as-of date.  The trailing
    # series is the same low-point stream with a monotone upward update for
    # the holding period.
    latest_stop: list[float | None] = [None] * len(technical_bars)
    trailing_stop: list[float | None] = [None] * len(technical_bars)
    current_latest: float | None = None
    current_trailing: float | None = None
    for pivot_index, confirmation_index, low in confirmed_swing_lows(technical_bars):
        if confirmation_index >= len(technical_bars):
            continue
        candidate = low - 0.01
        current_latest = candidate
        latest_stop[confirmation_index] = current_latest
        current_trailing = candidate if current_trailing is None else max(current_trailing, candidate)
        trailing_stop[confirmation_index] = current_trailing
    # Reset before forward filling: the loop above ends at the LAST pivot.
    # Seeding the prefix with it would expose a future low before confirmation.
    current_latest = None
    current_trailing = None
    for index in range(len(technical_bars)):
        if latest_stop[index] is None:
            latest_stop[index] = current_latest
        else:
            current_latest = latest_stop[index]
        if trailing_stop[index] is None:
            trailing_stop[index] = current_trailing
        else:
            current_trailing = trailing_stop[index]

    # Adjustment factors put technical bars on a common reference scale. For
    # execution and stop comparisons, convert each stop back to that date's
    # raw trading-price scale.
    prefix_records = ordered[: len(technical_bars)]
    if any(record.adj_factor is not None for record in prefix_records):
        latest_stop = [
            (
                value / record.adj_factor
                if value is not None and record.adj_factor not in (None, 0)
                else value
            )
            for value, record in zip(latest_stop, prefix_records)
        ]
        trailing_stop = [
            (
                value / record.adj_factor
                if value is not None and record.adj_factor not in (None, 0)
                else value
            )
            for value, record in zip(trailing_stop, prefix_records)
        ]

    return _CodeState(
        ordered,
        bars,
        technical_bars,
        date_to_index,
        atr_values,
        pivots,
        tuple(latest_stop),
        tuple(trailing_stop),
    )


def evaluate_candidate_events(
    candidates: Sequence[ProxyCandidate],
    records_by_code: Mapping[str, Sequence[DailyRecord]],
    *,
    actions_by_code: Mapping[str, Sequence[CorporateAction]] | None = None,
    costs: CostModel = CostModel(),
    horizons: Sequence[int] = (1, 5, 10, 20),
    max_initial_stop_distance_pct: float = 0.20,
) -> list[EventResult]:
    """Evaluate Adam-open event results on the proxy candidates.

    The Adam branch rejects plans whose initial stop is more than
    ``max_initial_stop_distance_pct`` below the frozen lower entry bound.
    """
    if not 0 <= max_initial_stop_distance_pct <= 1:
        raise ValueError("max_initial_stop_distance_pct must be between 0 and 1")
    results: list[EventResult] = []

    # Candidates are processed in date order.  Retain only the latest state
    # per security so the point-in-time rebuild does not retain thousands of
    # full historical technical-bar arrays in memory.
    states: dict[str, tuple[str, _CodeState]] = {}
    full_states: dict[str, _CodeState] = {}

    def get_state(code: str, asof_date: str) -> _CodeState:
        cached = states.get(code)
        if cached is not None and cached[0] == asof_date:
            return cached[1]
        actions = () if actions_by_code is None else actions_by_code.get(code, ())
        state = _build_code_state(
            records_by_code[code], actions=actions, asof_date=asof_date
        )
        states[code] = (asof_date, state)
        return state

    def get_full_state(code: str) -> _CodeState:
        state = full_states.get(code)
        if state is None:
            actions = () if actions_by_code is None else actions_by_code.get(code, ())
            state = _build_code_state(records_by_code[code], actions=actions)
            full_states[code] = state
        return state

    for candidate in candidates:
        state = get_state(candidate.ts_code, candidate.trade_date)
        records = state.records
        index = state.date_to_index.get(candidate.trade_date)
        if index is None or index + 1 >= len(records):
            continue
        next_record = records[index + 1]
        if next_record.bar.open <= 0:
            continue

        actions = () if actions_by_code is None else actions_by_code.get(candidate.ts_code, ())

        def state_provider(raw_index: int) -> _CodeState:
            return get_full_state(candidate.ts_code)

        initial = state.latest_stop_series[index]
        entry_price = _buy_price(next_record.bar.open, costs)

        current_bars = state.technical_bars
        gate = evaluate_entry_gate(current_bars)
        if not gate["qualified"]:
            continue
        atr = state.atr_values[index]
        if atr is None:
            continue
        low_pivots = [
            pivot for pivot in state.pivots
            if pivot.kind == "low"
            and pivot.confirmation_index <= index
            and pivot.pivot_index < index
        ]
        if not low_pivots:
            continue
        low = max(low_pivots, key=lambda pivot: pivot.confirmation_index)
        path = [bar.close for bar in current_bars[low.pivot_index :]]
        projection = second_reflection(path)
        plan = build_entry_plan(
            current_bars[-1].close,
            atr,
            projection,
            require_meaningful_pullback=True,
        )
        if not plan.approved:
            continue
        entry_factor = next_record.adj_factor
        lower_raw = (
            plan.lower / entry_factor
            if entry_factor is not None and entry_factor > 0
            else plan.lower
        )
        upper_raw = (
            plan.upper / entry_factor
            if entry_factor is not None and entry_factor > 0
            else plan.upper
        )
        initial_entry_scale = (
            adjusted_stop_asof(
                initial,
                candidate.trade_date,
                next_record.trade_date,
                actions,
            )
            if initial is not None
            else None
        )
        if (
            initial_entry_scale is None
            or initial_entry_scale <= 0
            or lower_raw <= initial_entry_scale
        ):
            results.append(
                EventResult(
                    candidate,
                    "adam_open",
                    "rejected_invalid_initial_stop",
                    None,
                    None,
                    initial,
                    None,
                    {horizon: None for horizon in horizons},
                )
            )
            continue
        stop_distance_pct = (lower_raw - initial_entry_scale) / lower_raw
        if stop_distance_pct > max_initial_stop_distance_pct:
            results.append(
                EventResult(
                    candidate,
                    "adam_open",
                    "rejected_stop_distance_over_20pct",
                    None,
                    None,
                    initial,
                    None,
                    {horizon: None for horizon in horizons},
                )
            )
            continue
        if not lower_raw <= next_record.bar.open <= upper_raw:
            results.append(
                EventResult(
                    candidate,
                    "adam_open",
                    "unfilled_open_outside_zone",
                    None,
                    None,
                    None,
                    None,
                    {horizon: None for horizon in horizons},
                )
            )
            continue
        results.append(
            _event_result(
                candidate,
                "adam_open",
                records,
                index + 1,
                entry_price,
                initial,
                state.trailing_stop_series,
                costs,
                horizons,
                actions,
                state_provider,
            )
        )
    return results


def summarize_results(
    results: Sequence[EventResult], horizons: Sequence[int] = (1, 5, 10, 20)
) -> dict[str, dict[str, float | int | None]]:
    """Summarize event-level fill counts and net return distributions."""
    summary: dict[str, dict[str, float | int | None]] = {}
    for strategy in sorted({result.strategy for result in results}):
        selected = [result for result in results if result.strategy == strategy]
        row: dict[str, float | int | None] = {
            "events": len(selected),
            "filled": sum(result.status == "filled" for result in selected),
        }
        for horizon in horizons:
            values = [
                result.returns[horizon]
                for result in selected
                if result.returns[horizon] is not None
            ]
            row[f"h{horizon}_n"] = len(values)
            row[f"h{horizon}_mean"] = mean(values) if values else None
            row[f"h{horizon}_median"] = median(values) if values else None
        summary[strategy] = row
    return summary
