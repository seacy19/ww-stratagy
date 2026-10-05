import unittest

from types import SimpleNamespace

from tushare_data import TushareDataClient


class FakeFrame:
    def __init__(self, rows):
        self.rows = [SimpleNamespace(**row) for row in rows]

    def __len__(self):
        return len(self.rows)

    def sort_values(self, field):
        return FakeFrame(
            [vars(row) for row in sorted(self.rows, key=lambda item: getattr(item, field))]
        )

    def reset_index(self, drop=True):
        return self

    def itertuples(self):
        return iter(self.rows)


class FakePro:
    def daily(self, **kwargs):
        return FakeFrame(
            [
                {
                    "trade_date": "20250102",
                    "open": 10.2,
                    "high": 10.5,
                    "low": 10.0,
                    "close": 10.4,
                    "pre_close": 10.1,
                    "vol": 1000,
                    "amount": 10000,
                },
                {
                    "trade_date": "20250101",
                    "open": 10.0,
                    "high": 10.2,
                    "low": 9.9,
                    "close": 10.1,
                    "pre_close": 10.0,
                    "vol": 900,
                    "amount": 9000,
                },
            ]
        )

    def adj_factor(self, **kwargs):
        return FakeFrame(
            [
                {"trade_date": "20250102", "adj_factor": 1.2},
                {"trade_date": "20250101", "adj_factor": 1.2},
            ]
        )

    def stk_mins(self, **kwargs):
        return FakeFrame(
            [
                {
                    "trade_time": "2025-01-02 09:31:00",
                    "open": 10.1,
                    "high": 10.2,
                    "low": 10.0,
                    "close": 10.15,
                },
                {
                    "trade_time": "2025-01-02 09:30:00",
                    "open": 10.0,
                    "high": 10.1,
                    "low": 9.95,
                    "close": 10.1,
                },
            ]
        )

    def dividend(self, **kwargs):
        return FakeFrame(
            [
                {
                    "div_proc": "实施",
                    "ex_date": "20190617",
                    "record_date": "20190614",
                    "pay_date": "20190617",
                    "ann_date": "20190426",
                    "stk_bo_rate": 0.5,
                    "stk_co_rate": 0.0,
                    "cash_div": 0.1,
                    "cash_div_tax": 0.1,
                },
                {
                    "div_proc": "预案",
                    "ex_date": None,
                    "record_date": None,
                    "pay_date": None,
                    "ann_date": "20190401",
                    "stk_bo_rate": 0.5,
                    "stk_co_rate": 0.0,
                    "cash_div": 0.1,
                    "cash_div_tax": 0.1,
                },
            ]
        )


class TushareAdapterTests(unittest.TestCase):
    def test_daily_records_are_sorted_and_keep_adjustment_factor(self):
        client = TushareDataClient(pro=FakePro())
        records = client.daily_records("000001.SZ", "20250101", "20250102")
        self.assertEqual([record.trade_date for record in records], ["20250101", "20250102"])
        self.assertEqual(records[0].bar.close, 10.1)
        self.assertEqual(records[1].adj_factor, 1.2)

    def test_minute_bars_are_sorted_and_convert_time_to_hhmm(self):
        client = TushareDataClient(pro=FakePro())
        bars = client.minute_bars("000001.SZ", "20250102 09:30:00", "20250102 09:31:00")
        self.assertEqual([bar.time for bar in bars], [930, 931])
        self.assertEqual(bars[0].open, 10.0)

    def test_dividend_actions_keep_only_implemented_rows(self):
        client = TushareDataClient(pro=FakePro())
        actions = client.dividend_actions("002919.SZ", "20190101", "20191231")
        self.assertEqual(len(actions), 1)
        self.assertEqual(actions[0].stock_bonus_ratio, 0.5)
        self.assertEqual(actions[0].cash_div_per_share, 0.1)

    def test_dividend_actions_add_bonus_and_transfer_rates(self):
        class TransferPro(FakePro):
            def dividend(self, **kwargs):
                return FakeFrame([{
                    "div_proc": "\u5b9e\u65bd",
                    "ex_date": "20190523",
                    "record_date": "20190522",
                    "pay_date": "20190523",
                    "ann_date": "20190419",
                    "stk_bo_rate": 0.0,
                    "stk_co_rate": 0.4,
                    "cash_div": 0.221,
                    "cash_div_tax": 0.221,
                }])

        actions = TushareDataClient(pro=TransferPro()).dividend_actions(
            "603027.SH", "20190101", "20191231"
        )
        self.assertEqual(actions[0].stock_bonus_ratio, 0.4)


if __name__ == "__main__":
    unittest.main()
