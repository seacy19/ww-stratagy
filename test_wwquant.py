import copy
from dataclasses import replace
from datetime import datetime, timedelta
import tempfile
import unittest
from unittest.mock import patch
import json
from pathlib import Path

from adam_gate import Bar
from pit_accounting import CorporateAction
from proxy_pool import ProxyCandidate, SecurityMeta
from tushare_data import DailyRecord
from wwquant.config import Config, from_dict
from wwquant.data import audit, select_evidence
from wwquant.engine import run
from wwquant.signals import Plan, make_plan
from wwquant.cli import sweep


def records(bars, start="20250101"):
    date = datetime.strptime(start, "%Y%m%d")
    return [DailyRecord((date + timedelta(days=i)).strftime("%Y%m%d"), b, 100000,
                        b.open, 1.0) for i, b in enumerate(bars)]


class ConfigTests(unittest.TestCase):
    def test_rejects_typos_nonfinite_and_wrong_types(self):
        for raw in ({"gate": {"typo": 1}}, {"portfolio": {"max_positions": 2.5}},
                    {"portfolio": {"initial_cash": float("nan")}},
                    {"signal": {"require_meaningful_pullback": "false"}},
                    {"gate": {"slow_ema": 0}}, {"selection": {"mode": "ww"}}):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                from_dict(raw)

    def test_roundtrip(self):
        config = Config()
        self.assertEqual(config.to_dict(), from_dict(config.to_dict()).to_dict())


class EvidenceTests(unittest.TestCase):
    def test_available_date_expiry_and_invalidation(self):
        config = Config().selection
        row = dict(available_date="20250102", expires_date="20250131", ts_code="A",
                   mainline="theme", mainline_score=8, company_score=.8,
                   invalidated="0", positive_cycle="1", real_member="1",
                   top_down="1", bottom_up="1")
        self.assertEqual(select_evidence([row], "20250101", config), {})
        self.assertIn("A", select_evidence([row], "20250102", config))
        self.assertEqual(select_evidence([row], "20250201", config), {})
        revoked = dict(row, available_date="20250103", invalidated="1")
        self.assertEqual(select_evidence([row, revoked], "20250103", config), {})


class PortfolioTests(unittest.TestCase):
    def setup_run(self, bars):
        c = Config()
        c.data.start, c.data.end = "20250101", "20250110"
        c.portfolio.initial_cash = 10000
        c.portfolio.risk_per_trade = .1
        c.portfolio.max_position_weight = 1
        c.portfolio.max_gross_exposure = 1
        c.portfolio.commission_bps = c.portfolio.slippage_bps = c.portfolio.sell_tax_bps = 0
        c.portfolio.min_commission = 0
        c.portfolio.trailing_stop = False
        return c, {"A": records(bars)}, {"A": SecurityMeta("A", "20200101")}

    def execute(self, c, data, metadata, actions=None):
        pool = [ProxyCandidate("20250101", "A", .1, 10, 100, 1, 1)]
        plan = Plan("20250101", "A", 9, 11, 8, .1)
        with patch("wwquant.engine.build_pit_proxy_candidates", return_value=pool), patch("wwquant.engine.make_plan", return_value=(plan, "approved")):
            return run(c, data, metadata, actions or {})

    def test_t1_and_gap_stop(self):
        c, data, meta = self.setup_run([Bar(10, 11, 9, 10, 100),
            Bar(10, 11, 7, 9, 100), Bar(7, 9, 6, 8, 100)])
        result = self.execute(c, data, meta)
        self.assertEqual([t["date"] for t in result["trades"]], ["20250102", "20250103"])
        self.assertEqual(result["trades"][-1]["price"], 7)
        self.assertEqual(result["trades"][-1]["reason"], "stop_gap")
        self.assertGreaterEqual(min(r["cash"] for r in result["equity"]), 0)

    def test_close_cannot_size_same_day_open(self):
        c, data, meta = self.setup_run([Bar(10, 11, 9, 10, 100), Bar(10, 11, 9, 10, 100)])
        first = self.execute(c, data, meta)
        data["A"][-1] = replace(data["A"][-1], bar=Bar(10, 100, 9, 99, 100))
        second = self.execute(c, data, meta)
        self.assertEqual(first["trades"][0], second["trades"][0])

    def test_suspended_next_day_order_expires(self):
        c, data, meta = self.setup_run([Bar(10, 11, 9, 10, 100)] * 3)
        data["A"].pop(1)
        data["B"] = records([Bar(10, 11, 9, 10, 100)] * 3)
        meta["B"] = SecurityMeta("B", "20200101")
        result = self.execute(c, data, meta)
        self.assertEqual(result["trades"], [])
        self.assertEqual(result["summary"]["decision_counts"]["no_next_session_bar_order_expired"], 1)

    def test_fee_and_lot_cash_constraint(self):
        c, data, meta = self.setup_run([Bar(10, 11, 9, 10, 100)] * 2)
        c.portfolio.min_commission = 5
        result = self.execute(c, data, meta)
        trade = result["trades"][0]
        self.assertEqual(trade["shares"] % 100, 0)
        self.assertLessEqual(trade["shares"] * trade["price"] + trade["fee"], 10000)

    def test_dividend_is_receivable_until_payment(self):
        c, data, meta = self.setup_run([Bar(10, 11, 9, 10, 100)] * 5)
        for i in range(2, 5):
            data["A"][i] = replace(data["A"][i], bar=Bar(9, 10, 8.5, 9, 100), adj_factor=10/9)
        action = CorporateAction("A", "20250103", record_date="20250102", pay_date="20250105",
                                 announce_date="20241201", cash_div_per_share=1, cash_div_tax_per_share=1)
        result = self.execute(c, data, meta, {"A": [action]})
        self.assertGreater(result["equity"][2]["receivables"], 0)
        self.assertEqual(result["equity"][4]["receivables"], 0)
        self.assertAlmostEqual(result["equity"][1]["equity"], result["equity"][4]["equity"])

    def test_future_prices_do_not_change_previous_results(self):
        c, data, meta = self.setup_run([Bar(10, 11, 9, 10, 100)] * 4)
        full = self.execute(c, data, meta)
        c.data.end = "20250103"
        short = self.execute(c, {"A": data["A"][:3]}, meta)
        self.assertEqual(full["equity"][:3], short["equity"])


class SignalTests(unittest.TestCase):
    def test_breakout_and_parameter_change(self):
        bars = [Bar(10, 10.5, 9.5, 10, 100)] * 61
        bars[58] = Bar(10, 10.2, 9, 10, 100)
        bars.append(Bar(10.5, 12, 10.2, 11.8, 300))
        c = Config()
        c.signal.mode = "gate_only"
        c.signal.max_stop_distance = .5
        data = records(bars, "20241001")
        plan, reason = make_plan("A", data, c)
        self.assertIsNotNone(plan, reason)
        c.gate = replace(c.gate, min_volume_ratio=10, min_range_atr=100, min_gap_atr=100,
                         trend_breakout_lookback=100)
        denied, _ = make_plan("A", data, c)
        self.assertIsNone(denied)


class SweepTests(unittest.TestCase):
    def test_only_training_selects_winner_and_prices_are_truncated(self):
        c = Config()
        c.data.start, c.data.end = "20250101", "20250106"
        data = {"A": records([Bar(10, 11, 9, 10, 100)] * 6)}
        calls = []

        def evaluate(config, prices, metadata, actions, evidence):
            calls.append((config.data.start, config.data.end, config.portfolio.risk_per_trade,
                          max(r.trade_date for rows in prices.values() for r in rows)))
            score = 0.1 if config.portfolio.risk_per_trade == .003 else .05
            return {"summary": {"total_return": score, "max_drawdown": .01,
                                "closed_trades": 12}}

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            grid = root / "grid.json"
            grid.write_text(json.dumps({"portfolio.risk_per_trade": [.003, .005]}))
            with patch("wwquant.cli.run", side_effect=evaluate), patch("wwquant.cli.save_run"):
                sweep(c, grid, "20250104", root / "output", (data, {}, {}, [], {}), 10, 4)
            self.assertEqual(len(calls), 3)
            self.assertEqual(calls[0][3], "20250103")
            self.assertEqual(calls[1][3], "20250103")
            self.assertEqual(calls[2][:3], ("20250104", "20250106", .003))

    def test_underpowered_training_does_not_touch_holdout(self):
        c = Config()
        c.data.start, c.data.end = "20250101", "20250106"
        data = {"A": records([Bar(10, 11, 9, 10, 100)] * 6)}
        result = {"summary": {"total_return": .1, "max_drawdown": .01, "closed_trades": 0}}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            grid = root / "grid.json"
            grid.write_text(json.dumps({"portfolio.risk_per_trade": [.003]}))
            with patch("wwquant.cli.run", return_value=result) as execute:
                sweep(c, grid, "20250104", root / "output", (data, {}, {}, [], {}), 10, 4)
            self.assertEqual(execute.call_count, 1)
            status = json.loads((root / "output/status.json").read_text())
            self.assertFalse(status["holdout_evaluated"])


if __name__ == "__main__":
    unittest.main()
