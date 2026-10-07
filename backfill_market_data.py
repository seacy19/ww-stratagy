"""Resumable, per-session Tushare daily-price and adjustment-factor backfill.

Raw vendor responses are written to ignored ``proxy_cache/backfill_daily``;
existing research pickles are never modified by this utility.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import tempfile
import time
from typing import Any

from tushare_credentials import create_tushare_client


ROOT = Path(__file__).resolve().parent
DEFAULT_OUTPUT = ROOT / "proxy_cache" / "backfill_daily"


def _rows(frame: Any) -> list[dict[str, Any]]:
    if frame is None or len(frame) == 0:
        return []
    # Convert NumPy scalars to JSON-native values and missing values to null.
    values = frame.to_dict(orient="records")
    clean = []
    for row in values:
        item = {}
        for key, value in row.items():
            if value is None:
                item[str(key)] = None
            elif hasattr(value, "item"):
                value = value.item()
                item[str(key)] = value if isinstance(value, (int, float, str, bool)) else str(value)
            elif isinstance(value, float) and value != value:
                item[str(key)] = None
            else:
                item[str(key)] = value
        clean.append(item)
    return clean


def _atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def plan_sessions(pro: Any, start: str, end: str) -> list[str]:
    if len(start) != 8 or len(end) != 8 or start > end:
        raise ValueError("start/end must be valid YYYYMMDD dates with start <= end")
    calendar = pro.trade_cal(exchange="SSE", start_date=start, end_date=end)
    rows = _rows(calendar)
    return sorted({str(row["cal_date"]) for row in rows if str(row.get("is_open")) in {"1", "True", "true"}})


def fetch_session(pro: Any, trade_date: str) -> dict[str, Any]:
    daily_rows = _rows(pro.daily(trade_date=trade_date))
    factor_rows = _rows(pro.adj_factor(trade_date=trade_date))
    daily_by_code: dict[str, dict[str, Any]] = {}
    for row in daily_rows:
        code, date = str(row.get("ts_code", "")), str(row.get("trade_date", ""))
        if not code or date != trade_date:
            raise ValueError(f"unexpected daily response row for {trade_date}")
        if code in daily_by_code:
            raise ValueError(f"duplicate daily row {code}/{trade_date}")
        daily_by_code[code] = row
    factors: dict[str, float] = {}
    for row in factor_rows:
        code, date = str(row.get("ts_code", "")), str(row.get("trade_date", ""))
        if not code or date != trade_date:
            raise ValueError(f"unexpected factor response row for {trade_date}")
        if code in factors:
            raise ValueError(f"duplicate factor row {code}/{trade_date}")
        factor = float(row["adj_factor"])
        if factor <= 0:
            raise ValueError(f"invalid factor {code}/{trade_date}")
        factors[code] = factor
    missing = sorted(set(daily_by_code) - set(factors))
    if missing:
        raise ValueError(f"{trade_date}: {len(missing)} daily securities lack an adjustment factor")
    if not daily_rows:
        raise ValueError(f"{trade_date}: trading calendar says open but daily endpoint returned no rows")
    canonical = json.dumps(
        {"daily": daily_rows, "adj_factor": factor_rows},
        ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")
    return {
        "schema_version": 1,
        "source": "Tushare Pro official HTTPS",
        "trade_date": trade_date,
        "fetched_at_utc": datetime.now(timezone.utc).isoformat(),
        "daily_count": len(daily_rows),
        "factor_count": len(factor_rows),
        "daily_without_factor": [],
        "sha256_payload": hashlib.sha256(canonical).hexdigest(),
        "daily": daily_rows,
        "adj_factor": factor_rows,
    }


def run(start: str, end: str, output: Path, limit_days: int | None, execute: bool,
        pause_seconds: float = 0.25, client: Any | None = None) -> dict[str, Any]:
    pro = client or create_tushare_client()
    sessions = plan_sessions(pro, start, end)
    output.mkdir(parents=True, exist_ok=True)
    pending = [date for date in sessions if not (output / f"{date}.json").exists()]
    selected = pending if limit_days is None else pending[:limit_days]
    status = {
        "source": "Tushare Pro official HTTPS",
        "start": start,
        "end": end,
        "calendar_sessions": len(sessions),
        "already_completed": len(sessions) - len(pending),
        "pending_sessions": len(pending),
        "selected_sessions": selected,
        "mode": "execute" if execute else "dry_run",
        "completed": [],
        "errors": [],
    }
    if not execute:
        return status
    for date in selected:
        destination = output / f"{date}.json"
        try:
            record = fetch_session(pro, date)
            _atomic_json(destination, record)
            status["completed"].append({"trade_date": date, "daily_count": record["daily_count"],
                                        "factor_count": record["factor_count"],
                                        "sha256": record["sha256_payload"]})
        except Exception as exc:
            status["errors"].append({"trade_date": date, "error": f"{type(exc).__name__}: {exc}"})
            break
        _atomic_json(output / "progress.json", status)
        if pause_seconds:
            time.sleep(pause_seconds)
    _atomic_json(output / "progress.json", status)
    return status


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start", required=True, help="YYYYMMDD")
    parser.add_argument("--end", required=True, help="YYYYMMDD")
    parser.add_argument("--limit-days", type=int, help="cap this run; default fetches all pending dates")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--pause-seconds", type=float, default=0.25)
    parser.add_argument("--execute", action="store_true", help="write per-day raw JSON checkpoints")
    args = parser.parse_args()
    if args.limit_days is not None and args.limit_days < 1:
        parser.error("--limit-days must be positive")
    if args.pause_seconds < 0:
        parser.error("--pause-seconds must be nonnegative")
    try:
        result = run(args.start, args.end, args.output, args.limit_days, args.execute,
                     pause_seconds=args.pause_seconds)
    except (RuntimeError, ValueError, OSError) as exc:
        parser.exit(2, f"Error: {exc}\n")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if result["errors"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
