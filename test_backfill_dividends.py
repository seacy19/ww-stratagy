import tempfile
import unittest
from pathlib import Path

from backfill_dividends import fetch_date, run


class Frame:
    def __init__(self, rows):
        self.rows = rows

    def __len__(self):
        return len(self.rows)

    def to_dict(self, orient):
        assert orient == "records"
        return self.rows


class FakeClient:
    def dividend(self, **kwargs):
        self.kwargs = kwargs
        return Frame([
            {"ts_code": "000001.SZ", "ex_date": kwargs["ex_date"], "div_proc": "实施"},
            {"ts_code": "000002.SZ", "ex_date": kwargs["ex_date"], "div_proc": "预案"},
        ])


class DividendBackfillTests(unittest.TestCase):
    def test_fetch_matches_only_implemented_actions(self):
        client = FakeClient()
        item = fetch_date(client, "20200520", ["000001.SZ", "000003.SZ"])
        self.assertEqual(client.kwargs, {"ex_date": "20200520"})
        self.assertEqual(item["factor_changes_matched_to_implemented_action"], ["000001.SZ"])
        self.assertEqual(item["factor_changes_without_implemented_action"], ["000003.SZ"])

    def test_rejects_rows_outside_requested_date(self):
        class WrongDateClient:
            def dividend(self, **kwargs):
                return Frame([{"ts_code": "000001.SZ", "ex_date": "20200521", "div_proc": "实施"}])
        with self.assertRaisesRegex(ValueError, "outside ex-date"):
            fetch_date(WrongDateClient(), "20200520", ["000001.SZ"])

    def test_resumable_fetch_covers_each_market_session(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "20200518.json").write_text(
                '{"trade_date":"20200518","adj_factor":[{"ts_code":"000001.SZ","adj_factor":2.0}]}',
                encoding="utf-8",
            )
            (root / "20200519.json").write_text(
                '{"trade_date":"20200519","adj_factor":[{"ts_code":"000001.SZ","adj_factor":2.2}]}',
                encoding="utf-8",
            )
            (root / "prev.pkl").write_bytes(b"unused")
            # The function needs a valid immediately preceding daily record.
            import pickle
            from adam_gate import Bar
            from tushare_data import DailyRecord
            shard = root / "before.pkl"
            with shard.open("wb") as stream:
                pickle.dump({"000001.SZ": [DailyRecord("20200515", Bar(1,1,1,1,1),1,1,2.0)]}, stream)
            output = root / "out"
            client = FakeClient()
            result = run("20200518", "20200519", output, 1, True, 0,
                         client=client, price_dir=root, preceding_shard=shard)
            self.assertEqual([x["ex_date"] for x in result["completed"]], ["20200518"])
            self.assertTrue((output / "20200518.json").exists())

    def test_factor_event_candidates_exclude_cross_source_rounding_noise(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "20200518.json").write_text(
                '{"trade_date":"20200518","adj_factor":[{"ts_code":"000001.SZ","adj_factor":2.0004}]}',
                encoding="utf-8",
            )
            (root / "20200519.json").write_text(
                '{"trade_date":"20200519","adj_factor":[{"ts_code":"000001.SZ","adj_factor":2.001}]}',
                encoding="utf-8",
            )
            import pickle
            from adam_gate import Bar
            from tushare_data import DailyRecord
            shard = root / "before.pkl"
            with shard.open("wb") as stream:
                pickle.dump({"000001.SZ": [DailyRecord("20200515", Bar(1,1,1,1,1),1,1,2.0)]}, stream)
            from backfill_dividends import factor_change_dates
            changes = factor_change_dates("20200518", "20200519", root, shard)
            self.assertNotIn("20200518", changes)
            self.assertEqual(changes["20200519"], ["000001.SZ"])


if __name__ == "__main__":
    unittest.main()
