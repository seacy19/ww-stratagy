"""Audit local daily-price cache continuity and PIT action coverage.

This tool reads trusted local pickle caches and writes aggregate diagnostics;
it never exports price rows or modifies source caches.
"""
from __future__ import annotations

import argparse
import pickle
from collections import Counter
from datetime import datetime
from pathlib import Path

from proxy_pool import apply_metadata_aliases


ROOT = Path(__file__).resolve().parent
CACHE = ROOT / "proxy_cache"


def load(path: Path):
    with path.open("rb") as stream:
        return pickle.load(stream)


def audit(cache: Path = CACHE) -> str:
    metadata = apply_metadata_aliases(
        load(cache / "stock_metadata_v2.pkl"),
        ROOT / "configs" / "security_metadata_aliases.json",
    )
    actions = load(cache / "actions_v4_pit_repaired_20181112_20250515.pkl")
    shards = sorted(cache.glob("daily_v3_*.pkl"))
    if not shards:
        raise ValueError(f"no daily_v3 shards found under {cache}")

    lines = [
        "# WW 本地全市场日线缓存覆盖审计",
        "",
        f"审计日期：{datetime.now().astimezone().strftime('%Y-%m-%d %H:%M %Z')}",
        "数据只读取本地忽略目录 `proxy_cache/`；报告不包含行情逐行数据。",
        "",
        "| 缓存区间 | 股票数 | 行数 | 实际日期 | 无历史证券元数据 | 因子变化 | 有公司行动对应 | 无对应行动（<0.1% / 0.1–1% / 1–5% / ≥5%） |",
        "|---|---:|---:|---|---|---:|---:|---|",
    ]
    ranges = []
    pre_bse_rows = 0
    before_list_rows = 0
    for path in shards:
        records = load(path)
        observed = [row.trade_date for rows in records.values() for row in rows]
        missing_meta = sorted(set(records) - set(metadata))
        pre_bse_rows += sum(
            1 for code, rows in records.items() if code.endswith(".BJ")
            for row in rows if row.trade_date < "20211115"
        )
        before_list_rows += sum(
            1 for code, rows in records.items() if code in metadata
            for row in rows if row.trade_date < metadata[code].list_date
        )
        matched = 0
        unmatched = []
        changes = 0
        for code, rows in records.items():
            action_dates = {action.ex_date for action in actions.get(code, ())}
            for previous, current in zip(rows, rows[1:]):
                if not previous.adj_factor or not current.adj_factor:
                    continue
                delta = current.adj_factor / previous.adj_factor - 1.0
                if abs(delta) <= 1e-5:
                    continue
                changes += 1
                if any(previous.trade_date < date <= current.trade_date for date in action_dates):
                    matched += 1
                else:
                    unmatched.append(abs(delta))
        buckets = Counter(
            "<0.1%" if x < .001 else "0.1–1%" if x < .01 else "1–5%" if x < .05 else "≥5%"
            for x in unmatched
        )
        start, end = path.stem.rsplit("_", 2)[-2:]
        ranges.append((start, end, min(observed), max(observed)))
        bucket_text = " / ".join(str(buckets[label]) for label in ("<0.1%", "0.1–1%", "1–5%", "≥5%"))
        meta_text = ", ".join(f"`{code}`" for code in missing_meta) or "无"
        lines.append(
            f"| {start}—{end} | {len(records):,} | {sum(map(len, records.values())):,} "
            f"| {min(observed)}—{max(observed)} | {meta_text} | {changes:,} | {matched:,} "
            f"| {len(unmatched):,}（{bucket_text}） |"
        )

    gaps = []
    for left, right in zip(ranges, ranges[1:]):
        if left[3] < right[2]:
            gaps.append(f"{left[3]}—{right[2]}")
    lines += [
        "",
        "## 覆盖结论",
        "",
        f"缓存分成 {len(shards)} 段；段间无行情缓存的日期范围：" + ("；".join(gaps) if gaps else "未发现") + "。",
        "这些间隔是缓存区间断层，是否包含开市日应以交易日历复核；不能把段首/段尾文件名当作连续全历史。",
        "20200518—20211111 已另存于 `proxy_cache/backfill_daily/` 并完成逐日校验，但尚未合并到上述 pickle；其它日期范围和原始缓存的因子/行动异常仍须单独处理。",
        f"原始行情另含 {pre_bse_rows:,} 行 `.BJ` 北交所前身记录（北交所于 2021-11-15 开始交易），以及 {before_list_rows:,} 行早于证券基础资料上市日的记录；运行时 A 股候选池会在北交所开市前排除 `.BJ`，但原始缓存保留供审计。",
        "",
        "因子变化只按现存公司行动 ex-date 精确匹配。未匹配变化包括供应商因子修订/舍入和疑似缺失公司行动，不能仅凭大小阈值自动删除或认定为分红送转。",
        "当前 `wwquant.data.audit` 会在首个无元数据证券或未匹配因子变化处停止；因此以上缓存尚不能直接作为完整正式回测输入。",
        "",
        "## 仍未补齐的数据层",
        "",
        "- QuantAgentChat 缓存只有 2026-09-30 一日全市场快照；个股历史仅覆盖曾进入筛选的 800 只股票，财务指标无逐期公告可用日。",
        "- WW PIT 目录已有申万行业、部分巨潮公告元数据、35 只股票的主营业务试点和日历；仍缺全市场逐期 PIT 财务历史、可审计主题成员历史及系统性历史新闻档案。",
        "- 本轮发现的是已缓存行情的完整性问题，不能从现存缓存推导出缺失公司行动或财务原文。要真正补齐需安全可用的数据源、查询权限及原始记录留档。",
        "",
    ]
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cache-dir", type=Path, default=CACHE)
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
