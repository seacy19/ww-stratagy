"""Expand sourced retrospective theme intervals over the local SSE calendar."""
from __future__ import annotations

import argparse
import csv
import json
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DEFAULT_INPUT = ROOT / "data/hindsight_mainlines/2019-04-01_2019-04-04.json"
CALENDAR = ROOT / "data/mainline_pit/trade_calendar.csv"


def expand(source: Path = DEFAULT_INPUT, calendar: Path = CALENDAR) -> list[dict]:
    spec = json.loads(source.read_text(encoding="utf-8"))
    with calendar.open(encoding="utf-8-sig", newline="") as handle:
        dates = [row["cal_date"] for row in csv.DictReader(handle)
                 if row["exchange"] == "SSE" and row["is_open"] == "1"
                 and spec["start"].replace("-", "") <= row["cal_date"] <= spec["end"].replace("-", "")]
    if not dates:
        raise ValueError("No open sessions in requested interval")
    by_date: dict[str, list[dict]] = defaultdict(list)
    for interval in spec["intervals"]:
        if not interval["sources"] or not interval["mapping_type"]:
            raise ValueError("Every label needs a source and mapping description")
        if interval["start"] > interval["end"]:
            raise ValueError("Reversed interval")
        for day in dates:
            date = f"{day[:4]}-{day[4:6]}-{day[6:]}"
            if interval["start"] <= date <= interval["end"]:
                by_date[date].append(interval)
    result = []
    for day in dates:
        date = f"{day[:4]}-{day[4:6]}-{day[6:]}"
        labels = by_date[date]
        if not labels:
            result.append(dict(date=date, status="missing", mainline="", mapping_type="",
                               members=[], sources=[], note="无已核实主线记录"))
        for item in labels:
            result.append(dict(date=date, status="labeled", mainline=item["mainline"],
                               mapping_type=item["mapping_type"], members=item["members"],
                               sources=item.get("sources_by_date", {}).get(date, item["sources"]),
                               note=item.get("note", "")))
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    args = parser.parse_args()
    writer = csv.DictWriter(__import__("sys").stdout, fieldnames=["date", "status", "mainline", "mapping_type", "members", "sources", "note"])
    writer.writeheader()
    for row in expand(args.input):
        writer.writerow({**row, "members": ";".join(row["members"]), "sources": ";".join(row["sources"])})
