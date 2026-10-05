import unittest

from adam_gate import Bar
from pit_accounting import (
    CorporateAction,
    adjusted_bar_asof,
    adjusted_bar_from_factor,
    adjusted_stop_asof,
    ledger_position_at_exit,
    ledger_return,
    repair_actions_from_factors,
)


class PitAccountingTests(unittest.TestCase):
    def setUp(self):
        self.action = CorporateAction(
            ts_code="002919.SZ",
            ex_date="20190617",
            record_date="20190614",
            pay_date="20190617",
            announce_date="20190426",
            stock_bonus_ratio=0.5,
            cash_div_per_share=0.1,
        )

    def test_future_action_is_not_used_before_ex_date(self):
        bar = Bar(32.0, 33.0, 31.0, 32.0, 100.0)
        unchanged = adjusted_bar_asof(
            bar, "20190613", "20190613", (self.action,)
        )
        self.assertEqual(unchanged.close, 32.0)

    def test_past_bar_is_adjusted_after_action_is_effective(self):
        bar = Bar(32.0, 33.0, 31.0, 32.0, 100.0)
        adjusted = adjusted_bar_asof(
            bar, "20190613", "20190618", (self.action,)
        )
        self.assertAlmostEqual(adjusted.close, (32.0 - 0.1) / 1.5)

    def test_ledger_handles_bonus_and_cash_dividend(self):
        shares, cash = ledger_position_at_exit(
            10, "20190613", "20190617", (self.action,)
        )
        self.assertEqual(shares, 15)
        self.assertAlmostEqual(cash, 1.0)
        self.assertAlmostEqual(
            ledger_return(10, 32.02, "20190613", 17.34, "20190617", (self.action,)),
            261.10 / 320.20 - 1.0,
        )

    def test_stop_is_moved_to_post_action_scale(self):
        self.assertAlmostEqual(
            adjusted_stop_asof(24.75, "20190613", "20190617", (self.action,)),
            (24.75 - 0.1) / 1.5,
        )

    def test_factor_adjusted_bar_scales_all_prices_not_volume(self):
        bar = adjusted_bar_from_factor(Bar(10, 12, 9, 11, 100), 1.5)
        self.assertEqual((bar.open, bar.high, bar.low, bar.close), (15, 18, 13.5, 16.5))
        self.assertEqual(bar.volume, 100)

    def test_repair_recovers_missing_transfer_rate_from_factor_jump(self):
        records = {
            "603027.SH": [
                type("R", (), {"trade_date": "20190522", "bar": Bar(24.0, 25.0, 23.0, 24.96, 1), "adj_factor": 2.03})(),
                type("R", (), {"trade_date": "20190523", "bar": Bar(17.59, 18.18, 17.32, 17.47, 1), "adj_factor": 2.868})(),
            ]
        }
        action = CorporateAction(
            "603027.SH", "20190523", "20190522", "20190523", "20190419", 0.0, 0.221
        )
        fixed = repair_actions_from_factors({"603027.SH": (action,)}, records)
        self.assertAlmostEqual(fixed["603027.SH"][0].stock_bonus_ratio, 0.4, places=3)


if __name__ == "__main__":
    unittest.main()
