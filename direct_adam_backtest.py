"""Direct all-A-share Adam-gate scan and event backtest.

This module deliberately does not construct a WW mainline or momentum proxy
pool.  Every non-ST security with sufficient daily history is scanned at each
as-of date.  Technical state is built once per security, then the Adam gate is
evaluated on the causal prefix for each date.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Sequence

from adam_execution import build_entry_plan
from adam_gate import evaluate_entry_gate, second_reflection
from pit_accounting import CorporateAction, adjusted_stop_asof
from proxy_backtest import (
    CostModel,
    EventResult,
    _build_code_state,
    _event_result,
)
from proxy_pool import ProxyCandidate, SecurityMeta, is_a_share_universe_member
from tushare_data import DailyRecord


@dataclass(frozen=True)
class DirectAdamRow:
    window: str
    trade_date: str
    ts_code: str
    gate_triggers: str
    t_close_raw: float
    t_close_technical: float
    return_5d: float | None
    return_10d: float | None
    return_20d: float | None
    close_vs_20d_high: float | None
    range_atr: float | None
    volume_ratio: float | None
    close_location: float | None
    pullback_depth_atr: float | None
    lower_raw: float | None
    upper_raw: float | None
    stop_raw: float | None
    stop_distance_pct: float | None
    status: str
    entry_date: str | None
    entry_price: float | None
    exit_reason: str | None
    exit_date: str | None
    exit_price: float | None
    holding_days: int | None
    h1_sell_date: str | None
    h1_sell_price: float | None
    h1_sell_type: str | None
    h5_sell_date: str | None
    h5_sell_price: float | None
    h5_sell_type: str | None
    h10_sell_date: str | None
    h10_sell_price: float | None
    h10_sell_type: str | None
    h20_sell_date: str | None
    h20_sell_price: float | None
    h20_sell_type: str | None
    h1_return: float | None
    h5_return: float | None
    h10_return: float | None
    h20_return: float | None


def _pct_change(values: Sequence[float], index: int, lookback: int) -> float | None:
    if index < lookback or values[index - lookback] <= 0:
        return None
    return values[index] / values[index - lookback] - 1.0


def _empty_returns(horizons: Sequence[int]) -> dict[int, float | None]:
    return {horizon: None for horizon in horizons}


def _row_from_event(
    window: str,
    code: str,
    trade_date: str,
    gate: Mapping[str, object],
    state,
    index: int,
    pullback_depth_atr: float | None,
    lower_raw: float | None,
    upper_raw: float | None,
    stop_raw: float | None,
    stop_distance_pct: float | None,
    status: str,
    event: EventResult | None = None,
) -> DirectAdamRow:
    technical = state.technical_bars
    records = state.records
    metrics = gate.get("metrics", {})
    current_technical = technical[index].close
    highs = [bar.high for bar in technical[max(0, index - 19) : index + 1]]
    close_vs_high = (
        current_technical / max(highs) - 1.0 if highs and max(highs) > 0 else None
    )
    returns = _empty_returns((1, 5, 10, 20))
    if event is not None:
        returns = dict(event.returns)
    horizon_exits = event.horizon_exits if event is not None else {}

    def snapshot(horizon: int, field: str):
        snapshot = (horizon_exits or {}).get(horizon)
        return getattr(snapshot, field) if snapshot is not None else None

    return DirectAdamRow(
        window,
        trade_date,
        code,
        ",".join(
            name for name, active in (gate.get("triggers", {}) or {}).items() if active
        ),
        records[index].bar.close,
        current_technical,
        _pct_change([bar.close for bar in technical], index, 5),
        _pct_change([bar.close for bar in technical], index, 10),
        _pct_change([bar.close for bar in technical], index, 20),
        close_vs_high,
        float(metrics["range_atr"]) if "range_atr" in metrics else None,
        float(metrics["volume_ratio"]) if "volume_ratio" in metrics else None,
        float(metrics["close_location"])
        if "close_location" in metrics
        else None,
        pullback_depth_atr,
        lower_raw,
        upper_raw,
        stop_raw,
        stop_distance_pct,
        status,
        event.entry_date if event else None,
        event.entry_price if event else None,
        event.exit_reason if event else None,
        event.exit_date if event else None,
        event.exit_price if event else None,
        event.holding_days if event else None,
        snapshot(1, "date"),
        snapshot(1, "price"),
        snapshot(1, "exit_type"),
        snapshot(5, "date"),
        snapshot(5, "price"),
        snapshot(5, "exit_type"),
        snapshot(10, "date"),
        snapshot(10, "price"),
        snapshot(10, "exit_type"),
        snapshot(20, "date"),
        snapshot(20, "price"),
        snapshot(20, "exit_type"),
        returns[1],
        returns[5],
        returns[10],
        returns[20],
    )


def scan_security(
    window: str,
    code: str,
    records: Sequence[DailyRecord],
    metadata: Mapping[str, SecurityMeta],
    actions: Sequence[CorporateAction],
    test_start: str,
    test_end: str,
    *,
    costs: CostModel = CostModel(),
    max_initial_stop_distance_pct: float = 0.20,
    meaningful_pullback_atr: float = 1.0,
) -> tuple[list[DirectAdamRow], int, int]:
    """Return direct Adam rows, gate-qualified count, and skipped count."""
    meta = metadata.get(code)
    ordered = tuple(sorted(records, key=lambda item: item.trade_date))
    if meta is not None and meta.delist_date and meta.delist_date < test_start:
        return [], 0, 1
    state = _build_code_state(ordered, actions=actions)
    technical = state.technical_bars
    rows: list[DirectAdamRow] = []
    qualified = 0
    skipped = 0
    closes = [bar.close for bar in technical]
    for index, record in enumerate(state.records):
        trade_date = record.trade_date
        if trade_date < test_start or trade_date > test_end:
            continue
        if not is_a_share_universe_member(code, trade_date, meta):
            skipped += 1
            continue
        if index >= len(technical) or index + 1 >= len(state.records):
            skipped += 1
            continue
        try:
            gate = evaluate_entry_gate(technical[: index + 1])
        except ValueError:
            skipped += 1
            continue
        if not gate["qualified"]:
            continue
        qualified += 1
        next_record = state.records[index + 1]
        atr = state.atr_values[index]
        low_pivots = [
            pivot
            for pivot in reversed(state.pivots)
            if pivot.kind == "low"
            and pivot.confirmation_index <= index
            and pivot.pivot_index < index
        ]
        if atr is None or not low_pivots:
            skipped += 1
            continue
        low = low_pivots[0]
        projection = second_reflection(
            [bar.close for bar in technical[low.pivot_index : index + 1]]
        )
        plan = build_entry_plan(
            technical[index].close,
            atr,
            projection,
            meaningful_pullback_atr=meaningful_pullback_atr,
            require_meaningful_pullback=True,
        )
        if not plan.approved:
            rows.append(
                _row_from_event(
                    window,
                    code,
                    trade_date,
                    gate,
                    state,
                    index,
                    plan.pullback.depth_atr,
                    None,
                    None,
                    state.latest_stop_series[index],
                    None,
                    (
                        "rejected_no_1atr_pullback"
                        if not plan.pullback.meaningful
                        else "rejected_adam_plan"
                    ),
                )
            )
            continue
        factor = next_record.adj_factor
        lower_raw = plan.lower / factor if factor and factor > 0 else plan.lower
        upper_raw = plan.upper / factor if factor and factor > 0 else plan.upper
        initial = state.latest_stop_series[index]
        initial_entry_scale = (
            adjusted_stop_asof(initial, trade_date, next_record.trade_date, actions)
            if initial is not None
            else None
        )
        if initial_entry_scale is None or initial_entry_scale <= 0 or lower_raw <= initial_entry_scale:
            rows.append(
                _row_from_event(
                    window,
                    code,
                    trade_date,
                    gate,
                    state,
                    index,
                    plan.pullback.depth_atr,
                    lower_raw,
                    upper_raw,
                    initial_entry_scale,
                    None,
                    "rejected_invalid_initial_stop",
                )
            )
            continue
        distance = (lower_raw - initial_entry_scale) / lower_raw
        if distance > max_initial_stop_distance_pct:
            rows.append(
                _row_from_event(
                    window,
                    code,
                    trade_date,
                    gate,
                    state,
                    index,
                    plan.pullback.depth_atr,
                    lower_raw,
                    upper_raw,
                    initial_entry_scale,
                    distance,
                    "rejected_stop_distance_over_20pct",
                )
            )
            continue
        if not lower_raw <= next_record.bar.open <= upper_raw:
            rows.append(
                _row_from_event(
                    window,
                    code,
                    trade_date,
                    gate,
                    state,
                    index,
                    plan.pullback.depth_atr,
                    lower_raw,
                    upper_raw,
                    initial_entry_scale,
                    distance,
                    "unfilled_open_outside_zone",
                )
            )
            continue
        candidate = ProxyCandidate(trade_date, code, 0.0, 0.0, 0.0, 0, 0)
        event = _event_result(
            candidate,
            "adam_open_direct_all_a",
            state.records,
            index + 1,
            next_record.bar.open * (1.0 + costs.slippage_bps / 10000.0),
            initial,
            state.trailing_stop_series,
            costs,
            (1, 5, 10, 20),
            actions,
            lambda _raw_index: state,
        )
        rows.append(
            _row_from_event(
                window,
                code,
                trade_date,
                gate,
                state,
                index,
                plan.pullback.depth_atr,
                lower_raw,
                upper_raw,
                initial_entry_scale,
                distance,
                event.status,
                event,
            )
        )
    return rows, qualified, skipped


def write_rows(path: str | Path, rows: Sequence[DirectAdamRow]) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = list(DirectAdamRow.__dataclass_fields__)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            writer.writerow({field: getattr(row, field) for field in fields})
