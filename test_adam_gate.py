import unittest

from adam_gate import AdamGateConfig, Bar, evaluate_entry_gate, second_reflection


class SecondReflectionTests(unittest.TestCase):
    def test_linear_projection_matches_diagram(self):
        prices = [90.0, 94.0, 92.0, 97.0, 100.0]
        self.assertEqual(second_reflection(prices), [103.0, 108.0, 106.0, 110.0])

    def test_log_projection_repeats_percentage_change(self):
        projection = second_reflection([80.0, 100.0], log_price=True)
        self.assertAlmostEqual(projection[0], 125.0)


class EntryGateTests(unittest.TestCase):
    def _base_bars(self, count=61):
        return [
            Bar(
                open=99.5 + index * 0.05,
                high=100.0 + index * 0.05,
                low=99.0 + index * 0.05,
                close=99.8 + index * 0.05,
                volume=1_000_000,
            )
            for index in range(count)
        ]

    def test_breakout_with_volume_is_qualified(self):
        bars = self._base_bars()
        prior_high = max(bar.high for bar in bars)
        bars.append(
            Bar(
                open=prior_high + 0.2,
                high=prior_high + 3.0,
                low=prior_high,
                close=prior_high + 2.5,
                volume=2_000_000,
            )
        )
        result = evaluate_entry_gate(bars)
        self.assertTrue(result["qualified"])
        self.assertTrue(result["triggers"]["breakout"])

    def test_breakout_without_volume_is_not_breakout_trigger(self):
        bars = self._base_bars()
        prior_high = max(bar.high for bar in bars)
        bars.append(
            Bar(
                open=prior_high + 0.2,
                high=prior_high + 1.0,
                low=prior_high,
                close=prior_high + 0.8,
                volume=1_100_000,
            )
        )
        result = evaluate_entry_gate(bars)
        self.assertFalse(result["triggers"]["breakout"])


if __name__ == "__main__":
    unittest.main()
