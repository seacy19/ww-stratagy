"""Local read-only inputs and fail-closed PIT evidence contracts."""
from __future__ import annotations

import csv
from datetime import datetime
import hashlib
import math
from pathlib import Path
import pickle


def digest(path):
    result = hashlib.sha256()
    with Path(path).open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            result.update(chunk)
    return result.hexdigest()


def load_inputs(config, root):
    """Pickles are only for this project's trusted historical local cache."""
    paths = {name: Path(root) / getattr(config.data, name)
             for name in ("prices", "metadata", "actions")}
    objects = {}
    manifest = {}
    for name, path in paths.items():
        with path.open("rb") as source:
            objects[name] = pickle.load(source)
        manifest[name] = {"path": str(path.resolve()), "sha256": digest(path)}
    records = {code: sorted(rows, key=lambda row: row.trade_date)
               for code, rows in objects["prices"].items() if code not in config.data.exclude_codes}
    manifest["explicit_exclusions"] = config.data.exclude_codes
    audit(records, objects["metadata"], objects["actions"], config)
    evidence = []
    if config.selection.mode == "ww":
        path = Path(root) / config.data.evidence
        evidence = read_evidence(path)
        manifest["evidence"] = {"path": str(path.resolve()), "sha256": digest(path)}
    return records, objects["metadata"], objects["actions"], evidence, manifest


def audit(records, metadata, actions, config):
    if not records:
        raise ValueError("empty price data")
    dates = set()
    for code, rows in records.items():
        if code not in metadata:
            raise ValueError(f"missing historical security metadata: {code}")
        seen = set()
        for row in rows:
            datetime.strptime(row.trade_date, "%Y%m%d")
            if row.trade_date in seen:
                raise ValueError(f"duplicate price: {code}/{row.trade_date}")
            seen.add(row.trade_date)
            dates.add(row.trade_date)
            b = row.bar
            numbers = [b.open, b.high, b.low, b.close, b.volume, row.amount]
            if not all(math.isfinite(x) for x in numbers):
                raise ValueError(f"nonfinite price data: {code}/{row.trade_date}")
            if min(b.open, b.low, b.close) <= 0 or b.high < max(b.open, b.close, b.low) or b.low > min(b.open, b.close):
                raise ValueError(f"invalid OHLC: {code}/{row.trade_date}")
            if min(b.volume, row.amount) < 0 or row.adj_factor is None or not math.isfinite(row.adj_factor) or row.adj_factor <= 0:
                raise ValueError(f"missing/invalid factor or volume: {code}/{row.trade_date}")
        # A factor jump without a corporate-action record cannot be accounted for.
        action_dates = {a.ex_date for a in actions.get(code, ())}
        for previous, current in zip(rows, rows[1:]):
            if config.data.start <= current.trade_date <= config.data.end and abs(current.adj_factor / previous.adj_factor - 1) > 1e-5:
                if not any(previous.trade_date < d <= current.trade_date for d in action_dates):
                    raise ValueError(f"unaccounted factor change: {code}/{current.trade_date}")
    if not any(config.data.start <= d <= config.data.end for d in dates):
        raise ValueError("no trading observations in requested period")


EVIDENCE_FIELDS = ("available_date", "expires_date", "ts_code", "mainline",
                   "mainline_score", "company_score", "top_down", "bottom_up",
                   "positive_cycle", "real_member", "invalidated", "source_url",
                   "source_sha256")


def read_evidence(path):
    with Path(path).open(encoding="utf-8-sig", newline="") as source:
        reader = csv.DictReader(source)
        if set(EVIDENCE_FIELDS) - set(reader.fieldnames or ()):
            raise ValueError(f"evidence CSV requires: {EVIDENCE_FIELDS}")
        rows = list(reader)
    if not rows:
        raise ValueError("WW evidence is empty; supply frozen, dated research records")
    seen = set()
    for row in rows:
        for key in ("available_date", "expires_date"):
            datetime.strptime(row[key], "%Y%m%d")
        if row["expires_date"] < row["available_date"]:
            raise ValueError("evidence expires before availability")
        key = row["available_date"], row["ts_code"], row["mainline"]
        if key in seen:
            raise ValueError(f"ambiguous evidence version: {key}")
        seen.add(key)
        for field in ("top_down", "bottom_up", "positive_cycle", "real_member", "invalidated"):
            if row[field] not in {"0", "1"}:
                raise ValueError(f"{field} must be 0 or 1")
        for field, maximum in (("mainline_score", 11), ("company_score", 1)):
            row[field] = float(row[field])
            if not 0 <= row[field] <= maximum:
                raise ValueError(f"invalid {field}")
        if not row["source_url"] or len(row["source_sha256"]) != 64 or any(c not in "0123456789abcdefABCDEF" for c in row["source_sha256"]):
            raise ValueError("evidence needs a source and SHA-256")
    return rows


def classify_evidence(rows, date, config):
    """Return frozen PIT candidate states before price execution filters.

    A top-down hit needs a live positive-cycle thesis and verified real member.
    A bottom-up hit needs an independently documented company inflection.
    Both states must be true for the intersection candidate under the default
    research rule. Single-leg cases remain visible for investigation.
    """
    latest = {}
    for row in rows:
        if row["available_date"] <= date:
            key = row["ts_code"], row["mainline"]
            if key not in latest or latest[key]["available_date"] < row["available_date"]:
                latest[key] = row
    statuses = {}
    for row in latest.values():
        age = (datetime.strptime(date, "%Y%m%d") - datetime.strptime(row["available_date"], "%Y%m%d")).days
        code = row["ts_code"]
        if row["expires_date"] < date or age > config.evidence_max_age_days:
            status = "stale_evidence"
        elif row["invalidated"] == "1":
            status = "invalidated"
        else:
            top_down = (row["top_down"] == "1" and row["positive_cycle"] == "1"
                        and row["real_member"] == "1"
                        and row["mainline_score"] >= config.min_mainline_score)
            bottom_up = (row["bottom_up"] == "1"
                         and row["company_score"] >= config.min_company_score)
            if top_down and bottom_up:
                status = "both_legs_intersection"
            elif top_down:
                status = "top_down_only_investigate"
            elif bottom_up:
                status = "bottom_up_only_investigate"
            else:
                status = "neither_leg_qualified"
        previous = statuses.get(code)
        if previous is None or row["company_score"] > previous["evidence"]["company_score"]:
            statuses[code] = {"status": status, "evidence": row}
    return statuses


def select_evidence(rows, date, config):
    """Select tradable WW members; diagnostics for single legs remain separate."""
    statuses = classify_evidence(rows, date, config)
    selected = {}
    for code, item in statuses.items():
        row = item["evidence"]
        if item["status"] == "both_legs_intersection":
            selected[code] = row
        elif (not config.require_both_legs and item["status"] in
              {"top_down_only_investigate", "bottom_up_only_investigate"}):
            selected[code] = row
    return selected
