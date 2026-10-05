"""Strict, serializable configuration. Unknown parameters fail loudly."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field, fields
from datetime import datetime
import json
import math
from pathlib import Path

from adam_gate import AdamGateConfig


@dataclass
class DataConfig:
    prices: str = "proxy_cache/daily_v3_20240814_20250515.pkl"
    metadata: str = "proxy_cache/stock_metadata_v2.pkl"
    actions: str = "proxy_cache/actions_v4_pit_repaired_20181112_20250515.pkl"
    evidence: str = ""
    exclude_codes: list = field(default_factory=list)
    start: str = "20250102"
    end: str = "20250331"


@dataclass
class SelectionConfig:
    mode: str = "price_proxy"
    return_lookback: int = 60
    liquidity_lookback: int = 20
    top_return_fraction: float = 0.20
    top_liquidity_fraction: float = 0.50
    min_mainline_score: float = 7.0
    min_company_score: float = 0.60
    evidence_max_age_days: int = 45
    require_both_legs: bool = True


@dataclass
class SignalConfig:
    mode: str = "second_reflection"
    atr_period: int = 14
    reversal_atr: float = 2.0
    alpha: float = 0.0
    beta: float = 0.25
    safety_atr: float = 0.0
    meaningful_pullback_atr: float = 1.0
    require_meaningful_pullback: bool = True
    log_projection: bool = False
    stop_buffer: float = 0.01
    max_stop_distance: float = 0.20
    max_wait_sessions: int = 5
    reclaim_min_atr: float = 1.0
    reclaim_max_atr: float = 2.0
    reclaim_min_close_location: float = 0.70
    reclaim_min_volume_ratio: float = 1.5
    reclaim_alpha: float = 0.25


@dataclass
class PortfolioConfig:
    initial_cash: float = 1000000.0
    risk_per_trade: float = 0.005
    max_position_weight: float = 0.25
    max_gross_exposure: float = 0.80
    max_positions: int = 5
    lot_size: int = 100
    max_holding_sessions: int = 60
    trailing_stop: bool = True
    drawdown_halt: float = 0.15
    commission_bps: float = 3.0
    min_commission: float = 5.0
    sell_tax_bps: float = 5.0
    slippage_bps: float = 5.0


@dataclass
class Config:
    data: DataConfig = field(default_factory=DataConfig)
    selection: SelectionConfig = field(default_factory=SelectionConfig)
    gate: AdamGateConfig = field(default_factory=AdamGateConfig)
    signal: SignalConfig = field(default_factory=SignalConfig)
    portfolio: PortfolioConfig = field(default_factory=PortfolioConfig)

    def to_dict(self):
        return asdict(self)

    def validate(self):
        if any(not isinstance(code, str) or not code for code in self.data.exclude_codes):
            raise ValueError("data.exclude_codes must contain nonempty code strings")
        for section, values in self.to_dict().items():
            for key, value in values.items():
                if isinstance(value, (int, float)) and not isinstance(value, bool):
                    if not math.isfinite(value) or value < 0:
                        raise ValueError(f"{section}.{key}: must be finite and nonnegative")
        for section in (self.data,):
            for date in (section.start, section.end):
                if len(date) != 8:
                    raise ValueError("dates must use YYYYMMDD")
                datetime.strptime(date, "%Y%m%d")
            if section.start >= section.end:
                raise ValueError("start must precede end")
        if self.selection.mode not in {"price_proxy", "ww"}:
            raise ValueError("selection.mode must be price_proxy or ww")
        if self.selection.mode == "ww" and not self.data.evidence:
            raise ValueError("WW requires a dated evidence CSV; proxy substitution is forbidden")
        if self.signal.mode not in {"second_reflection", "pullback_reclaim", "gate_only"}:
            raise ValueError("unknown signal.mode")
        positive = [self.selection.return_lookback, self.selection.liquidity_lookback,
                    self.selection.evidence_max_age_days, self.signal.atr_period,
                    self.signal.reversal_atr, self.signal.max_wait_sessions,
                    self.portfolio.initial_cash, self.portfolio.max_positions,
                    self.portfolio.lot_size, self.portfolio.max_holding_sessions]
        positive += [getattr(self.gate, key) for key in
                     ("breakout_lookback", "volume_lookback", "fast_ema", "slow_ema",
                      "trend_breakout_lookback", "atr_lookback")]
        if min(positive) <= 0:
            raise ValueError("windows, counts, capital and reversal_atr must be positive")
        fractions = [self.selection.top_return_fraction, self.selection.top_liquidity_fraction,
                     self.signal.max_stop_distance, self.signal.reclaim_min_close_location,
                     self.gate.min_close_location, self.portfolio.risk_per_trade,
                     self.portfolio.max_position_weight, self.portfolio.max_gross_exposure,
                     self.portfolio.drawdown_halt]
        if any(not 0 < x <= 1 for x in fractions):
            raise ValueError("fractions must be in (0, 1]")
        if not 0 < self.signal.reclaim_min_atr <= self.signal.reclaim_max_atr:
            raise ValueError("invalid reclaim ATR range")
        if self.selection.min_mainline_score > 11 or self.selection.min_company_score > 1:
            raise ValueError("mainline score is 0..11; company score is 0..1")
        if self.gate.fast_ema >= self.gate.slow_ema:
            raise ValueError("fast_ema must be less than slow_ema")
        if max(self.portfolio.commission_bps, self.portfolio.sell_tax_bps,
               self.portfolio.slippage_bps) >= 10000:
            raise ValueError("costs must be less than 10000 bps")
        return self


def from_dict(raw):
    constructors = {"data": DataConfig, "selection": SelectionConfig,
                    "gate": AdamGateConfig, "signal": SignalConfig,
                    "portfolio": PortfolioConfig}
    if set(raw) - set(constructors):
        raise ValueError(f"unknown config sections: {set(raw) - set(constructors)}")
    sections = {}
    for name, constructor in constructors.items():
        values = raw.get(name, {})
        defaults = constructor()
        if not isinstance(values, dict) or set(values) - {f.name for f in fields(defaults)}:
            raise ValueError(f"unknown or invalid fields in {name}")
        for key, value in values.items():
            expected = type(getattr(defaults, key))
            valid = type(value) is expected
            if expected is float:
                valid = type(value) in (int, float)
            if not valid:
                raise ValueError(f"{name}.{key}: expected {expected.__name__}")
        sections[name] = constructor(**values)
    return Config(**sections).validate()


def load_config(path, overrides=()):
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    # Expand defaults first, so every documented key can be overridden.
    raw = from_dict(raw).to_dict()
    for override in overrides:
        key, value = override.split("=", 1)
        section, field_name = key.split(".", 1)
        if section not in raw or field_name not in raw[section]:
            raise ValueError(f"unknown parameter: {key}")
        raw[section][field_name] = json.loads(value)
    return from_dict(raw)
