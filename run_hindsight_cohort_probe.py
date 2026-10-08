"""Retrospective price-engine probe on a fixed 2019-04-01 theme cohort.

This is intentionally NOT a point-in-time WW backtest. The candidate cohort
was assembled from public theme coverage and manually frozen later; group
returns are forward-looking outcome diagnostics. The strategy engine itself
still reads only causal price prefixes and keeps its execution/cost rules.
"""
from __future__ import annotations

import hashlib
import json
import pickle
from pathlib import Path

import pandas as pd

from proxy_pool import apply_metadata_aliases
from wwquant.config import from_dict, load_config
from wwquant.engine import run


ROOT = Path(__file__).resolve().parent
PACK = ROOT / "data/mainline_pit/evidence_packs/2019-04-01"
PRICE_PATH = ROOT / "proxy_cache/daily_v3_20181112_20190814.pkl"
META_PATH = ROOT / "proxy_cache/stock_metadata_v2.pkl"
ACTION_PATH = ROOT / "proxy_cache/actions_v4_pit_repaired_20181112_20250515.pkl"
COHORT_PATH = PACK / "candidate_profile.csv"
ANCHOR = "20190401"
ENTRY = "20190402"
HORIZONS = (5, 10, 20, 40, 60)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def hindsight_returns(prices, cohort: pd.DataFrame) -> list[dict]:
    sessions = sorted(
        {row.trade_date for rows in prices.values() for row in rows}
    )
    first = sessions.index(ENTRY)
    outputs = []
    for horizon in HORIZONS:
        exit_date = sessions[first + horizon - 1]
        for theme, group in cohort.groupby("theme", sort=True):
            returns = []
            missing = []
            for code in sorted(group.ts_code.astype(str)):
                rows = {row.trade_date: row for row in prices.get(code, ())}
                entry = rows.get(ENTRY)
                eligible_exit = [date for date in rows if date <= exit_date]
                if entry is None or not eligible_exit:
                    missing.append(code)
                    continue
                exit_row = rows[max(eligible_exit)]
                if entry.adj_factor is None or exit_row.adj_factor is None:
                    missing.append(code)
                    continue
                entry_value = entry.bar.open * entry.adj_factor
                exit_value = exit_row.bar.close * exit_row.adj_factor
                returns.append(exit_value / entry_value - 1)
            outputs.append(
                {
                    "horizon_sessions": horizon,
                    "exit_session": exit_date,
                    "theme": theme,
                    "candidate_count": len(group),
                    "observed_count": len(returns),
                    "missing_count": len(missing),
                    "equal_weight_return": sum(returns) / len(returns) if returns else None,
                    "median_return": float(pd.Series(returns).median()) if returns else None,
                    "positive_count": sum(value > 0 for value in returns),
                }
            )
    return outputs


def strategy_probe(prices, metadata, actions, cohort: pd.DataFrame) -> list[dict]:
    raw = load_config(ROOT / "configs/ww_price_proxy.json").to_dict()
    raw["data"].update(start=ENTRY, end="20190628")
    raw["selection"].update(mode="price_proxy")
    config = from_dict(raw)
    outputs = []
    for theme, group in cohort.groupby("theme", sort=True):
        codes = set(group.ts_code.astype(str))
        records = {code: rows for code, rows in prices.items() if code in codes}
        result = run(config, records, metadata, actions)
        summary = result["summary"]
        outputs.append(
            {
                "theme": theme,
                "candidate_count": len(codes),
                "bars": sum(map(len, records.values())),
                "plans": len(result["plans"]),
                "buys": sum(trade["side"] == "buy" for trade in result["trades"]),
                "sells": sum(trade["side"] == "sell" for trade in result["trades"]),
                "closed_trades": summary["closed_trades"],
                "total_return": summary["total_return"],
                "max_drawdown": summary["max_drawdown"],
                "decision_counts": summary["decision_counts"],
            }
        )
    return outputs


def main() -> None:
    prices = pickle.loads(PRICE_PATH.read_bytes())
    metadata = apply_metadata_aliases(
        pickle.loads(META_PATH.read_bytes()), ROOT / "configs/security_metadata_aliases.json"
    )
    actions = pickle.loads(ACTION_PATH.read_bytes())
    cohort = pd.read_csv(COHORT_PATH)
    if cohort.ts_code.duplicated().any() or cohort.anchor_date.ne("2019-04-01").any():
        raise ValueError("cohort must contain unique securities for the 2019-04-01 anchor")

    output = {
        "experiment": "retrospective_fixed_cohort_price_probe",
        "anchor_date": ANCHOR,
        "entry_date": ENTRY,
        "forward_group_returns": hindsight_returns(prices, cohort),
        "causal_price_engine_by_theme": strategy_probe(prices, metadata, actions, cohort),
        "input_sha256": {
            str(path.relative_to(ROOT)): sha256(path)
            for path in (PRICE_PATH, META_PATH, ACTION_PATH, COHORT_PATH)
        },
        "interpretation": [
            "Theme cohort labels are historical research selections and are not point-in-time WW evidence.",
            "Forward group returns are outcome diagnostics and intentionally use future prices.",
            "The trading engine uses causal price prefixes, with unchanged daily-open execution and cost assumptions.",
            "The 35-name news cohort is a small, manually curated candidate set, not a complete theme universe.",
        ],
    }
    print(json.dumps(output, ensure_ascii=False, indent=2, allow_nan=False))


if __name__ == "__main__":
    main()
