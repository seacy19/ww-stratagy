"""Summarize QuantAgentChat screener cache coverage without exporting rows."""
from __future__ import annotations

import argparse
import gzip
import json
from collections import Counter
from datetime import datetime
from pathlib import Path
import re
from zoneinfo import ZoneInfo


SERIES_RE = re.compile(r"^series2_(mf|nomf)_(.+)_([0-9]{8})_([0-9]{8})\.json\.gz$")
SNAPSHOT_RE = re.compile(r"^xs_daily_([0-9]{8})\.json\.gz$")
DATE_RE = re.compile(r"^[0-9]{8}$")
DEFAULT_CACHE = Path.home() / "works" / "QuantAgentChat" / "data" / "screener-cache"
LOCAL_PIT_MANIFEST = Path(__file__).resolve().parent / "data" / "mainline_pit" / "manifest.json"


def read_cache(path: Path):
    with gzip.open(path, "rt", encoding="utf-8") as stream:
        wrapper = json.load(stream)
    return wrapper.get("v")


def audit(cache_dir: Path) -> str:
    files = sorted(cache_dir.glob("*.json.gz"))
    if not files:
        raise ValueError(f"no .json.gz cache files in {cache_dir}")

    groups = Counter()
    total_bytes = sum(path.stat().st_size for path in files)
    snapshots = []
    series = Counter()
    series_stocks = Counter()
    series_rows = Counter()
    series_min_dates = []
    series_max_dates = []
    series_sessions = []
    malformed = []
    fina_files = 0
    fina_fields = Counter()
    fina_dated_records = 0

    for path in files:
        name = path.name
        snapshot_match = SNAPSHOT_RE.match(name)
        series_match = SERIES_RE.match(name)
        if snapshot_match:
            groups["全市场日线快照"] += 1
            date = snapshot_match.group(1)
            value = read_cache(path)
            rows = value if isinstance(value, list) else []
            codes = {str(row.get("ts_code", "")) for row in rows if isinstance(row, dict)}
            dates = sorted({str(row.get("trade_date", "")) for row in rows if isinstance(row, dict)})
            snapshots.append((date, len(rows), len(codes), dates[0] if dates else "", dates[-1] if dates else ""))
            continue

        if series_match:
            mode = series_match.group(1)
            groups["个股历史序列（含资金流）" if mode == "mf" else "个股历史序列（无资金流）"] += 1
            value = read_cache(path)
            if not isinstance(value, dict):
                malformed.append(name)
                continue
            series_stocks[mode] += 1
            for field in ("bars", "basic", "flow"):
                date_map = value.get(field, {})
                if not isinstance(date_map, dict):
                    malformed.append(f"{name}:{field}")
                    continue
                dates = sorted(date for date in date_map if DATE_RE.match(str(date)))
                series[(mode, field)] += 1
                series_rows[(mode, field)] += len(dates)
                if dates:
                    series_min_dates.append(dates[0])
                    series_max_dates.append(dates[-1])
                    if field == "bars":
                        series_sessions.append(len(dates))
            continue

        if name.startswith("fina_"):
            groups["最新财务指标快照"] += 1
            fina_files += 1
            value = read_cache(path)
            if isinstance(value, dict):
                fina_fields.update(value.keys())
                fina_dated_records += int(any(DATE_RE.match(str(key)) for key in value))
            else:
                malformed.append(name)
            continue

        groups["其他"] += 1

    if not snapshots and not series_stocks and not fina_files:
        raise ValueError(f"no recognized QuantAgentChat cache files in {cache_dir}")

    generated = datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d %H:%M %Z")
    cache_label = "QuantAgentChat/data/screener-cache（本地只读审计，目录未复制）" if cache_dir == DEFAULT_CACHE else "用户指定的本地 screener-cache 目录（未复制）"
    lines = [
        "# QuantAgentChat 本地缓存覆盖审计",
        "",
        f"审计时间：{generated}",
        f"缓存来源：{cache_label}",
        f"文件数：{len(files)}；压缩后总量：{total_bytes / 1024 / 1024:.1f} MiB。",
        "",
        "## 本机实际缓存",
        "",
        "| 数据集 | 缓存文件 | 日期/覆盖 | 记录量 |",
        "|---|---:|---|---:|",
    ]
    for date, rows, codes, first, last in sorted(snapshots):
        date_range = f"{first}—{last}" if first else "无有效日期"
        lines.append(f"| 全市场未复权日线快照 | 1 | 文件日期 {date}；行内 {date_range} | {rows:,} 行 / {codes:,} 只股票 |")

    modes = sorted(series_stocks)
    for mode in modes:
        label = "含资金流" if mode == "mf" else "无资金流"
        stock_count = series_stocks[mode]
        start = min(series_min_dates) if series_min_dates else "未知"
        end = max(series_max_dates) if series_max_dates else "未知"
        parts = []
        for field, field_label in (("bars", "行情"), ("basic", "估值基础指标"), ("flow", "资金流")):
            count = series_rows[(mode, field)]
            if series[(mode, field)]:
                parts.append(f"{field_label} {count:,} 行")
        median_sessions = sorted(series_sessions)[len(series_sessions) // 2] if series_sessions else 0
        lines.append(f"| 个股历史序列（{label}） | {stock_count} | {start}—{end} | {'；'.join(parts)}；每股行情中位数 {median_sessions} 个交易日 |")

    if fina_files:
        fields = "、".join(sorted(fina_fields)) or "字段未识别"
        lines.append(f"| 财务指标缓存 | {fina_files} | 仅缓存最新值；不含逐期可见日期 | {fields} |")

    lines += [
        "",
        "## WW 项目已有的本地 PIT 数据底座",
        "",
    ]
    if LOCAL_PIT_MANIFEST.exists():
        manifest = json.loads(LOCAL_PIT_MANIFEST.read_text(encoding="utf-8"))
        stages = manifest.get("stages", {})
        sw = stages.get("sw", {})
        cninfo = stages.get("cninfo", {})
        fundamentals = stages.get("fundamentals", {})
        calendar = stages.get("calendar", {})
        evidence = stages.get("evidence_2019_04_01", {})
        evidence_status = evidence.get("status", "unknown")
        lines += [
            "本机另有一套与 QuantAgentChat 筛选缓存分离的 WW PIT 研究数据，存于本地忽略目录 `data/mainline_pit/`；这些原始文件没有复制进代码仓库。以下计数来自该目录的 `manifest.json`：",
            "",
            "| 数据层 | 本机记录 | 适用边界 |",
            "|---|---:|---|",
            f"| 申万历史行业成员 | {sw.get('membership_rows', 0):,} 个规范化区间；覆盖 {sw.get('unique_stocks', 0):,} 只历史股票 | 可按纳入/退出日期重建行业成员，不等同产业主题成员 |",
            f"| 巨潮公告元数据 | {cninfo.get('unique_announcements', 0):,} 条唯一公告；{cninfo.get('query_count', 0)} 个查询 | 有公告日期/来源链接，保守可用日；不是系统性新闻档案 |",
            f"| 主营业务构成 | {fundamentals.get('mainbz_rows', 0):,} 行，{fundamentals.get('candidate_stocks', 0)} 只试点股票 | 条件 PIT：按首次披露日期对齐，历史值可能被供应商修订 |",
            f"| 交易日历 | {calendar.get('open_trade_dates', 0):,} 个开市日（{calendar.get('min_date', '?')}—{calendar.get('max_date', '?')}） | 交易日期基准已保存 |",
            f"| 2019-04-01 试点证据包 | 状态 `{evidence_status}`；锚定前主营证据覆盖 {fundamentals.get('candidate_stocks_with_pre_anchor_business_evidence', 0)}/{fundamentals.get('candidate_stocks', 0)} 只 | 骨架已建，成员/利润落点仍需人工核验；正式主线测试未启动 |",
        ]
    else:
        lines.append("本次未发现本地 `data/mainline_pit/manifest.json`；不会据此假定 WW PIT 数据层已就绪。")
    lines += [
        "",
        "## 与 WW 回测的关系",
        "",
        "- `series2_mf` 缓存的 bars 已由 QuantAgentChat 按查询窗口末端复权因子转换为前复权价格；缓存保留行情、估值指标和资金流，但不保留原始复权因子及公司行为记录。",
        "- 个股序列只覆盖曾进入筛选缓存的股票，不能视为全 A 股、无幸存者偏差的历史股票池。全市场缓存目前是单日快照，不是全市场逐日历史面板。",
        "- 财务缓存只有少数字段的最新值，没有每期公告可用日及更正版本，不能直接作为 PIT 回测证据。公告、主题成分历史变化也未见于这批缓存。",
        "- QuantAgentChat 的回测器可以运行时向数据代理请求日线和复权因子；那是在线取数能力，不等于此目录内已保存完整历史。",
        "- 因此这些缓存可支持候选标的的价格/指标复核与原型研究；直接并入正式 WW 回测前，仍需补齐历史股票池、可审计的公司行为/PIT 财务及主题证据。",
    ]
    if malformed:
        lines += ["", f"解析异常文件：{len(malformed)} 个；首项：`{malformed[0]}`。"]
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cache-dir", type=Path, default=DEFAULT_CACHE)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    report = audit(args.cache_dir)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(report, encoding="utf-8")
    else:
        print(report, end="")


if __name__ == "__main__":
    main()
