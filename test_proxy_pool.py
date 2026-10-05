import unittest

from adam_gate import Bar
from proxy_pool import SecurityMeta, build_pit_proxy_candidates
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


if __name__ == "__main__":
    unittest.main()
