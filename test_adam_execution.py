import unittest

from adam_execution import (
    MinuteBar,
    atr_rmse,
    breakout_pullback_reclaim_plan,
    build_entry_plan,
    confirmed_swing_lows,
    confirmed_zigzag_pivots,
    first_pullback,
    initial_stop,
    move_stop_up,
    normalized_path_errors,
    risk_position_size,
    simulate_t1_entry,
    wilder_atr,
)
from adam_gate import Bar


class AtrAndZigZagTests(unittest.TestCase):
    def test_wilder_atr_first_value_and_smoothing(self):
        bars = [
            Bar(open=10, high=11, low=10, close=10.5, volume=1)
            for _ in range(14)
        ]
        bars.append(Bar(open=10.5, high=13, low=10.5, close=12, volume=1))
        values = wilder_atr(bars, period=14)
        self.assertIsNone(values[12])
        self.assertAlmostEqual(values[13], 1.0)
        self.assertAlmostEqual(values[14], (13 * 1.0 + 2.5) / 14)

    def test_zigzag_confirms_low_only_on_later_bar(self):
        bars = [
            Bar(open=10, high=11, low=10, close=10.5, volume=1)
            for _ in range(14)
        ]
        bars.extend(
            [
                Bar(open=10, high=10.1, low=9.0, close=9.5, volume=1),
                Bar(open=9.5, high=11.5, low=9.5, close=11, volume=1),
                Bar(open=11, high=12, low=11, close=11.5, volume=1),
                Bar(open=11.5, high=11.6, low=9.5, close=10, volume=1),
            ]
        )
        pivots = confirmed_zigzag_pivots(bars, wilder_atr(bars), reversal_atr=2.0)
        lows = [pivot for pivot in pivots if pivot.kind == "low"]
        self.assertTrue(lows)
        self.assertEqual(lows[0].pivot_index, 14)
        self.assertEqual(lows[0].confirmation_index, 15)


class ProjectionAndEntryTests(unittest.TestCase):
    def test_first_pullback_separates_shallow_and_meaningful(self):
        shallow = first_pullback([103, 106, 105, 108], atr=2.0)
        meaningful = first_pullback([103, 106, 103.5, 108], atr=2.0)
        self.assertFalse(shallow.meaningful)
        self.assertAlmostEqual(shallow.depth_atr, 0.5)
        self.assertTrue(meaningful.meaningful)
        self.assertAlmostEqual(meaningful.depth_atr, 1.25)

    def test_entry_plan_approves_safe_pullback_and_rejects_low_pullback(self):
        safe = build_entry_plan(100, 2, [103, 106, 103.5, 108])
        unsafe = build_entry_plan(100, 2, [103, 106, 102, 108])
        self.assertTrue(safe.approved)
        self.assertFalse(unsafe.approved)
        self.assertAlmostEqual(safe.lower, 99.5)
        self.assertAlmostEqual(safe.upper, 103.0)

    def test_strategy_mode_requires_one_atr_pullback(self):
        plan = build_entry_plan(
            100,
            2,
            [103, 104, 103.5, 105],
            require_meaningful_pullback=True,
        )
        self.assertFalse(plan.approved)
        self.assertIn("at least", plan.reason)


class PullbackReclaimTests(unittest.TestCase):
    def _episode_bars(self, pullback_low):
        # 61 quiet bars, then a volume-backed breakout and an actual retest.
        bars = [Bar(99.5, 101.0, 99.0, 100.0, 1_000) for _ in range(61)]
        bars.append(Bar(100.5, 103.0, 100.0, 102.5, 2_000))
        bars.append(Bar(102.4, 102.9, pullback_low, 102.6, 2_000))
        return bars

    def test_actual_one_atr_pullback_with_acceptance_approves(self):
        plan = breakout_pullback_reclaim_plan(self._episode_bars(100.7))
        self.assertTrue(plan.approved)
        self.assertAlmostEqual(plan.breakout_level, 101.0)
        self.assertGreaterEqual(plan.pullback_atr, 1.0)
        self.assertLessEqual(plan.pullback_atr, 2.0)
        self.assertGreaterEqual(plan.close_location, 0.70)

    def test_shallow_or_unaccepted_pullback_is_rejected(self):
        shallow = breakout_pullback_reclaim_plan(self._episode_bars(101.6))
        self.assertFalse(shallow.approved)
        self.assertIn("1--2 ATR", shallow.reason)

        bars = self._episode_bars(100.7)
        bars[-1] = Bar(102.4, 102.9, 100.7, 101.0, 2_000)
        unaccepted = breakout_pullback_reclaim_plan(bars)
        self.assertFalse(unaccepted.approved)
        self.assertIn("acceptance", unaccepted.reason)

class MinuteExecutionTests(unittest.TestCase):
    def setUp(self):
        self.plan = build_entry_plan(100, 2, [103, 104, 105])

    def test_open_in_zone_fills_immediately(self):
        result = simulate_t1_entry(self.plan, 102.0, [])
        self.assertEqual(result.status, "filled")
        self.assertEqual(result.entry_type, "open_direct")

    def test_reentry_from_below_fills_next_minute_open(self):
        minutes = [
            MinuteBar(930, 98, 98, 97.5, 98.5),
            MinuteBar(931, 99, 98.5, 98.5, 99.6),
            MinuteBar(932, 100, 99.5, 99.5, 100.2),
        ]
        result = simulate_t1_entry(self.plan, 98.0, minutes, slippage=0.1)
        self.assertEqual(result.entry_type, "reentry_from_below")
        self.assertEqual(result.execution_time, 932)
        self.assertAlmostEqual(result.execution_price, 100.1)

    def test_reentry_from_above_requires_return_then_turn(self):
        minutes = [
            MinuteBar(930, 103.5, 103.5, 102.8, 103.0),
            MinuteBar(931, 102.8, 103.0, 102.4, 102.5),
            MinuteBar(932, 102.5, 103.0, 102.4, 102.7),
            MinuteBar(933, 102.8, 103.0, 102.6, 102.9),
        ]
        result = simulate_t1_entry(self.plan, 104.0, minutes)
        self.assertEqual(result.entry_type, "reentry_from_above")
        self.assertEqual(result.execution_time, 933)

    def test_no_reentry_expires(self):
        minutes = [MinuteBar(930, 98, 98, 97, 97.5), MinuteBar(931, 98, 98, 97, 97.5)]
        result = simulate_t1_entry(self.plan, 98.0, minutes)
        self.assertEqual(result.status, "expired")


class StopAndMetricTests(unittest.TestCase):
    def _bars(self):
        lows = [10, 9, 11, 10, 12]
        return [
            Bar(open=low + 0.5, high=low + 1, low=low, close=low + 0.5, volume=1)
            for low in lows
        ]

    def test_swing_low_is_available_only_after_confirmation_bar(self):
        bars = self._bars()
        pivots = confirmed_swing_lows(bars)
        self.assertEqual(pivots[0], (1, 2, 9))
        self.assertIsNone(initial_stop(bars, asof_index=1, buffer=0.01))
        self.assertAlmostEqual(initial_stop(bars, asof_index=2, buffer=0.01), 8.99)

    def test_stop_only_moves_up(self):
        bars = self._bars()
        stop = initial_stop(bars, asof_index=2, buffer=0.01)
        self.assertIsNotNone(stop)
        moved = move_stop_up(bars, asof_index=4, current_stop=stop, buffer=0.01)
        self.assertAlmostEqual(moved, 9.99)

    def test_normalized_error_rmse_and_position_size(self):
        errors = normalized_path_errors([102, 103], [100, 101], atr=2)
        self.assertEqual(errors, [1.0, 1.0])
        self.assertAlmostEqual(atr_rmse(errors), 1.0)
        self.assertEqual(risk_position_size(1000, 110, 100, lot_size=100), 100)


if __name__ == "__main__":
    unittest.main()
