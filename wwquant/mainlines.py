"""Load ex-post daily mainline labels and resolve their dated stock universe."""
from __future__ import annotations

import csv
import json
from collections import defaultdict
from datetime import datetime
from pathlib import Path

import pandas as pd


def _iso(value: str) -> str:
    value = str(value).strip()
    if len(value) == 8 and value.isdigit():
        return f"{value[:4]}-{value[4:6]}-{value[6:]}"
    datetime.strptime(value, "%Y-%m-%d")
    return value


def _members(value) -> list[str]:
    if isinstance(value, list):
        return [str(code).strip() for code in value if str(code).strip()]
    return [code.strip() for code in str(value or "").split(";") if code.strip()]


def read_labels(path: str | Path, calendar_path: str | Path,
                start: str, end: str) -> list[dict]:
    """Read interval JSON or expanded CSV and return labels on open sessions."""
    source = Path(path)
    calendar = pd.read_csv(calendar_path, dtype={"cal_date": str}, encoding="utf-8-sig")
    dates = sorted(calendar.loc[
        calendar["exchange"].eq("SSE") & calendar["is_open"].astype(str).eq("1")
        & calendar["cal_date"].between(start, end), "cal_date"
    ].astype(str).unique())
    if not dates:
        raise ValueError("No SSE open sessions in hindsight-mainline window")
    daily = defaultdict(list)
    if source.suffix.lower() == ".json":
        spec = json.loads(source.read_text(encoding="utf-8"))
        intervals = spec.get("intervals")
        if not isinstance(intervals, list):
            raise ValueError("mainline JSON must contain an intervals list")
        for item in intervals:
            first, last = _iso(item["start"]), _iso(item["end"])
            if first > last:
                raise ValueError("mainline interval end precedes start")
            if not item.get("sources"):
                raise ValueError("every mainline interval needs at least one source")
            for day in dates:
                date = f"{day[:4]}-{day[4:6]}-{day[6:]}"
                if first <= date <= last:
                    daily[day].append({
                        "date": day,
                        "mainline": str(item["mainline"]).strip(),
                        "mapping_type": str(item.get("mapping_type", "")).strip(),
                        "members": _members(item.get("members", [])),
                        "sources": item.get("sources_by_date", {}).get(date, item["sources"]),
                        "note": str(item.get("note", "")),
                    })
    else:
        with source.open(encoding="utf-8-sig", newline="") as handle:
            for row in csv.DictReader(handle):
                day = _iso(row["date"]).replace("-", "")
                if day in dates and row.get("status") == "labeled":
                    sources = [x for x in row.get("sources", "").split(";") if x]
                    if not sources:
                        raise ValueError(f"mainline row {day}/{row.get('mainline')} has no source")
                    daily[day].append({
                        "date": day,
                        "mainline": str(row.get("mainline", "")).strip(),
                        "mapping_type": str(row.get("mapping_type", "")).strip(),
                        "members": _members(row.get("members", "")),
                        "sources": sources,
                        "note": str(row.get("note", "")),
                    })
    labels = []
    for day in dates:
        labels.extend(daily.get(day, ()))
    if not labels:
        raise ValueError("No dated mainline labels overlap the requested window")
    return labels


def build_daily_universe(labels: list[dict], membership_path: str | Path,
                         mapping_path: str | Path) -> dict:
    """Resolve label rows to historical members, keeping provenance by day.

    Industry rules expand to every security whose historical interval is active
    on the label date. Explicit reported members are unioned into that set.
    When no industry rule exists, explicit names remain a partial universe.
    """
    rules = json.loads(Path(mapping_path).read_text(encoding="utf-8"))
    aliases = rules.get("aliases", {})
    industry_rules = rules.get("industry_rules", {})
    intervals = pd.read_parquet(membership_path)
    required = {"ts_code", "in_date", "out_date", "l1_name", "l2_name", "l3_name"}
    if required - set(intervals.columns):
        raise ValueError(f"historical membership lacks columns: {sorted(required-set(intervals.columns))}")
    intervals = intervals.copy()
    intervals["in_date"] = intervals["in_date"].fillna("").astype(str)
    intervals["out_date"] = intervals["out_date"].fillna("").astype(str)
    intervals["ts_code"] = intervals["ts_code"].astype(str)

    codes_by_date: dict[str, set[str]] = defaultdict(set)
    themes_by_date: dict[str, dict[str, set[str]]] = defaultdict(lambda: defaultdict(set))
    active_themes: dict[str, set[str]] = defaultdict(set)
    provenance: dict[str, list[dict]] = defaultdict(list)
    for label in labels:
        date = label["date"]
        original_name = label["mainline"]
        theme = aliases.get(original_name, original_name)
        spec = industry_rules.get(theme, {})
        codes = set(label["members"])
        coverage = "partial_reported_members" if codes else "unmapped"
        if spec:
            field = spec.get("field")
            values = set(spec.get("values", []))
            if field not in {"l1_name", "l2_name", "l3_name", "l1_code", "l2_code", "l3_code"} or not values:
                raise ValueError(f"invalid industry rule for mainline {theme}")
            active = intervals["in_date"].le(date) & (intervals["out_date"].eq("") | intervals["out_date"].gt(date))
            codes.update(intervals.loc[active & intervals[field].astype(str).isin(values), "ts_code"])
            coverage = f"historical_{field}"
        if not codes:
            raise ValueError(f"mainline {theme} on {date} has no industry mapping or explicit members")
        active_themes[date].add(theme)
        provenance[date].append({
            "mainline": theme,
            "coverage": coverage,
            "member_count": len(codes),
            "sources": label["sources"],
            "mapping_type": label["mapping_type"],
            "note": label["note"],
        })
        for code in codes:
            codes_by_date[date].add(code)
            themes_by_date[date][code].add(theme)
    return {
        "codes_by_date": {date: sorted(codes) for date, codes in codes_by_date.items()},
        "themes_by_date": {date: {code: sorted(themes) for code, themes in codes.items()}
                           for date, codes in themes_by_date.items()},
        "active_themes_by_date": {date: sorted(themes) for date, themes in active_themes.items()},
        "provenance_by_date": dict(provenance),
    }
