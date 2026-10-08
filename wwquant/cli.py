"""python3 -m wwquant.cli {init,audit,run,sweep} ..."""
from __future__ import annotations

import argparse
import csv
from datetime import datetime, timezone
import hashlib
import itertools
import json
from pathlib import Path

from wwquant.config import Config, from_dict, load_config
from wwquant.data import EVIDENCE_FIELDS, load_inputs
from wwquant.engine import run

ROOT = Path(__file__).resolve().parents[1]


def write_json(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")


def write_csv(path, rows, empty_fields=()):
    keys = list(rows[0]) if rows else list(empty_fields)
    with Path(path).open("w", encoding="utf-8-sig", newline="") as output:
        writer = csv.DictWriter(output, fieldnames=keys)
        writer.writeheader()
        writer.writerows(rows)


def save_run(directory, config, result, manifest):
    directory.mkdir(parents=True, exist_ok=False)
    write_json(directory / "config.json", config.to_dict())
    write_json(directory / "summary.json", result["summary"])
    write_json(directory / "positions.json", result["positions"])
    source_files = list((ROOT / "wwquant").glob("*.py")) + [ROOT / name for name in
                    ("adam_gate.py", "adam_execution.py", "proxy_pool.py", "pit_accounting.py", "tushare_data.py")]
    source_hashes = {str(path.relative_to(ROOT)): hashlib.sha256(path.read_bytes()).hexdigest() for path in source_files}
    write_json(directory / "manifest.json", {"created_at": datetime.now(timezone.utc).isoformat(),
        "inputs": manifest, "source_sha256": source_hashes,
        "config_sha256": hashlib.sha256(json.dumps(config.to_dict(), sort_keys=True).encode()).hexdigest()})
    for name in ("equity", "trades", "plans", "decisions"):
        write_csv(directory / f"{name}.csv", result[name])
    s = result["summary"]
    report = ["# WW 量化研究运行报告", "", f"模式：{s['selection_mode']} / {s['signal_mode']}"]
    if s.get("mainline_label_mode") == "retrospective_daily_labels":
        report.extend(["", "主线标签：事后整理的逐日标签；全量行业按历史申万成员映射，未映射主题仅用来源列举的部分成员。",
                       "筛选口径：在当日主线成员并集内计算历史强度、流动性排名，再运行既有价格闸门。",
                       "主线退出：原持仓所属主线不再活跃时，下一交易日开盘退出。"])
        counts = manifest.get("hindsight_mainline_members_by_date", {})
        report.extend(["", "每日主线映射成员数：" + ", ".join(f"{date}={count}" for date, count in sorted(counts.items()))])
        missing = manifest.get("hindsight_mainline_missing_price_codes_by_date", {})
        if missing:
            report.extend(["缓存缺行情的成员：" + "; ".join(
                f"{date}={','.join(codes)}" for date, codes in sorted(missing.items()))])
    report.extend(["", "执行模型：日线开盘成交研究代理；不是实盘执行验证。", "",
        f"区间：{config.data.start}—{config.data.end}",
        f"累计净值收益：{s['total_return']:.2%}", f"最大回撤：{s['max_drawdown']:.2%}",
        f"已平仓交易：{s['closed_trades']}；期末持仓：{s['open_positions']}",
        "", "收益包含期末未平仓的市值变化，未假设期末强制成交。参数未通过独立留出验证时，不代表策略有效。", "",
        "## 研究边界", ""] + [f"- {v}" for v in s["limitations"]])
    (directory / "report.md").write_text("\n".join(report) + "\n", encoding="utf-8")


def sweep(config, grid_path, split, output, loaded, min_trades, max_trials):
    """Fit on dates BEFORE split; evaluate only the chosen config AFTER split.

    Each segment starts flat. There are no forward labels spanning the split.
    This is a single chronological holdout, not a walk-forward robustness claim.
    """
    records, metadata, actions, evidence, manifest = loaded
    if not config.data.start < split <= config.data.end:
        raise ValueError("split must fall inside the configured window")
    dates = sorted({r.trade_date for rows in records.values() for r in rows
                    if config.data.start <= r.trade_date <= config.data.end})
    train_dates, test_dates = [d for d in dates if d < split], [d for d in dates if d >= split]
    if min(len(train_dates), len(test_dates)) < 2:
        raise ValueError("train and holdout each require at least two sessions")
    grid = json.loads(Path(grid_path).read_text(encoding="utf-8"))
    if not grid or not all(isinstance(v, list) and v for v in grid.values()):
        raise ValueError("grid must map parameter paths to nonempty lists")
    count = 1
    for key, values in grid.items():
        if key.split(".")[0] == "data":
            raise ValueError("data sources and date windows cannot be optimized")
        count *= len(values)
    if count > max_trials:
        raise ValueError(f"grid has {count} trials, above --max-trials={max_trials}")
    if output.exists():
        raise ValueError("output already exists; choose a new directory")
    trials = []
    best = None
    for number, combination in enumerate(itertools.product(*grid.values()), 1):
        raw = config.to_dict()
        overrides = dict(zip(grid, combination))
        for key, value in overrides.items():
            section, name = key.split(".", 1)
            if section not in raw or name not in raw[section]:
                raise ValueError(f"unknown parameter: {key}")
            raw[section][name] = value
        raw["data"].update(start=train_dates[0], end=train_dates[-1])
        candidate = from_dict(raw)
        # Physically truncate training prices; future rows cannot enter fitting.
        training = {code: [r for r in rows if r.trade_date < split] for code, rows in records.items()}
        training = {code: rows for code, rows in training.items() if rows}
        result = run(candidate, training, metadata, actions, evidence)
        stats = result["summary"]
        objective = stats["total_return"] - stats["max_drawdown"]
        eligible = stats["closed_trades"] >= min_trades
        trials.append({"trial": number, "parameters": json.dumps(overrides, sort_keys=True),
                       "train_return": stats["total_return"], "train_drawdown": stats["max_drawdown"],
                       "closed_trades": stats["closed_trades"], "objective": objective, "eligible": eligible})
        print(f"trial {number}/{count}: return={stats['total_return']:.4f}, eligible={eligible}", flush=True)
        if eligible and (best is None or objective > best[0]):
            best = objective, candidate, result
    output.mkdir(parents=True)
    write_csv(output / "training_trials.csv", trials)
    write_json(output / "search_protocol.json", {"grid": grid, "split": split,
        "objective": "training total return minus training maximum drawdown",
        "min_closed_trades": min_trades, "holdout_selection": "never used to select parameters",
        "boundary": "flat restart; training marks final open positions, no future labels"})
    if best is None:
        write_json(output / "status.json", {"status": "no_eligible_training_trial", "holdout_evaluated": False})
        print("No eligible training trial; holdout remains unevaluated.")
        return
    _, chosen, train_result = best
    save_run(output / "selected_training", chosen, train_result, manifest)
    raw = chosen.to_dict()
    raw["data"].update(start=test_dates[0], end=test_dates[-1])
    chosen = from_dict(raw)
    result = run(chosen, records, metadata, actions, evidence)
    save_run(output / "holdout", chosen, result, manifest)
    write_json(output / "selected_config.json", chosen.to_dict())


def main(argv=None):
    parser = argparse.ArgumentParser(description="WW configurable research workflow")
    subs = parser.add_subparsers(dest="command", required=True)
    init = subs.add_parser("init")
    init.add_argument("--output", type=Path, required=True)
    for command in ("audit", "run", "sweep"):
        sub = subs.add_parser(command)
        sub.add_argument("--config", type=Path, required=True)
        sub.add_argument("--set", action="append", default=[], metavar="SECTION.KEY=JSON")
        if command != "audit":
            sub.add_argument("--output", type=Path, required=True)
        if command == "sweep":
            sub.add_argument("--grid", type=Path, required=True)
            sub.add_argument("--split", required=True)
            sub.add_argument("--min-trades", type=int, default=10)
            sub.add_argument("--max-trials", type=int, default=64)
    args = parser.parse_args(argv)
    try:
        if args.command == "init":
            if args.output.exists():
                raise ValueError("will not overwrite existing config")
            args.output.parent.mkdir(parents=True, exist_ok=True)
            write_json(args.output, Config().to_dict())
            return
        config = load_config(args.config, args.set)
        loaded = load_inputs(config, ROOT)
        if args.command == "audit":
            records, _, _, evidence, manifest = loaded
            result = {"status": "structural_audit_passed", "codes": len(records),
                "bars": sum(map(len, records.values())),
                "note": "Does not prove whole-market coverage or authenticity of historical evidence.",
                "inputs": manifest}
            if config.selection.mode == "hindsight_mainline":
                result["hindsight_mainline_label_rows"] = manifest.get("hindsight_mainline_label_rows", 0)
                result["hindsight_mainline_members_by_date"] = manifest.get("hindsight_mainline_members_by_date", {})
            else:
                result["evidence_records"] = len(evidence)
            print(json.dumps(result, ensure_ascii=False, indent=2))
        elif args.command == "run":
            if args.output.exists():
                raise ValueError("output already exists; choose a new run directory")
            records, metadata, actions, evidence, manifest = loaded
            result = run(config, records, metadata, actions, evidence)
            save_run(args.output, config, result, manifest)
            print(json.dumps(result["summary"], ensure_ascii=False, indent=2))
        else:
            if args.min_trades < 1 or args.max_trials < 1:
                raise ValueError("min-trades and max-trials must be positive")
            sweep(config, args.grid, args.split, args.output, loaded, args.min_trades, args.max_trials)
    except (ValueError, KeyError, OSError) as error:
        parser.exit(2, f"Error: {error}\n")


if __name__ == "__main__":
    main()
