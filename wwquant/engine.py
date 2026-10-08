"""Daily research portfolio simulator, with explicit execution approximations.

Decisions at close become one-session open-only orders. No broker operations.
Daily OHLC cannot establish minute re-entry, queues, or actual fill liquidity.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import asdict
from math import floor, sqrt
from statistics import mean, stdev

from adam_execution import initial_stop
from pit_accounting import adjusted_stop_asof
from proxy_pool import build_pit_proxy_candidates
from wwquant.data import classify_evidence, select_evidence
from wwquant.signals import make_plan, technical_bars


def metrics(curve, trades, initial):
    equity = [initial] + [row["equity"] for row in curve]
    returns = [b / a - 1 for a, b in zip(equity, equity[1:]) if a > 0]
    peak, max_dd = initial, 0.0
    for value in equity:
        peak = max(peak, value)
        max_dd = max(max_dd, 1 - value / peak)
    sells = [t for t in trades if t["side"] == "sell"]
    return {"total_return": equity[-1] / initial - 1,
            "annualized_return": (equity[-1] / initial) ** (252 / len(returns)) - 1 if returns and equity[-1] > 0 else None,
            "max_drawdown": max_dd,
            "sharpe_zero_rf": mean(returns) / stdev(returns) * sqrt(252) if len(returns) > 1 and stdev(returns) > 0 else None,
            "closed_trades": len(sells),
            "win_rate": sum(t["pnl"] > 0 for t in sells) / len(sells) if sells else None,
            "fees_paid": sum(t["fee"] for t in trades),
            "sessions": len(curve)}


def run(config, records, metadata, actions, evidence=()):
    config.validate()
    p = config.portfolio
    days = sorted({r.trade_date for rows in records.values() for r in rows
                   if config.data.start <= r.trade_date <= config.data.end})
    if not days:
        raise ValueError("empty run window")
    index = {code: {r.trade_date: i for i, r in enumerate(rows)} for code, rows in records.items()}
    candidates = defaultdict(dict)
    if config.selection.mode == "price_proxy":
        s = config.selection
        pool = build_pit_proxy_candidates(records, metadata,
                return_lookback=s.return_lookback, liquidity_lookback=s.liquidity_lookback,
                top_return_fraction=s.top_return_fraction,
                top_liquidity_fraction=s.top_liquidity_fraction)
        for candidate in pool:
            if days[0] <= candidate.trade_date <= days[-1]:
                candidates[candidate.trade_date][candidate.ts_code] = {
                    "score": candidate.return_lookback, "mainline": "price_proxy"}
    elif config.selection.mode == "hindsight_mainline":
        if not isinstance(evidence, dict) or not evidence.get("codes_by_date"):
            raise ValueError("hindsight_mainline requires resolved dated mainline membership")
        s = config.selection
        eligible = {date: set(codes) for date, codes in evidence["codes_by_date"].items()}
        pool = build_pit_proxy_candidates(records, metadata,
                return_lookback=s.return_lookback, liquidity_lookback=s.liquidity_lookback,
                top_return_fraction=s.top_return_fraction,
                top_liquidity_fraction=s.top_liquidity_fraction,
                eligible_codes_by_date=eligible)
        for candidate in pool:
            if days[0] <= candidate.trade_date <= days[-1]:
                themes = evidence["themes_by_date"].get(candidate.trade_date, {}).get(candidate.ts_code, ())
                candidates[candidate.trade_date][candidate.ts_code] = {
                    "score": candidate.return_lookback,
                    "mainline": "|".join(themes),
                }
    elif not evidence:
        raise ValueError("WW cannot run without frozen evidence")

    cash, peak = p.initial_cash, p.initial_cash
    positions, pending = {}, []
    curve, trades, plans, decisions = [], [], [], []
    halted = False
    receivables = []
    entitlements = {}
    action_events = []
    for code, values in actions.items():
        if code not in records:
            continue
        for n, action in enumerate(values):
            if days[0] <= action.ex_date <= days[-1]:
                action_events.append((code, n, action))
    applied = set()

    def fee(value, sell=False):
        return max(p.min_commission, value * p.commission_bps / 10000) + (value * p.sell_tax_bps / 10000 if sell else 0)

    def nav():
        return cash + sum(pos["shares"] * pos["mark"] for pos in positions.values()) + sum(r["amount"] for r in receivables)

    for day_number, date in enumerate(days):
        # Corporate actions: use record-close entitlements, ex-date bonus, and
        # pay-date cash. Missing record/pay dates explicitly use ex-date proxy.
        for code, n, action in action_events:
            key = code, n
            if action.ex_date <= date and key not in applied:
                applied.add(key)
                quantity = entitlements.get(key, positions.get(code, {}).get("shares", 0) if not action.record_date else 0)
                pos = positions.get(code)
                if pos:
                    pos["mark"] = max(0.0, (pos["mark"] - action.cash_div_tax_per_share) / (1 + action.stock_bonus_ratio))
                    pos["stop"] = adjusted_stop_asof(pos["stop"], pos["stop_date"], date, (action,))
                    pos["stop_date"] = date
                if quantity:
                    if action.stock_bonus_ratio:
                        # Listing date is absent in the legacy action schema.
                        # This is an ex-date availability approximation.
                        if pos is None:
                            raise ValueError(f"bonus entitlement after sale needs share-listing ledger: {code}/{date}")
                        pos["shares"] += quantity * action.stock_bonus_ratio
                    dividend = quantity * action.net_cash_div_per_share
                    if dividend:
                        receivables.append({"pay_date": action.pay_date or action.ex_date, "amount": dividend})
                        if pos:
                            pos["dividends"] += dividend
        due = [r for r in receivables if r["pay_date"] <= date]
        cash += sum(r["amount"] for r in due)
        receivables = [r for r in receivables if r["pay_date"] > date]

        # Mark to OPEN before any sizing. Today's close must never size an order.
        for code, pos in positions.items():
            i = index[code].get(date)
            if i is not None:
                pos["mark"] = records[code][i].bar.open

        def sell(code, raw_price, reason):
            nonlocal cash
            pos = positions.pop(code)
            price = raw_price * (1 - p.slippage_bps / 10000)
            value = price * pos["shares"]
            cost = fee(value, True)
            cash += value - cost
            trades.append({"date": date, "code": code, "side": "sell", "price": price,
                           "shares": pos["shares"], "fee": cost, "reason": reason,
                           "pnl": value - cost + pos["dividends"] - pos["basis"]})

        # Only open exits can release cash for open entries. Intraday stop
        # proceeds are deliberately processed AFTER the open entry batch.
        for code, pos in list(positions.items()):
            i = index[code].get(date)
            if i is None:
                continue
            bar = records[code][i].bar
            if bar.volume <= 0 or bar.high == bar.low:
                continue
            reason = None
            if bar.open <= pos["stop"]:
                reason = "stop_gap"
            elif day_number - pos["entry_session"] >= p.max_holding_sessions:
                reason = "time_exit"
            elif pos.get("logic_exit"):
                reason = pos.get("logic_exit_reason", "evidence_exit")
            if reason:
                sell(code, bar.open, reason)

        for plan in sorted(pending, key=lambda item: (-item.score, item.code)):
            code = plan.code
            i = index[code].get(date)
            reason = None
            meta = metadata[code]
            if code in positions:
                reason = "already_held"
            elif halted:
                reason = "drawdown_halt"
            elif len(positions) >= p.max_positions:
                reason = "position_count_limit"
            elif (config.selection.mode == "hindsight_mainline"
                  and not set(plan.mainline.split("|")).intersection(
                      evidence["active_themes_by_date"].get(date, ()))):
                reason = "mainline_expired_before_execution"
            elif meta.is_st_on(date) or date < meta.list_date or (meta.delist_date and date >= meta.delist_date):
                reason = "ineligible_security"
            elif i is None:
                reason = "no_next_session_bar_order_expired"
            if reason:
                decisions.append({"date": date, "code": code, "stage": "execution", "reason": reason})
                continue
            bar = records[code][i].bar
            relevant = actions.get(code, ())
            lower = adjusted_stop_asof(plan.lower, plan.signal_date, date, relevant)
            upper = adjusted_stop_asof(plan.upper, plan.signal_date, date, relevant)
            stop = adjusted_stop_asof(plan.stop, plan.signal_date, date, relevant)
            price = bar.open * (1 + p.slippage_bps / 10000)
            if bar.volume <= 0 or bar.high == bar.low:
                reason = "zero_volume_or_flat_bar"
            elif not lower <= bar.open <= upper:
                reason = "open_outside_zone"
            elif price <= stop or (price - stop) / price > config.signal.max_stop_distance:
                reason = "fill_risk_exceeds_limit"
            else:
                equity = nav()
                gross = sum(pos["shares"] * pos["mark"] for pos in positions.values())
                budget = min(cash, equity * p.max_position_weight,
                             max(0, equity * p.max_gross_exposure - gross))
                risk_shares = equity * p.risk_per_trade / (price - stop)
                shares = floor(min(budget / price, risk_shares) / p.lot_size) * p.lot_size
                while shares > 0 and shares * price + fee(shares * price) > budget:
                    shares -= p.lot_size
                if shares <= 0:
                    reason = "insufficient_cash_or_risk_budget"
                else:
                    cost = fee(shares * price)
                    basis = shares * price + cost
                    cash -= basis
                    positions[code] = {"shares": shares, "mark": bar.open,
                        "stop": stop, "stop_date": date, "basis": basis,
                        "entry_session": day_number, "dividends": 0.0,
                        "mainline": plan.mainline}
                    trades.append({"date": date, "code": code, "side": "buy", "price": price,
                        "shares": shares, "fee": cost, "reason": config.signal.mode, "pnl": None})
                    reason = "filled"
            decisions.append({"date": date, "code": code, "stage": "execution", "reason": reason})

        for code, pos in list(positions.items()):
            i = index[code].get(date)
            if i is None:
                continue
            bar = records[code][i].bar
            if pos["entry_session"] < day_number and bar.volume > 0 and bar.high > bar.low and bar.low <= pos["stop"]:
                sell(code, min(bar.open, pos["stop"]), "intraday_stop_daily_proxy")
                continue
            pos["mark"] = bar.close
            if p.trailing_stop:
                prefix = records[code][:i + 1]
                stop = initial_stop(technical_bars(prefix), i, config.signal.stop_buffer)
                if stop is not None:
                    pos["stop"] = max(pos["stop"], stop)
                pos["stop_date"] = date

        # Save record-date entitlements at the close, after today's executions.
        for code, n, action in action_events:
            if action.record_date == date:
                entitlements[(code, n)] = positions.get(code, {}).get("shares", 0)
        equity = nav()
        peak = max(peak, equity)
        halted = halted or 1 - equity / peak >= p.drawdown_halt
        curve.append({"date": date, "equity": equity, "cash": cash,
                      "receivables": sum(r["amount"] for r in receivables),
                      "positions": len(positions), "drawdown": 1 - equity / peak,
                      "halted": halted,
                      "stale_positions": sum(date not in index[c] for c in positions)})
        if cash < -1e-6:
            raise AssertionError("negative cash")

        if config.selection.mode == "ww":
            leg_states = classify_evidence(evidence, date, config.selection)
            for code, item in sorted(leg_states.items()):
                decisions.append({"date": date, "code": code, "stage": "dual_leg",
                                  "reason": item["status"]})
            selected = select_evidence(evidence, date, config.selection)
            todays = {code: {"score": row["company_score"], "mainline": row["mainline"]}
                      for code, row in selected.items()}
            for code, pos in positions.items():
                pos["logic_exit"] = code not in selected
        elif config.selection.mode == "hindsight_mainline":
            todays = candidates.get(date, {})
            active_themes = set(evidence["active_themes_by_date"].get(date, ()))
            for code, pos in positions.items():
                held_themes = set(pos.get("mainline", "").split("|"))
                left_mainline = not held_themes.intersection(active_themes)
                pos["logic_exit"] = left_mainline
                pos["logic_exit_reason"] = "mainline_exit" if left_mainline else ""
        else:
            todays = candidates.get(date, {})
        pending = []
        for code, item in sorted(todays.items()):
            if code not in records or date not in index[code]:
                continue
            meta = metadata[code]
            if meta.is_st_on(date) or date < meta.list_date or (meta.delist_date and date >= meta.delist_date):
                continue
            prefix = records[code][:index[code][date] + 1]
            plan, reason = make_plan(code, prefix, config, **item)
            decisions.append({"date": date, "code": code, "stage": "signal", "reason": reason})
            if plan:
                pending.append(plan)
                plans.append(asdict(plan))
    summary = metrics(curve, trades, p.initial_cash)
    summary.update({"selection_mode": config.selection.mode, "signal_mode": config.signal.mode,
        "open_positions": len(positions), "unexecuted_final_plans": len(pending),
        "halted": halted, "decision_counts": dict(Counter(r["reason"] for r in decisions)),
        "execution_model": "daily_open_only_research_proxy",
        "limitations": ["只有日线数据，无法还原涨跌停排队和盘中回到买入区间的过程；全天价格不变的股票按无法成交处理。",
                        "旧公司行动数据缺少送转股上市日期；本研究暂按除权日即可使用新增股份计算。",
                        "缺少股权登记日或派息日时，暂用除权日估算；交易费用是实验假设，并非逐年适用的真实费率。",
                        "个股缺少行情时沿用上次价格估值；尚未模拟退市清算，也不假设期末强制平仓。",
                        "交易日历由现有行情日期合并生成；全市场是否缺少某个交易日，还需用独立日历核验。",
                        "主线和公司评分是事先保存的研究输入，尚未建立自动核验的基本面评分模型。"]})
    if config.selection.mode == "hindsight_mainline":
        summary["mainline_label_mode"] = "retrospective_daily_labels"
        summary["mainline_exit_rule"] = "exit_next_open_when_held_theme_is_no_longer_active"
        summary["limitations"].append("主线标签是事后整理的；其成员范围按历史申万行业或报道列举的部分名单映射，不代表点时可知的完整题材成员。")
    return {"summary": summary, "equity": curve, "trades": trades, "plans": plans,
            "decisions": decisions, "positions": positions}
