"""Point-in-time price adjustment and raw-trade asset accounting.

Technical calculations and trading returns intentionally use different
representations:

* technical prices are rebuilt at each as-of date using only actions already
  effective/known by that date;
* trading returns use raw execution prices and an asset ledger that carries
  share additions and cash dividends.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Mapping, Sequence

from adam_gate import Bar


@dataclass(frozen=True)
class CorporateAction:
    ts_code: str
    ex_date: str
    record_date: str | None = None
    pay_date: str | None = None
    announce_date: str | None = None
    stock_bonus_ratio: float = 0.0
    # Tushare's cash_div is the per-share after-tax cash dividend; the
    # separate cash_div_tax field is the pre-tax reference value.
    cash_div_per_share: float = 0.0
    cash_div_tax_per_share: float = 0.0

    @property
    def net_cash_div_per_share(self) -> float:
        return self.cash_div_per_share


def repair_actions_from_factors(
    actions_by_code: Mapping[str, Sequence[CorporateAction]],
    records_by_code: Mapping[str, Sequence[object]],
) -> dict[str, tuple[CorporateAction, ...]]:
    """Fill missing transfer-share rates using the PIT adjustment-factor jump.

    Older cached Tushare dividend responses exposed ``stk_bo_rate`` but not
    ``stk_co_rate``.  A 10转4 event was therefore cached as a cash-only
    action, even though the daily adjustment factor changed accordingly.
    When an implemented cash action coincides with a factor jump, the missing
    stock-addition rate can be recovered without looking ahead:

        1 + bonus = (factor_after / factor_before)
                    * (close_before - cash) / close_before

    The factor on the ex-date and the previous raw close were observable at
    the time, so this repair remains point-in-time safe.  Existing non-zero
    stock additions are left untouched.
    """
    repaired: dict[str, tuple[CorporateAction, ...]] = {}
    for code, actions in actions_by_code.items():
        ordered = sorted(records_by_code.get(code, ()), key=lambda item: item.trade_date)
        fixed: list[CorporateAction] = []
        for action in actions:
            if action.stock_bonus_ratio > 1e-9:
                fixed.append(action)
                continue
            before = [item for item in ordered if item.trade_date < action.ex_date]
            after = [item for item in ordered if item.trade_date >= action.ex_date]
            if not before or not after:
                fixed.append(action)
                continue
            previous = before[-1]
            ex_day = after[0]
            f_before = getattr(previous, "adj_factor", None)
            f_after = getattr(ex_day, "adj_factor", None)
            close_before = float(previous.bar.close)
            cash = float(action.cash_div_per_share)
            if (
                f_before is None
                or f_after is None
                or f_before <= 0
                or f_after <= f_before * 1.001
                or close_before <= cash
            ):
                fixed.append(action)
                continue
            implied = (float(f_after) / float(f_before)) * (
                (close_before - cash) / close_before
            ) - 1.0
            # Ignore tiny factor rounding noise; genuine transfer/bonus rates
            # are materially above this threshold.
            if implied > 0.005:
                # Adjustment factors are rounded by the data vendor.  Most
                # Chinese equity distributions use 0.05-share increments
                # (10转4, 10送3.5, ...); snap only when the rounded value is
                # close enough, otherwise retain the measured value.
                standard = round(implied * 20.0) / 20.0
                repaired_ratio = (
                    standard if abs(standard - implied) <= 0.015 else round(implied, 6)
                )
                fixed.append(replace(action, stock_bonus_ratio=repaired_ratio))
            else:
                fixed.append(action)
        repaired[code] = tuple(fixed)
    return repaired


def _known_by(action: CorporateAction, asof_date: str) -> bool:
    return (
        action.ex_date <= asof_date
        and (action.announce_date is None or action.announce_date <= asof_date)
    )


def _apply_backward_adjustment(price: float, action: CorporateAction) -> float:
    factor = 1.0 + action.stock_bonus_ratio
    if factor <= 0:
        raise ValueError("stock adjustment factor must be positive")
    return (price - action.cash_div_per_share) / factor


def adjusted_bar_asof(
    bar: Bar,
    bar_date: str,
    asof_date: str,
    actions: Sequence[CorporateAction],
) -> Bar:
    """Return a continuous technical bar in the price scale valid at T.

    For a bar before an already-effective ex-date, the action is applied to
    its OHLC.  Actions after T are ignored.  The current bar on/after its
    ex-date is already quoted on the post-action scale and is not adjusted.
    Volume is not price-adjusted.
    """
    prices = [bar.open, bar.high, bar.low, bar.close]
    for action in sorted(actions, key=lambda item: item.ex_date):
        if not _known_by(action, asof_date) or bar_date >= action.ex_date:
            continue
        prices = [_apply_backward_adjustment(price, action) for price in prices]
    return Bar(*prices, volume=bar.volume)


def adjusted_bar_from_factor(bar: Bar, adj_factor: float | None) -> Bar:
    """Convert a raw bar using a per-date cumulative adjustment factor.

    This is equivalent to the action-chain representation when the factor is
    the factor published for that trade date.  It is safe for PIT use only
    after checking that the factor changes on/after the effective action date.
    """
    if adj_factor is None or adj_factor <= 0:
        return bar
    return Bar(
        bar.open * adj_factor,
        bar.high * adj_factor,
        bar.low * adj_factor,
        bar.close * adj_factor,
        bar.volume,
    )


def adjusted_bars_asof(
    records: Sequence[tuple[str, Bar]],
    asof_date: str,
    actions: Sequence[CorporateAction],
) -> tuple[Bar, ...]:
    return tuple(
        adjusted_bar_asof(bar, trade_date, asof_date, actions)
        for trade_date, bar in records
        if trade_date <= asof_date
    )


def adjusted_stop_asof(
    stop_price: float,
    from_date: str,
    asof_date: str,
    actions: Sequence[CorporateAction],
) -> float:
    """Move a stop from its old price scale into the current raw scale."""
    value = stop_price
    for action in sorted(actions, key=lambda item: item.ex_date):
        if from_date < action.ex_date <= asof_date and _known_by(action, asof_date):
            value = _apply_backward_adjustment(value, action)
    return value


def ledger_position_at_exit(
    quantity: int,
    entry_date: str,
    exit_date: str,
    actions: Sequence[CorporateAction],
) -> tuple[int, float]:
    """Return shares and net cash dividends entitled by the exit date."""
    shares = quantity
    cash = 0.0
    for action in sorted(actions, key=lambda item: item.ex_date):
        record_date = action.record_date or action.ex_date
        if not (entry_date <= record_date <= exit_date):
            continue
        if action.ex_date <= exit_date:
            cash += shares * action.net_cash_div_per_share
            shares = int(round(shares * (1.0 + action.stock_bonus_ratio)))
    return shares, cash


def ledger_return(
    quantity: int,
    entry_price: float,
    entry_date: str,
    exit_price: float,
    exit_date: str,
    actions: Sequence[CorporateAction] = (),
    *,
    buy_cost: float = 0.0,
    sell_cost: float = 0.0,
) -> float:
    if quantity <= 0 or entry_price <= 0:
        raise ValueError("quantity and entry_price must be positive")
    shares, dividend_cash = ledger_position_at_exit(
        quantity, entry_date, exit_date, actions
    )
    invested = quantity * entry_price + buy_cost
    exit_value = shares * exit_price + dividend_cash - sell_cost
    return exit_value / invested - 1.0
