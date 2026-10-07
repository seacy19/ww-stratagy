"""Resumable Tushare dividend/action backfill for dates with factor changes."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from typing import Any

from backfill_market_data import _atomic_json, _rows
from tushare_credentials import create_tushare_client


ROOT = Path(__file__).resolve().parent
PRICE_BACKFILL = ROOT / "proxy_cache" / "backfill_daily"
PRICE_SHARDS = ROOT / "proxy_cache"
DEFAULT_OUTPUT = ROOT / "proxy_cache" / "backfill_dividends"


def factor_change_dates(start: str, end: str, price_dir: Path = PRICE_BACKFILL,
                        preceding_shard: Path | None = None) -> dict[str, list[str]]:
    """Return dates and affected securities, starting from the preceding cache edge."""
    import pickle

    previous: dict[str, tuple[str, float]] = {}
    if preceding_shard is not None:
        with preceding_shard.open("rb") as source:
            prior_records = pickle.load(source)
        for code, rows in prior_records.items():
            eligible = [row for row in rows if row.trade_date < start and row.adj_factor]
            if eligible:
                latest = max(eligible, key=lambda row: row.trade_date)
                previous[code] = (latest.trade_date, float(latest.adj_factor))
    else:
        # Locate the last cached observation for every security before this
        # backfill start. This supports later historical gaps without a
        # hard-coded predecessor filename.
        for shard in sorted(PRICE_SHARDS.glob("daily_v3_*.pkl")):
            with shard.open("rb") as source:
                cached = pickle.load(source)
            for code, rows in cached.items():
                for row in rows:
                    if row.trade_date < start and row.adj_factor:
                        old = previous.get(code)
                        if old is None or row.trade_date > old[0]:
                            previous[code] = (row.trade_date, float(row.adj_factor))
    changes: dict[str, list[str]] = {}
    for path in sorted(price_dir.glob("*.json")):
        date = path.stem
        if not (start <= date <= end):
            continue
        record = json.loads(path.read_text(encoding="utf-8"))
        current = {str(row["ts_code"]): float(row["adj_factor"]) for row in record["adj_factor"]}
        # Tushare's all-market factor endpoint commonly returns three decimal
        # places (while older cached rows can carry four). A 0.0005 absolute
        # delta may therefore be rounding alone. Treat only larger moves as
        # corporate-action lookup candidates; raw values remain untouched.
        changed = sorted(
            code for code, factor in current.items()
            if code in previous and abs(factor - previous[code][1]) > 0.00055
        )
        if changed:
            changes[date] = changed
        for code, factor in current.items():
            previous[code] = (date, factor)
    return changes


def fetch_date(pro: Any, date: str, changed_codes: list[str]) -> dict[str, Any]:
    rows = _rows(pro.dividend(ex_date=date))
    bad_dates = [row for row in rows if str(row.get("ex_date", "")) != date]
    if bad_dates:
        raise ValueError(f"dividend query returned rows outside ex-date {date}")
    if len(rows) >= 2000:
        raise ValueError(f"{date}: dividend result reached the documented 2000-row cap")
    implementations = [row for row in rows if row.get("div_proc") == "实施"]
    implementation_codes = {str(row.get("ts_code", "")) for row in implementations}
    matched = sorted(set(changed_codes) & implementation_codes)
    canonical = json.dumps(rows, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    return {
        "schema_version": 1,
        "source": "Tushare Pro official HTTPS dividend(ex_date=...) ",
        "ex_date": date,
        "fetched_at_utc": datetime.now(timezone.utc).isoformat(),
        "changed_factor_securities": changed_codes,
        "rows_count": len(rows),
        "implemented_count": len(implementations),
        "factor_changes_matched_to_implemented_action": matched,
        "factor_changes_without_implemented_action": sorted(set(changed_codes) - implementation_codes),
        "sha256_payload": hashlib.sha256(canonical).hexdigest(),
        "rows": rows,
    }


def run(start: str, end: str, output: Path, limit_days: int | None, execute: bool,
        pause_seconds: float = 0.25, client: Any | None = None,
        price_dir: Path = PRICE_BACKFILL, preceding_shard: Path | None = None) -> dict[str, Any]:
    pro = client or create_tushare_client()
    dates = factor_change_dates(start, end, price_dir, preceding_shard)
    # Query every available market session, not just sessions with material
    # factor moves: small cash actions can be hidden by vendor factor rounding.
    sessions = sorted(
        path.stem for path in price_dir.glob("*.json")
        if path.stem.isdigit() and start <= path.stem <= end
    )
    output.mkdir(parents=True, exist_ok=True)
    pending = [date for date in sessions if not (output / f"{date}.json").exists()]
    selected = pending if limit_days is None else pending[:limit_days]
    status = {
        "source": "Tushare Pro official HTTPS",
        "start": start,
        "end": end,
        "market_sessions": len(sessions),
        "material_factor_change_dates": len(dates),
        "already_completed": len(sessions) - len(pending),
        "pending_dates": len(pending),
        "selected_dates": selected,
        "mode": "execute" if execute else "dry_run",
        "completed": [],
        "errors": [],
    }
    if not execute:
        return status
    import time

    for date in selected:
        try:
            item = fetch_date(pro, date, dates.get(date, []))
            _atomic_json(output / f"{date}.json", item)
            status["completed"].append({
                "ex_date": date,
                "factor_change_codes": len(dates.get(date, [])),
                "action_rows": item["rows_count"],
                "matched": len(item["factor_changes_matched_to_implemented_action"]),
                "unmatched": len(item["factor_changes_without_implemented_action"]),
                "sha256": item["sha256_payload"],
            })
        except Exception as exc:
            status["errors"].append({"ex_date": date, "error": f"{type(exc).__name__}: {exc}"})
            break
        _atomic_json(output / "progress.json", status)
        if pause_seconds:
            time.sleep(pause_seconds)
    _atomic_json(output / "progress.json", status)
    return status


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start", default="20200518")
    parser.add_argument("--end", default="20211111")
    parser.add_argument("--limit-days", type=int)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--pause-seconds", type=float, default=0.25)
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args()
    if args.limit_days is not None and args.limit_days < 1:
        parser.error("--limit-days must be positive")
    if args.pause_seconds < 0:
        parser.error("--pause-seconds must be nonnegative")
    try:
        result = run(args.start, args.end, args.output, args.limit_days, args.execute,
                     args.pause_seconds)
    except (RuntimeError, ValueError, OSError) as exc:
        parser.exit(2, f"Error: {exc}\n")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if result["errors"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
