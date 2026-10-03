"""Causality, one-change execution and adoption gates for EXP-003."""

import copy
import hashlib
import math
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from modules.profitability import load_config
from research.backtest_initial_stop import simulate_period
from research.backtest_regime_filter import (
    KNOWN_COMPARISONS, LEVEL_VARIANT, SCENARIOS, STRATEGIES, adjusted_bootstrap,
    build_signal_streams, evaluate_gates, regime_evidence, verify_baseline,
)
from research.test_initial_stop import bar, decision
from research.verify_regime_filter_result import source_hash_matches


class RegimeInformationTests(unittest.TestCase):
    def test_insufficient_history_cannot_authorize_entry(self):
        self.assertIsNone(regime_evidence([100] * 199))

    def test_rising_history_allows_only_long(self):
        evidence = regime_evidence([100 + index for index in range(200)])
        self.assertTrue(evidence["LONG"])
        self.assertFalse(evidence["SHORT"])
        self.assertAlmostEqual(evidence["dma50"], 274.5)
        self.assertAlmostEqual(evidence["dma200"], 199.5)

    def test_falling_history_allows_only_short(self):
        evidence = regime_evidence([300 - index for index in range(200)])
        self.assertTrue(evidence["SHORT"])
        self.assertFalse(evidence["LONG"])

    def test_equal_moving_averages_are_blocked(self):
        evidence = regime_evidence([100] * 200)
        self.assertFalse(evidence["LONG"])
        self.assertFalse(evidence["SHORT"])

    def test_long_requires_price_above_fast_average_too(self):
        evidence = regime_evidence([90] * 150 + [110] * 49 + [100])
        self.assertGreater(evidence["dma50"], evidence["dma200"])
        self.assertGreater(evidence["signal_close"], evidence["dma200"])
        self.assertFalse(evidence["LONG"])

    def test_short_requires_price_below_fast_average_too(self):
        evidence = regime_evidence([110] * 150 + [90] * 49 + [100])
        self.assertLess(evidence["dma50"], evidence["dma200"])
        self.assertLess(evidence["signal_close"], evidence["dma200"])
        self.assertFalse(evidence["SHORT"])

    def test_nonfinite_or_nonpositive_price_is_rejected(self):
        for value in (0, -1, math.nan, math.inf):
            with self.assertRaises(ValueError):
                regime_evidence([100] * 199 + [value])

    def test_features_use_last_200_prices_only(self):
        self.assertEqual(regime_evidence([1e9] * 10 + [100 + i for i in range(200)]),
                         regime_evidence([100 + i for i in range(200)]))

    def test_future_mutation_cannot_change_filter_or_prior_cost(self):
        from datetime import date, timedelta
        bars = [bar((date(2020, 1, 1) + timedelta(days=i)).isoformat(),
                    opened=100 + i * .03, high=101 + i * .03, low=99 + i * .03, close=100 + i * .03)
                for i in range(300)]
        before, _ = build_signal_streams("USDJPY", bars, load_config())
        changed = copy.deepcopy(bars)
        changed[290]["close"] = 900
        changed[290]["spread_close_price"] = 50
        after, _ = build_signal_streams("USDJPY", changed, load_config())
        for strategy in STRATEGIES:
            self.assertEqual(before[strategy][:290], after[strategy][:290])
        self.assertIsNotNone(before[STRATEGIES[1]][289])
        self.assertNotEqual(before[STRATEGIES[1]][290:], after[STRATEGIES[1]][290:])


class EntryOnlyTests(unittest.TestCase):
    def test_gate_does_not_modify_source_levels_or_costs(self):
        bars = [bar(f"2020-{i:03}", close=100 + i) for i in range(200)]
        signal = decision(date=bars[-1]["date"], stop=297, target=303)
        original = copy.deepcopy(signal)
        signals = [None] * 199 + [signal]
        with patch("research.backtest_regime_filter.prepare_signals", return_value=signals):
            streams, counts = build_signal_streams("USDJPY", bars, load_config())
        self.assertEqual(signal, original)
        self.assertEqual(streams[STRATEGIES[0]], signals)
        filtered = streams[STRATEGIES[1]][-1]
        self.assertEqual(filtered["costs"], original["costs"])
        for field, value in original["levels"][LEVEL_VARIANT].items():
            self.assertEqual(filtered["levels"][LEVEL_VARIANT][field], value)
        self.assertEqual(counts["allowed"], 1)

    def test_opposing_gate_removes_only_candidate_entry(self):
        bars = [bar(f"2020-{i:03}", close=300 - i) for i in range(200)]
        signals = [None] * 199 + [decision(date=bars[-1]["date"])]
        with patch("research.backtest_regime_filter.prepare_signals", return_value=signals):
            streams, counts = build_signal_streams("USDJPY", bars, load_config())
        self.assertIsNotNone(streams[STRATEGIES[0]][-1])
        self.assertIsNone(streams[STRATEGIES[1]][-1])
        self.assertEqual(counts["blocked_by_direction"]["LONG"], 1)

    def test_gate_does_not_force_exit_existing_trade(self):
        bars = [bar("2022-01-03"), bar("2022-01-04"), bar("2022-01-05", high=105)]
        baseline = [decision(), decision(date="2022-01-04"), None]
        candidate = [decision(), None, None]
        a = simulate_period("USDJPY", bars, baseline, LEVEL_VARIANT, "2022-01-04", "2022-12-31")
        b = simulate_period("USDJPY", bars, candidate, LEVEL_VARIANT, "2022-01-04", "2022-12-31")
        self.assertEqual(a, b)
        self.assertEqual(b["trades"][0]["exit_reason"], "TP")
        self.assertEqual(b["trades"][0]["holding_bars"], 2)


class GateTests(unittest.TestCase):
    def fixtures(self):
        periods = {f"test_{year}": {"overall": {
            STRATEGIES[0]: {s: {"trades": 100, "total_net_r": -1} for s in SCENARIOS},
            STRATEGIES[1]: {s: {"trades": 100, "total_net_r": 2} for s in SCENARIOS}}}
            for year in range(2022, 2026)}
        tests = {name: {"overall": {"adverse": {"p_bonferroni_33": .01, "ci95": [.01, .02]}}}
                 for name in ("improvement", "absolute_candidate")}
        return periods, tests

    def test_all_positive_years_and_both_tests_required(self):
        periods, tests = self.fixtures()
        self.assertEqual(evaluate_gates(periods, tests), [])
        tests["absolute_candidate"]["overall"]["adverse"]["p_bonferroni_33"] = .06
        self.assertTrue(evaluate_gates(periods, tests))

    def test_loss_reduction_cannot_pass_as_profit(self):
        periods, tests = self.fixtures()
        periods["test_2023"]["overall"][STRATEGIES[1]]["standard"]["total_net_r"] = -.5
        self.assertTrue(any("正ではない" in reason for reason in evaluate_gates(periods, tests)))

    def test_tiny_sample_cannot_pass_even_with_profit(self):
        periods, tests = self.fixtures()
        periods["test_2022"]["overall"][STRATEGIES[1]]["standard"]["trades"] = 29
        self.assertTrue(any("30取引未満" in reason for reason in evaluate_gates(periods, tests)))

    def test_non_improving_positive_year_cannot_pass(self):
        periods, tests = self.fixtures()
        periods["test_2025"]["overall"][STRATEGIES[0]]["adverse"]["total_net_r"] = 3
        self.assertTrue(any("改善していない" in reason for reason in evaluate_gates(periods, tests)))

    def test_known_previous_search_is_included_in_adjustment(self):
        result = adjusted_bootstrap([1] * 40, iterations=100)
        self.assertEqual(result["known_comparisons"], KNOWN_COMPARISONS)
        self.assertAlmostEqual(result["p_bonferroni_33"], min(1, 33 * result["p_one_sided"]))
        self.assertNotIn("p_bonferroni_11", result)
        self.assertFalse(result["ci95_multiple_testing_adjusted"])

    def test_zero_candidate_has_no_positive_edge(self):
        result = adjusted_bootstrap([0] * 40, iterations=100)
        self.assertEqual(result["p_bonferroni_33"], 1)
        self.assertEqual(result["ci95"], [0, 0])

    def test_baseline_reconciliation_fails_closed(self):
        periods = {"reference_2017_2021": {"overall": {STRATEGIES[0]: {"trades": 1}}, "by_pair": {}}}
        previous = {"periods": {"reference_2017_2021": {"overall": {LEVEL_VARIANT: {"trades": 2}}}}}
        with self.assertRaisesRegex(ValueError, "baseline changed"):
            verify_baseline(periods, previous)


class PortableSourceIntegrityTests(unittest.TestCase):
    def test_git_newline_conversion_preserves_source_integrity(self):
        lf = b"signal = 1\nstop = 2\n"
        crlf = lf.replace(b"\n", b"\r\n")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "source.py"
            for saved, checked_out in ((lf, lf), (lf, crlf), (crlf, lf)):
                path.write_bytes(checked_out)
                self.assertTrue(source_hash_matches(path, hashlib.sha256(saved).hexdigest()))

    def test_changed_source_is_rejected_after_newline_conversion(self):
        expected = hashlib.sha256(b"signal = 1\r\nstop = 2\r\n").hexdigest()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "source.py"
            path.write_bytes(b"signal = 999\nstop = 2\n")
            self.assertFalse(source_hash_matches(path, expected))


if __name__ == "__main__":
    unittest.main(verbosity=2)
