"""Execution, chronology, price decoding and costs required for EXP-005."""

import copy
import json
import lzma
import struct
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from research.backtest_initial_stop import (
    VARIANTS, bootstrap_difference, daily_differences, make_levels,
    prepare_signals, simulate_period, summarize,
)
from research.download_daily_history import decode_year
from modules.profitability import load_config


def bar(date, opened=100, high=101, low=99, close=100):
    return {"date": date, "open": opened, "high": high, "low": low, "close": close,
            "spread_close_price": 0.01}


def decision(date="2022-01-03", direction="LONG", stop=98, target=104):
    levels = {"direction": direction, "stop": stop, "target": target,
              "chandelier_used": False, "ta_score": 70, "atr": 1, "regime": "normal"}
    return {"date": date, "levels": {key: levels.copy() for key in VARIANTS},
            "costs": {"standard_price_cost": 0.2, "adverse_price_cost": 0.4}}


def run_bars(bars, signal=None, max_hold=20, end="2022-12-31"):
    signals = [signal or decision()] + [None] * (len(bars) - 1)
    return simulate_period("USDJPY", bars, signals, VARIANTS[0], "2022-01-04", end, max_hold)


class ExecutionTests(unittest.TestCase):
    def test_fills_next_open_not_signal_close(self):
        result = run_bars([bar("2022-01-03", close=101), bar("2022-01-04", opened=100.5)])
        trade = result["trades"][0]
        self.assertEqual(trade["entry_price"], 100.5)
        self.assertEqual(trade["entry_date"], "2022-01-04")

    def test_stop_priority_when_both_touched(self):
        result = run_bars([bar("2022-01-03"), bar("2022-01-04", high=105, low=97)])
        trade = result["trades"][0]
        self.assertEqual(trade["exit_reason"], "SL")
        self.assertEqual(trade["exit_price"], 98)
        self.assertAlmostEqual(trade["standard_net_r"], -1)
        self.assertEqual(result["ambiguous_stop_target_bars"], 1)

    def test_existing_long_gap_uses_worse_open(self):
        trade = run_bars([bar("2022-01-03"), bar("2022-01-04"),
                          bar("2022-01-05", opened=95, high=97, low=94, close=96)])["trades"][0]
        self.assertEqual(trade["exit_reason"], "SL_GAP")
        self.assertEqual(trade["exit_price"], 95)
        self.assertLess(trade["standard_net_r"], -1)

    def test_existing_short_gap_uses_worse_open(self):
        trade = run_bars([bar("2022-01-03"), bar("2022-01-04"),
                          bar("2022-01-05", opened=106, high=108, low=105, close=107)],
                         decision(direction="SHORT", stop=102, target=96))["trades"][0]
        self.assertEqual(trade["exit_reason"], "SL_GAP")
        self.assertEqual(trade["exit_price"], 106)
        self.assertLess(trade["standard_net_r"], -1)

    def test_invalid_next_open_not_backdated_to_limit(self):
        result = run_bars([bar("2022-01-03"), bar("2022-01-04", opened=97, low=96, high=99)])
        self.assertEqual(result["trades"], [])
        self.assertEqual(result["skipped_geometry"], 1)

    def test_favourable_target_gap_not_awarded_extra_profit(self):
        trade = run_bars([bar("2022-01-03"), bar("2022-01-04"),
                          bar("2022-01-05", opened=107, high=108, low=105, close=107)])["trades"][0]
        self.assertEqual(trade["exit_price"], 104)

    def test_cost_scenario_does_not_resize_quantity(self):
        trade = run_bars([bar("2022-01-03"), bar("2022-01-04", high=105, low=99)])["trades"][0]
        self.assertAlmostEqual(trade["planned_standard_risk_price"], 2.2)
        self.assertAlmostEqual(trade["standard_net_r"] - trade["adverse_net_r"], 0.2 / 2.2)

    def test_timeout_is_exact_number_of_held_bars(self):
        trade = run_bars([bar("2022-01-03"), bar("2022-01-04"), bar("2022-01-05")], max_hold=2)["trades"][0]
        self.assertEqual(trade["holding_bars"], 2)
        self.assertEqual(trade["exit_reason"], "TIME_20_BARS")

    def test_period_end_purges_unsettled_positions(self):
        trade = run_bars([bar("2022-01-03"), bar("2022-01-04"), bar("2023-01-03", high=106)],
                         end="2022-12-31")["trades"][0]
        self.assertEqual(trade["exit_reason"], "PERIOD_END")
        self.assertEqual(trade["exit_date"], "2022-01-04")

    def test_same_pair_signals_cannot_overlap(self):
        bars = [bar("2022-01-03"), bar("2022-01-04"), bar("2022-01-05"),
                bar("2022-01-06", high=105), bar("2022-01-07", high=105)]
        signals = [decision(date=row["date"]) for row in bars]
        result = simulate_period("USDJPY", bars, signals, VARIANTS[0], "2022-01-04", "2022-12-31")
        self.assertEqual(len(result["trades"]), 2)
        self.assertGreater(result["trades"][1]["entry_index"], result["trades"][0]["exit_index"])


class InformationTests(unittest.TestCase):
    def test_initial_chandelier_only_if_legal_at_signal(self):
        closes = [100.5] * 30 + [100]
        ta = {"ta_score": 70, "atr": 0.2}
        old = make_levels("USDJPY", closes, ta, VARIANTS[0])
        new = make_levels("USDJPY", closes, ta, VARIANTS[1])
        self.assertTrue(old["chandelier_used"])
        self.assertGreater(old["stop"], new["stop"])
        self.assertLess(old["stop"], 100)
        self.assertEqual(old["target"], new["target"])
        too_high = make_levels("USDJPY", [110] * 30 + [100], ta, VARIANTS[0])
        self.assertFalse(too_high["chandelier_used"])

    def test_future_mutation_cannot_change_prior_signal_or_spread(self):
        from datetime import date, timedelta
        bars = [bar((date(2020, 1, 1) + timedelta(days=i)).isoformat(),
                    opened=100 + i * .03, high=101 + i * .03, low=99 + i * .03, close=100 + i * .03)
                for i in range(300)]
        before = prepare_signals("USDJPY", bars, load_config())
        changed = copy.deepcopy(bars)
        changed[290]["close"] = 900
        changed[290]["spread_close_price"] = 50
        after = prepare_signals("USDJPY", changed, load_config())
        self.assertEqual(before[:290], after[:290])
        self.assertIsNotNone(before[289])
        self.assertNotEqual(before[290:], after[290:])

    def test_neutral_ta_does_not_generate_orders(self):
        self.assertIsNone(make_levels("USDJPY", [100] * 300, {"ta_score": 50, "atr": 1}, VARIANTS[0]))


class DataAndStatisticsTests(unittest.TestCase):
    def test_native_candle_scaling_and_weekend_filter(self):
        # 2024-01-01 Monday, 2024-01-06 Saturday.
        raw = b"".join(struct.pack(">5if", seconds, 140000, 141000, 139000, 142000, 1.0)
                       for seconds in (0, 86400, 5 * 86400))
        rows, exclusions = decode_year(lzma.compress(raw), "USDJPY", 2024)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["open"], 140)
        self.assertEqual(exclusions["weekend"], 1)
        rows, _ = decode_year(lzma.compress(raw), "EURUSD", 2024)
        self.assertEqual(rows[0]["open"], 1.4)

    def test_decoder_rejects_inverted_high_low(self):
        raw = struct.pack(">5if", 0, 140000, 141000, 145000, 139000, 1.0)
        with self.assertRaises(ValueError):
            decode_year(lzma.compress(raw), "USDJPY", 2024)

    def test_closed_market_placeholder_is_not_a_price_observation(self):
        raw = struct.pack(">5if", 0, 0, 0, 0, 0, 0.0)
        rows, exclusions = decode_year(lzma.compress(raw), "USDJPY", 2024)
        self.assertEqual(rows, [])
        self.assertEqual(exclusions["zero_volume"], 1)

    def test_decoder_rejects_duplicate_or_incomplete_record(self):
        record = struct.pack(">5if", 0, 140000, 141000, 139000, 142000, 1.0)
        for raw in (record + record, record + b"x"):
            with self.assertRaises(ValueError):
                decode_year(lzma.compress(raw), "USDJPY", 2024)

    def test_same_day_correlated_pair_results_stay_in_one_panel(self):
        baseline = [{"exit_date": "2022-01-04", "standard_net_r": -1}]
        candidate = [{"exit_date": "2022-01-04", "standard_net_r": 2},
                     {"exit_date": "2022-01-04", "standard_net_r": -2}]
        self.assertEqual(daily_differences(baseline, candidate, ["2022-01-04", "2022-01-05"], "standard"), [1, 0])

    def test_bootstrap_reproducible_and_zero_edge_fails(self):
        a = bootstrap_difference([0] * 40, iterations=100)
        self.assertEqual(a, bootstrap_difference([0] * 40, iterations=100))
        self.assertEqual(a["p_one_sided"], 1)
        self.assertEqual(a["ci95"], [0, 0])

    def test_statistics_normalize_r_and_aggregate_same_day_before_dd(self):
        trades = [{"pair": "USDJPY", "entry_date": "2022-01-03", "exit_date": "2022-01-04",
                   "exit_reason": "SL", "standard_net_r": -1},
                  {"pair": "EURUSD", "entry_date": "2022-01-03", "exit_date": "2022-01-04",
                   "exit_reason": "TP", "standard_net_r": 2}]
        result = summarize(trades, "standard")
        self.assertEqual(result["total_net_r"], 1)
        self.assertEqual(result["profit_factor"], 2)
        self.assertEqual(result["max_realized_drawdown_r"], 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
