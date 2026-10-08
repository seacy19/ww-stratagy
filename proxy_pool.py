"""Point-in-time objective price proxy pool.

This is deliberately not WW. It is a reproducible universe used to measure
the incremental effect of the Adam gate when historical WW candidate records
are unavailable.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
import json
from math import ceil
from pathlib import Path
from statistics import median
from typing import Mapping, Sequence

from tushare_data import DailyRecord
from pit_accounting import (
    CorporateAction,
    adjusted_bar_asof,
    adjusted_bar_from_factor,
)


@dataclass(frozen=True)
class SecurityMeta:
    ts_code: str
    list_date: str
    delist_date: str | None = None
    list_status: str | None = None
    # Half-open intervals (start_date inclusive, end_date exclusive) during
    # which the security name was ST/*ST.  These must come from historical
    # name-change records; the current name is not PIT-safe for backtests.
    st_periods: tuple[tuple[str, str | None], ...] = ()

    def is_st_on(self, trade_date: str) -> bool:
        return any(
            start <= trade_date and (end is None or trade_date < end)
            for start, end in self.st_periods
        )


def apply_metadata_aliases(metadata: Mapping[str, SecurityMeta], path: str | Path):
    """Overlay documented historical ticker aliases without mutating the cache."""
    alias_path = Path(path)
    if not alias_path.exists():
        return dict(metadata)
    raw = json.loads(alias_path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("security metadata aliases must be a JSON object")
    result = dict(metadata)
    for code, item in raw.items():
        if code in result:
            raise ValueError(f"metadata alias would overwrite existing security: {code}")
        if not isinstance(item, dict) or not item.get("alias_of") or not item.get("source"):
            raise ValueError(f"metadata alias {code} needs alias_of and source")
        list_date = str(item.get("list_date", ""))
        if len(list_date) != 8 or not list_date.isdigit():
            raise ValueError(f"metadata alias {code} needs YYYYMMDD list_date")
        delist_date = item.get("delist_date") or None
        if delist_date is not None:
            delist_date = str(delist_date)
            if len(delist_date) != 8 or not delist_date.isdigit():
                raise ValueError(f"metadata alias {code} has invalid delist_date")
        periods = tuple(
            (str(period[0]), str(period[1]) if period[1] else None)
            for period in item.get("st_periods", ())
        )
        result[code] = SecurityMeta(
            ts_code=code,
            list_date=list_date,
            delist_date=delist_date,
            list_status=item.get("list_status"),
            st_periods=periods,
        )
    return result


def is_a_share_universe_member(ts_code: str, trade_date: str,
                               meta: SecurityMeta | None = None) -> bool:
    """Check listing dates, ST state, and the historical BSE trading boundary."""
    if ts_code.endswith(".BJ") and trade_date < "20211115":
        return False
    if meta is None:
        return True
    if trade_date < meta.list_date:
        return False
    if meta.delist_date and trade_date >= meta.delist_date:
        return False
    if meta.is_st_on(trade_date):
        return False
    return True


@dataclass(frozen=True)
class ProxyCandidate:
    trade_date: str
    ts_code: str
    return_lookback: float
    sma_lookback: float
    median_amount: float
    return_rank: int
    liquidity_rank: int


def build_pit_proxy_candidates(
    records_by_code: Mapping[str, Sequence[DailyRecord]],
    metadata: Mapping[str, SecurityMeta],
    *,
    actions_by_code: Mapping[str, Sequence[CorporateAction]] | None = None,
    return_lookback: int = 60,
    liquidity_lookback: int = 20,
    top_return_fraction: float = 0.20,
    top_liquidity_fraction: float = 0.50,
    eligible_codes_by_date: Mapping[str, set[str] | Sequence[str]] | None = None,
) -> list[ProxyCandidate]:
    """Build daily candidates using only information available on each T.

    Rules:
    - at least ``return_lookback`` prior bars;
    - current close above the lookback SMA;
    - lookback return in the cross-sectional top fraction;
    - median traded amount in the cross-sectional top fraction;
    - listing and delisting dates respected.
    - securities named ST/*ST on T are excluded using PIT name-change intervals.
    """
    if return_lookback < 1 or liquidity_lookback < 1:
        raise ValueError("lookback values must be positive")
    if not 0 < top_return_fraction <= 1 or not 0 < top_liquidity_fraction <= 1:
        raise ValueError("top fractions must be in (0, 1]")

    metrics_by_date: dict[str, list[dict[str, object]]] = defaultdict(list)
    for ts_code, records in records_by_code.items():
        ordered = sorted(records, key=lambda record: record.trade_date)
        meta = metadata.get(ts_code)
        actions = () if actions_by_code is None else actions_by_code.get(ts_code, ())
        for index in range(return_lookback, len(ordered)):
            current = ordered[index]
            trade_date = current.trade_date
            if (eligible_codes_by_date is not None
                    and ts_code not in eligible_codes_by_date.get(trade_date, ())):
                continue
            if not is_a_share_universe_member(ts_code, trade_date, meta):
                continue
            if current.amount <= 0:
                continue
            return_window = ordered[index - return_lookback : index + 1]
            liquidity_window = ordered[
                max(0, index - liquidity_lookback + 1) : index + 1
            ]
            technical_window = [
                (
                    adjusted_bar_from_factor(record.bar, record.adj_factor)
                    if record.adj_factor is not None
                    else adjusted_bar_asof(
                        record.bar, record.trade_date, trade_date, actions
                    )
                )
                for record in return_window
            ]
            start_price = technical_window[0].close
            current_price = technical_window[-1].close
            if start_price <= 0 or current_price <= 0:
                continue
            return_lookback_value = current_price / start_price - 1.0
            sma = sum(item.close for item in technical_window) / len(technical_window)
            if current_price <= sma:
                continue
            median_amount = median(item.amount for item in liquidity_window)
            metrics_by_date[trade_date].append(
                {
                    "ts_code": ts_code,
                    "return_lookback": return_lookback_value,
                    "sma_lookback": sma,
                    "median_amount": median_amount,
                }
            )

    candidates: list[ProxyCandidate] = []
    for trade_date, metrics in metrics_by_date.items():
        return_order = sorted(
            metrics, key=lambda item: float(item["return_lookback"]), reverse=True
        )
        liquidity_order = sorted(
            metrics, key=lambda item: float(item["median_amount"]), reverse=True
        )
        return_rank = {
            str(item["ts_code"]): index + 1 for index, item in enumerate(return_order)
        }
        liquidity_rank = {
            str(item["ts_code"]): index + 1 for index, item in enumerate(liquidity_order)
        }
        return_cutoff = max(1, ceil(len(metrics) * top_return_fraction))
        liquidity_cutoff = max(1, ceil(len(metrics) * top_liquidity_fraction))
        for item in metrics:
            ts_code = str(item["ts_code"])
            if return_rank[ts_code] > return_cutoff:
                continue
            if liquidity_rank[ts_code] > liquidity_cutoff:
                continue
            candidates.append(
                ProxyCandidate(
                    trade_date=trade_date,
                    ts_code=ts_code,
                    return_lookback=float(item["return_lookback"]),
                    sma_lookback=float(item["sma_lookback"]),
                    median_amount=float(item["median_amount"]),
                    return_rank=return_rank[ts_code],
                    liquidity_rank=liquidity_rank[ts_code],
                )
            )
    return sorted(candidates, key=lambda item: (item.trade_date, item.ts_code))
