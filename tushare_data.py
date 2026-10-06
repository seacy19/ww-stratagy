"""Read-only Tushare data adapter for the WW × Adam backtest.

The adapter intentionally does not decide WW candidates and does not write to
Tushare. It converts daily and minute responses into the local rule-layer data
structures while preserving the source timestamps.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from adam_execution import MinuteBar
from adam_gate import Bar
from pit_accounting import CorporateAction
from tushare_credentials import create_tushare_client


@dataclass(frozen=True)
class DailyRecord:
    trade_date: str
    bar: Bar
    amount: float
    pre_close: float | None
    adj_factor: float | None


class TushareDataClient:
    def __init__(self, token: str | None = None, *, pro: Any | None = None):
        if pro is None:
            try:
                pro = create_tushare_client(token)
            except ImportError as exc:  # pragma: no cover - environment setup
                raise RuntimeError("tushare is required for live data access") from exc
        self.pro = pro

    def daily_records(
        self,
        ts_code: str,
        start_date: str,
        end_date: str,
        *,
        include_adj_factor: bool = True,
    ) -> list[DailyRecord]:
        daily = self.pro.daily(
            ts_code=ts_code, start_date=start_date, end_date=end_date
        )
        if daily is None or len(daily) == 0:
            return []
        daily = daily.sort_values("trade_date").reset_index(drop=True)
        factors: dict[str, float] = {}
        if include_adj_factor:
            factor_frame = self.pro.adj_factor(
                ts_code=ts_code, start_date=start_date, end_date=end_date
            )
            if factor_frame is not None and len(factor_frame):
                factors = {
                    str(row.trade_date): float(row.adj_factor)
                    for row in factor_frame.itertuples()
                }

        records: list[DailyRecord] = []
        for row in daily.itertuples():
            records.append(
                DailyRecord(
                    trade_date=str(row.trade_date),
                    bar=Bar(
                        open=float(row.open),
                        high=float(row.high),
                        low=float(row.low),
                        close=float(row.close),
                        volume=float(row.vol),
                    ),
                    amount=float(row.amount),
                    pre_close=(
                        float(row.pre_close)
                        if row.pre_close is not None
                        else None
                    ),
                    adj_factor=factors.get(str(row.trade_date)),
                )
            )
        return records

    def minute_bars(
        self,
        ts_code: str,
        start_time: str,
        end_time: str,
        *,
        freq: str = "1min",
    ) -> list[MinuteBar]:
        frame = self.pro.stk_mins(
            ts_code=ts_code,
            start_date=start_time,
            end_date=end_time,
            freq=freq,
        )
        if frame is None or len(frame) == 0:
            return []
        frame = frame.sort_values("trade_time").reset_index(drop=True)
        result: list[MinuteBar] = []
        for row in frame.itertuples():
            timestamp = str(row.trade_time)
            hhmm = int(timestamp[11:13] + timestamp[14:16])
            result.append(
                MinuteBar(
                    time=hhmm,
                    open=float(row.open),
                    high=float(row.high),
                    low=float(row.low),
                    close=float(row.close),
                )
            )
        return result

    def dividend_actions(
        self, ts_code: str, start_date: str, end_date: str
    ) -> list[CorporateAction]:
        """Load implemented, dated corporate actions for point-in-time use."""
        # Tushare's dividend API filters on ann_date/record_date/ex_date,
        # not start_date/end_date. Fetch this security's action history, then
        # apply the requested ex-date window locally.
        frame = self.pro.dividend(ts_code=ts_code)
        if frame is None or len(frame) == 0:
            return []

        def clean(value: Any) -> str | None:
            text = "" if value is None else str(value)
            return None if text in {"", "nan", "NaT", "None"} else text

        actions: dict[tuple[str, str | None, float, float], CorporateAction] = {}
        for row in frame.itertuples():
            if str(getattr(row, "div_proc", "")) != "实施":
                continue
            ex_date = clean(getattr(row, "ex_date", None))
            if ex_date is None or not start_date <= ex_date <= end_date:
                continue
            # Tushare splits stock additions into bonus shares (送股) and
            # capitalisation/transfer shares (转增).  Both increase the
            # holder's share count and both must enter the post-ex-date price
            # scale.  The earlier loader kept only stk_bo_rate, which made a
            # pure 10转4 action look like a cash-only dividend.
            bonus = getattr(row, "stk_bo_rate", 0.0)
            transfer = getattr(row, "stk_co_rate", 0.0)
            cash = getattr(row, "cash_div", 0.0)
            tax = getattr(row, "cash_div_tax", 0.0)
            bonus_value = 0.0 if bonus is None or str(bonus) == "nan" else float(bonus)
            transfer_value = (
                0.0 if transfer is None or str(transfer) == "nan" else float(transfer)
            )
            stock_addition_value = bonus_value + transfer_value
            cash_value = 0.0 if cash is None or str(cash) == "nan" else float(cash)
            tax_value = 0.0 if tax is None or str(tax) == "nan" else float(tax)
            record_date = clean(getattr(row, "record_date", None))
            key = (ex_date, record_date, stock_addition_value, cash_value)
            actions[key] = CorporateAction(
                ts_code=ts_code,
                ex_date=ex_date,
                record_date=record_date,
                pay_date=clean(getattr(row, "pay_date", None)),
                announce_date=clean(getattr(row, "ann_date", None)),
                stock_bonus_ratio=stock_addition_value,
                cash_div_per_share=cash_value,
                cash_div_tax_per_share=tax_value,
            )
        return sorted(actions.values(), key=lambda item: item.ex_date)
