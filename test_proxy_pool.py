import unittest
import json
import tempfile
from pathlib import Path

from adam_gate import Bar
from proxy_pool import (
    SecurityMeta,
    apply_metadata_aliases,
    build_pit_proxy_candidates,
    is_a_share_universe_member,
)
from tushare_data import DailyRecord


def make_records(code, closes, amounts):
    result = []
    for index, (close, amount) in enumerate(zip(closes, amounts), start=1):
        result.append(
            DailyRecord(
                trade_date=f"2025010{index}",
                bar=Bar(close, close + 0.2, close - 0.2, close, 1000),
                amount=amount,
                pre_close=close,
                adj_factor=1.0,
            )
        )
    return result


class ProxyPoolTests(unittest.TestCase):
    def test_cross_sectional_proxy_uses_only_past_bars(self):
        records = {
            "A": make_records("A", [10, 11, 12], [100, 110, 120]),
            "B": make_records("B", [10, 10.5, 10.8], [90, 95, 100]),
            "C": make_records("C", [10, 10.2, 10.1], [80, 85, 90]),
        }
        metadata = {
            code: SecurityMeta(code, "20200101") for code in records
        }
        candidates = build_pit_proxy_candidates(
            records,
            metadata,
            return_lookback=2,
            liquidity_lookback=2,
            top_return_fraction=1.0,
            top_liquidity_fraction=1.0,
        )
        self.assertEqual([item.ts_code for item in candidates], ["A", "B"])
        self.assertEqual(candidates[0].trade_date, "20250103")
        self.assertGreater(candidates[0].return_lookback, candidates[1].return_lookback)

    def test_delisted_security_is_not_used_on_or_after_delist_date(self):
        records = {"D": make_records("D", [10, 11, 12], [100, 100, 100])}
        metadata = {"D": SecurityMeta("D", "20200101", delist_date="20250103")}
        candidates = build_pit_proxy_candidates(
            records,
            metadata,
            return_lookback=2,
            liquidity_lookback=2,
        )
        self.assertEqual(candidates, [])

    def test_st_security_is_excluded_only_during_pit_st_interval(self):
        records = {
            "ST": make_records("ST", [10, 11, 12, 13], [100, 100, 100, 100]),
            "OK": make_records("OK", [10, 10.5, 10.8, 11.0], [90, 90, 90, 90]),
        }
        metadata = {
            "ST": SecurityMeta(
                "ST", "20200101", st_periods=(("20250103", "20250104"),)
            ),
            "OK": SecurityMeta("OK", "20200101"),
        }
        candidates = build_pit_proxy_candidates(
            records,
            metadata,
            return_lookback=2,
            liquidity_lookback=2,
            top_return_fraction=1.0,
            top_liquidity_fraction=1.0,
        )
        by_date = {}
        for candidate in candidates:
            by_date.setdefault(candidate.trade_date, set()).add(candidate.ts_code)
        self.assertNotIn("ST", by_date.get("20250103", set()))
        self.assertIn("ST", by_date.get("20250104", set()))

    def test_bse_history_is_excluded_before_exchange_open(self):
        dates = ["20211111", "20211112", "20211115"]
        records = {}
        for code, prices in {"600000.SH": [10, 11, 12], "430047.BJ": [10, 11, 12]}.items():
            records[code] = [
                DailyRecord(date, Bar(price, price, price, price, 100), 1000, price, 1.0)
                for date, price in zip(dates, prices)
            ]
        metadata = {
            "600000.SH": SecurityMeta("600000.SH", "20000101"),
            # NEEQ listing predates BSE; the exchange trading date still gates
            # eligibility in this historical A-share universe.
            "430047.BJ": SecurityMeta("430047.BJ", "20140101"),
        }
        candidates = build_pit_proxy_candidates(
            records, metadata, return_lookback=1, liquidity_lookback=1,
            top_return_fraction=1.0, top_liquidity_fraction=1.0,
        )
        by_date = {}
        for item in candidates:
            by_date.setdefault(item.trade_date, set()).add(item.ts_code)
        self.assertNotIn("430047.BJ", by_date.get("20211112", set()))
        self.assertIn("430047.BJ", by_date.get("20211115", set()))
        self.assertFalse(is_a_share_universe_member("430047.BJ", "20211112"))
        self.assertTrue(is_a_share_universe_member("430047.BJ", "20211115"))

    def test_historical_code_alias_is_applied_without_mutating_base_map(self):
        with tempfile.TemporaryDirectory() as directory:
            alias_file = Path(directory) / "aliases.json"
            alias_file.write_text(json.dumps({
                "300114.SZ": {
                    "alias_of": "302132.SZ", "list_date": "20100827",
                    "delist_date": "20250217", "source": "verified Tushare namechange"
                }
            }), encoding="utf-8")
            base = {"600000.SH": SecurityMeta("600000.SH", "19991110")}
            result = apply_metadata_aliases(base, alias_file)
            self.assertNotIn("300114.SZ", base)
            self.assertEqual(result["300114.SZ"].list_date, "20100827")
            self.assertFalse(is_a_share_universe_member("300114.SZ", "20250217", result["300114.SZ"]))


if __name__ == "__main__":
    unittest.main()
