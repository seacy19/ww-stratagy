import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from backfill_market_data import fetch_session, run


class Frame:
    def __init__(self, rows):
        self.rows = rows

    def __len__(self):
        return len(self.rows)

    def to_dict(self, orient):
        assert orient == "records"
        return self.rows


class FakeClient:
    def trade_cal(self, **kwargs):
        return Frame([{"cal_date": "20200518", "is_open": 1},
                      {"cal_date": "20200519", "is_open": 0}])

    def daily(self, **kwargs):
        return Frame([{"ts_code": "000001.SZ", "trade_date": kwargs["trade_date"],
                       "open": 10.0, "high": 11.0, "low": 9.0, "close": 10.5,
                       "pre_close": 10.0, "vol": 100.0, "amount": 1000.0}])

    def adj_factor(self, **kwargs):
        return Frame([{"ts_code": "000001.SZ", "trade_date": kwargs["trade_date"],
                       "adj_factor": 2.0}])


class BackfillTests(unittest.TestCase):
    def test_session_fetch_joins_daily_to_factor_and_hashes_payload(self):
        item = fetch_session(FakeClient(), "20200518")
        self.assertEqual(item["daily_count"], 1)
        self.assertEqual(item["factor_count"], 1)
        self.assertEqual(len(item["sha256_payload"]), 64)

    def test_missing_factor_fails_closed(self):
        client = FakeClient()
        client.adj_factor = lambda **kwargs: Frame([])
        with self.assertRaisesRegex(ValueError, "lack an adjustment factor"):
            fetch_session(client, "20200518")

    def test_run_checkpoints_and_resumes(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            first = run("20200518", "20200519", output, 1, True,
                        pause_seconds=0, client=FakeClient())
            self.assertEqual([item["trade_date"] for item in first["completed"]], ["20200518"])
            second = run("20200518", "20200519", output, 1, True,
                         pause_seconds=0, client=FakeClient())
            self.assertEqual([item["trade_date"] for item in second["completed"]], [])
            self.assertEqual(second["already_completed"], 1)


if __name__ == "__main__":
    unittest.main()
