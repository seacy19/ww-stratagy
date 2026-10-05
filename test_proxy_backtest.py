"""Causality regressions for the historical event backtester."""
import unittest

from adam_gate import Bar
from proxy_backtest import _build_code_state
from tushare_data import DailyRecord


class StopCausalityTests(unittest.TestCase):
    def test_full_history_stop_prefix_matches_available_history(self):
        lows = [10, 9, 11, 12, 8, 13, 14]
        records = [
            DailyRecord(f"202501{day:02d}", Bar(low + 1, low + 2, low,
                        low + 1, 100), 1000, low + 1, 1.0)
            for day, low in enumerate(lows, 1)
        ]
        full = _build_code_state(records)
        self.assertIsNone(full.latest_stop_series[0])
        self.assertIsNone(full.latest_stop_series[1])
        for length in range(1, len(records) + 1):
            prefix = _build_code_state(records[:length])
            self.assertEqual(full.latest_stop_series[:length], prefix.latest_stop_series)
            self.assertEqual(full.trailing_stop_series[:length], prefix.trailing_stop_series)


if __name__ == "__main__":
    unittest.main()
