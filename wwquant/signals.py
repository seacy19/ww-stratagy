"""End-of-day plans in that date's raw-price scale, built from causal prefixes."""
from __future__ import annotations

from dataclasses import dataclass

from adam_execution import (PullbackReclaimConfig, breakout_pullback_reclaim_plan,
                            build_entry_plan, confirmed_zigzag_pivots,
                            initial_stop, wilder_atr)
from adam_gate import Bar, evaluate_entry_gate, second_reflection


@dataclass
class Plan:
    signal_date: str
    code: str
    lower: float
    upper: float
    stop: float
    score: float
    mainline: str = "price_proxy"


def technical_bars(rows):
    denominator = rows[-1].adj_factor
    return [Bar(r.bar.open * r.adj_factor / denominator,
                r.bar.high * r.adj_factor / denominator,
                r.bar.low * r.adj_factor / denominator,
                r.bar.close * r.adj_factor / denominator, r.bar.volume)
            for r in rows]


def make_plan(code, rows, config, score=0.0, mainline="price_proxy"):
    s, g = config.signal, config.gate
    minimum = max(g.breakout_lookback + 1, g.slow_ema + 1,
                  g.trend_breakout_lookback + 2, g.atr_lookback + 2,
                  g.volume_lookback + 1, s.atr_period)
    if len(rows) < minimum:
        return None, "insufficient_history"
    bars = technical_bars(rows)
    atr_values = wilder_atr(bars, s.atr_period)
    atr = atr_values[-1]
    if atr is None or atr <= 0:
        return None, "invalid_atr"
    if s.mode == "pullback_reclaim":
        reclaim = breakout_pullback_reclaim_plan(bars, PullbackReclaimConfig(
            max_wait_sessions=s.max_wait_sessions, min_pullback_atr=s.reclaim_min_atr,
            max_pullback_atr=s.reclaim_max_atr,
            min_close_location=s.reclaim_min_close_location,
            min_volume_ratio=s.reclaim_min_volume_ratio, beta_atr=s.beta,
            alpha_atr=s.reclaim_alpha, stop_buffer=s.stop_buffer,
            max_stop_distance=s.max_stop_distance), gate_config=g, atr_period=s.atr_period)
        if not reclaim.approved:
            return None, reclaim.reason
        lower, upper, stop = reclaim.lower, reclaim.upper, reclaim.stop
    else:
        gate = evaluate_entry_gate(bars, g)
        if not gate["qualified"]:
            return None, "gate_rejected"
        stop = initial_stop(bars, len(bars) - 1, buffer=s.stop_buffer)
        if stop is None:
            return None, "no_confirmed_stop"
        if s.mode == "gate_only":
            lower, upper = bars[-1].close - s.beta * atr, bars[-1].close + s.alpha * atr
        else:
            pivots = [p for p in confirmed_zigzag_pivots(bars, atr_values, s.reversal_atr)
                      if p.kind == "low" and p.pivot_index < len(bars) - 1]
            if not pivots:
                return None, "no_confirmed_pivot"
            path = [b.close for b in bars[pivots[-1].pivot_index:]]
            projection = second_reflection(path, log_price=s.log_projection)
            entry = build_entry_plan(bars[-1].close, atr, projection,
                alpha=s.alpha, beta=s.beta, safety_atr=s.safety_atr,
                meaningful_pullback_atr=s.meaningful_pullback_atr,
                require_meaningful_pullback=s.require_meaningful_pullback)
            if not entry.approved:
                return None, entry.reason
            lower, upper = entry.lower, entry.upper
    if not 0 < stop < lower <= upper or (upper - stop) / upper > s.max_stop_distance:
        return None, "invalid_or_distant_stop"
    return Plan(rows[-1].trade_date, code, lower, upper, stop, score, mainline), "approved"
