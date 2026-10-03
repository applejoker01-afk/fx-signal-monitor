"""Financial invariants, cost scenarios and point-in-time regression tests."""
import copy
import contextlib
import io
import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from modules.advanced_analytics import calc_staged_tp
from modules.pending_orders import create_pending_order, check_pending_fills, pending_order_to_trade
from modules.position_sizing import calc_position_size
from modules.trade_tracker import open_trade_from_pending_fill, update_trades, calc_stats_from_trades, check_exit_condition
from modules.profitability import (audit_trade, build_report, cost_snapshot, load_config,
                                  partition_trades, portfolio_budget, summarize,
                                  trade_metrics, validate_geometry, walk_forward)


NOW = datetime(2026, 10, 1, 10, tzinfo=timezone.utc)
API = {"USDJPY": ("USD", "JPY"), "GBPJPY": ("GBP", "JPY"),
       "EURUSD": ("EUR", "USD"), "AUDJPY": ("AUD", "JPY")}
RATES = {"USDJPY": 150.0, "GBPJPY": 200.0, "EURUSD": 1.1, "EURJPY": 165.0, "AUDJPY": 100.0}
ACCOUNT = {"current_balance": 1000000.0, "risk_pct_per_trade": 3.0,
           "max_margin_usage_pct": 30.0, "leverage": 25}


def closed(pair="USDJPY", direction="LONG", entry=150.0, stop=149.0, exit=151.0, day=0):
    sign = 1 if direction.endswith("LONG") else -1
    return {"pair": pair, "direction": direction, "entry_price": entry,
            "initial_sl": stop, "sl": stop, "exit_price": exit,
            "pips": sign * (exit - entry), "result": "WIN" if sign * (exit-entry) > 0 else "LOSS",
            "entry_time": (NOW - timedelta(days=100) + timedelta(days=day)).isoformat(),
            "exit_time": (NOW - timedelta(days=100) + timedelta(days=day, hours=1)).isoformat()}


def signal(direction="LONG", pair="USDJPY", price=150.0, atr=1.0):
    regime = {"sl_multiplier": 3.0, "tp1_multiplier": 1.5, "regime": "normal"}
    staged = calc_staged_tp(price, direction, atr, regime, [price+2.999]*22, pair, spread_pips=0.2)
    return {"pair": pair, "direction": direction, "price": price, "stars": 4,
            "volatility_regime": regime, "staged_tp": staged}


class GeometryTests(unittest.TestCase):
    def test_stop_remains_fixed_with_chandelier_extreme(self):
        for pair, price, atr in (("USDJPY", 150, 1), ("GBPJPY", 200, 1.2),
                                 ("EURUSD", 1.1, .001), ("KRWJPY", .107, .001)):
            for direction in ("LONG", "SHORT", "LIGHT_LONG", "LIGHT_SHORT"):
                with self.subTest(pair=pair, direction=direction):
                    sign = 1 if direction.endswith("LONG") else -1
                    regime = {"sl_multiplier": 3.0, "tp1_multiplier": 1.5}
                    staged = calc_staged_tp(price, direction, atr, regime, [price+sign*2.99*atr]*22, pair)
                    self.assertFalse(staged["chandelier_sl_active"])
                    self.assertAlmostEqual(abs(price-staged["sl"]), 3*atr, places=6)
                    self.assertEqual(staged["rr_tp"], round(abs(staged["tp"]-price)/abs(price-staged["sl"]), 2))
                    self.assertIsNone(validate_geometry(direction, price, staged["sl"], staged["tp"]))

    def test_invalid_prices_and_directions(self):
        for direction, entry, stop, target in (("LONG", 150, 151, 152), ("SHORT", 150, 149, 148),
                ("LONG", 150, 149, 148), ("WAIT", 150, 149, 151), ("LONG", float("nan"), 149, 151),
                ("LONG", 150, float("inf"), 151), ("LONG", 0, 149, 151)):
            with self.subTest(direction=direction, entry=entry, stop=stop):
                self.assertIsNotNone(validate_geometry(direction, entry, stop, target))

    def test_initial_sl_cannot_be_replaced_with_trailing_sl(self):
        trade = closed()
        trade["sl"] = 150.9
        self.assertAlmostEqual(trade_metrics(trade)["gross_r"], 1.0)
        del trade["initial_sl"]
        self.assertIsNone(trade_metrics(trade))


class PendingTests(unittest.TestCase):
    def setUp(self):
        self.order = create_pending_order(signal(), NOW)

    def test_pullback_retains_loss_side_stop_and_frozen_costs(self):
        self.assertGreater(self.order["limit_price"], self.order["sl"])
        trade = pending_order_to_trade(self.order, NOW)
        self.assertEqual(trade["initial_sl"], self.order["sl"])
        self.assertEqual(trade["execution_costs"], self.order["execution_costs"])
        self.assertEqual(trade["strategy_version"], "profitability_v1")

    def test_invalid_order_rejected_at_creation_and_conversion(self):
        result = signal()
        result["staged_tp"]["sl"] = 151.0
        with self.assertRaises(ValueError):
            create_pending_order(result, NOW)
        self.order["sl"] = 151.0
        with self.assertRaises(ValueError):
            pending_order_to_trade(self.order, NOW)

    def test_missing_target_rejected(self):
        result = signal()
        result["staged_tp"]["tp"] = None
        result["staged_tp"]["tp1"] = None
        with self.assertRaises(ValueError):
            create_pending_order(result, NOW)

    def test_live_valid_order_fills(self):
        filled, remaining, cancelled = check_pending_fills({"USDJPY": self.order}, {"USDJPY": 149.6}, NOW+timedelta(minutes=5))
        self.assertEqual(list(filled), ["USDJPY"])
        self.assertFalse(remaining or cancelled)

    def test_expired_touch_is_never_a_fill(self):
        self.order["valid_until"] = NOW.isoformat()
        filled, _, cancelled = check_pending_fills({"USDJPY": self.order}, {"USDJPY": 149.6}, NOW)
        self.assertFalse(filled)
        self.assertEqual(cancelled["USDJPY"]["cancel_reason"], "EXPIRED")

    def test_bad_or_absent_expiry_fails_closed(self):
        for expiry in (None, "garbled"):
            with self.subTest(expiry=expiry):
                self.order["valid_until"] = expiry
                filled, _, cancelled = check_pending_fills({"USDJPY": self.order}, {"USDJPY": 149.6}, NOW)
                self.assertFalse(filled)
                self.assertTrue(cancelled)

    def test_gap_through_stop_is_unverified(self):
        filled, _, cancelled = check_pending_fills({"USDJPY": self.order}, {"USDJPY": 146.0}, NOW)
        self.assertFalse(filled)
        self.assertEqual(cancelled["USDJPY"]["cancel_reason"], "GAP_THROUGH_STOP_UNVERIFIED")

    def test_missing_and_nan_quote_do_not_fill(self):
        for price in (None, float("nan"), float("inf")):
            with self.subTest(price=price):
                filled, remaining, _ = check_pending_fills({"USDJPY": self.order}, {"USDJPY": price}, NOW)
                self.assertFalse(filled)
                self.assertTrue(remaining)


class CostAndAuditTests(unittest.TestCase):
    def test_round_trip_spread_counted_once_and_slippage_twice(self):
        c = cost_snapshot("USDJPY", 2.0, NOW)
        self.assertAlmostEqual(c["standard_price_cost"], .024)
        self.assertAlmostEqual(c["adverse_price_cost"], .052)

    def test_non_jpy_pip_scale(self):
        self.assertAlmostEqual(cost_snapshot("EURUSD", 2.0, NOW)["standard_price_cost"], .00024)

    def test_negative_and_nonfinite_costs_rejected(self):
        for spread in (-1, float("nan"), float("inf")):
            with self.assertRaises(ValueError):
                cost_snapshot("USDJPY", spread, NOW)

    def test_r_is_comparable_across_pair_units(self):
        a = closed()
        b = closed("EURUSD", entry=1.1, stop=1.09, exit=1.11)
        self.assertAlmostEqual(trade_metrics(a)["gross_r"], trade_metrics(b)["gross_r"])
        self.assertAlmostEqual(summarize([a,b])["gross_r"]["mean"], 1.0)

    def test_small_gross_profit_can_be_net_loss(self):
        trade = closed(exit=150.001)
        trade["execution_costs"] = cost_snapshot("USDJPY", 2.0, NOW)
        self.assertGreater(trade_metrics(trade)["gross_r"], 0)
        self.assertLess(trade_metrics(trade)["standard_net_r_estimate"], 0)

    def test_recorded_costs_are_not_recomputed(self):
        trade = closed()
        trade["execution_costs"] = cost_snapshot("USDJPY", 2.0, NOW)
        with patch("modules.spread_monitor.get_dynamic_spread_pips", side_effect=AssertionError("must use snapshot")):
            self.assertAlmostEqual(trade_metrics(trade)["standard_net_r_estimate"], .976)

    def test_corrupt_stop_and_pnl_are_excluded_without_editing_source(self):
        bad_stop = closed(stop=151)
        bad_pnl = closed()
        bad_pnl["pips"] = 100
        missing = closed()
        del missing["initial_sl"]
        rows = [closed(), bad_stop, bad_pnl, missing]
        original = copy.deepcopy(rows)
        valid, invalid = partition_trades(rows)
        self.assertEqual((len(valid),len(invalid)), (1,3))
        self.assertEqual(rows, original)
        self.assertEqual(summarize(rows)["missing_initial_sl"], 1)

    def test_win_loss_sign_mismatch_and_missing_pair_rejected(self):
        trade = closed()
        trade["result"] = "LOSS"
        self.assertTrue(audit_trade(trade))
        del trade["pair"]
        self.assertTrue(audit_trade(trade))

    def test_empty_statistics_do_not_invent_zero_expectancy(self):
        self.assertIsNone(summarize([])["standard_net_r_estimate"]["mean"])
        self.assertIsNone(summarize([closed()])["standard_net_r_estimate"]["profit_factor"])

    def test_weekly_stats_exclude_invalid_and_compare_net_r(self):
        stats = calc_stats_from_trades([closed(), closed(stop=151)])
        self.assertEqual(stats["total_trades"], 1)
        self.assertEqual(stats["excluded_trades"], 1)
        self.assertIn("profit_metrics", stats)


class RiskBudgetTests(unittest.TestCase):
    def test_currency_concentration_reduces_second_jpy_short(self):
        existing = {"USDJPY": [{"direction": "LONG", "entry_price": 150, "sl": 149, "units": 20000}]}
        budget = portfolio_budget("GBPJPY", "LONG", existing, API, RATES, 1000000)
        self.assertEqual(budget["available_jpy"], 10000)

    def test_hedges_do_not_cancel_cash_risk(self):
        existing = {"USDJPY": [{"direction":"LONG", "entry_price":150, "sl":149, "units":30000},
                               {"direction":"SHORT", "entry_price":150, "sl":151, "units":30000}]}
        self.assertEqual(portfolio_budget("EURUSD", "LONG", existing, API, RATES, 1000000)["available_jpy"], 0)

    def test_missing_existing_quantity_blocks_new_risk(self):
        existing = {"USDJPY": [{"direction":"LONG", "entry_price":150, "sl":149}]}
        self.assertEqual(portfolio_budget("GBPJPY", "LONG", existing, API, RATES, 1000000)["available_jpy"], 0)

    def test_sizing_includes_costs_without_exceeding_risk_budget(self):
        size = calc_position_size("USDJPY", 150, 149, API, RATES, account=ACCOUNT, open_trades={},
                                  direction="LONG", execution_costs=cost_snapshot("USDJPY", 2, NOW))
        self.assertTrue(size["tradable"])
        self.assertLessEqual(size["estimated_loss_jpy"], 30000)
        self.assertLess(size["units"], 30000)

    def test_sizing_enforces_direction_and_portfolio_budget(self):
        invalid = calc_position_size("USDJPY", 150, 151, API, RATES, ACCOUNT, {}, direction="LONG")
        self.assertFalse(invalid["tradable"])
        existing = {"USDJPY": [{"direction":"LONG", "entry_price":150, "sl":149, "units":30000}]}
        size = calc_position_size("GBPJPY", 200, 198, API, RATES, ACCOUNT, existing, direction="LONG")
        self.assertFalse(size["tradable"])

    def test_nonfinite_sizing_configuration_rejected(self):
        account = {**ACCOUNT, "risk_pct_per_trade": float("nan")}
        result = calc_position_size("USDJPY", 150, 149, API, RATES, account, {}, direction="LONG")
        self.assertFalse(result["tradable"])

    def test_wrong_quote_conversion_fails_closed(self):
        existing = {"EURUSD": [{"direction":"LONG", "entry_price":1.1, "sl":1.09, "units":10000}]}
        self.assertEqual(portfolio_budget("USDJPY", "LONG", existing, API, {}, 1000000)["available_jpy"], 0)

    def test_profit_locked_by_trailing_stop_does_not_increase_caps(self):
        existing = {"USDJPY": [{"direction":"LONG", "entry_price":150, "sl":151, "units":30000}]}
        self.assertEqual(portfolio_budget("GBPJPY", "LONG", existing, API, RATES, 1000000)["available_jpy"], 30000)

    def test_untradable_fill_does_not_create_phantom_position(self):
        trade = pending_order_to_trade(create_pending_order(signal(), NOW), NOW)
        with patch("modules.trade_tracker.load_open_trades", return_value={}), \
             patch("modules.trade_tracker.calc_position_size", return_value={"tradable":False,"units":0,"note":"insufficient"}), \
             patch("modules.trade_tracker.save_open_trades") as save:
            result = open_trade_from_pending_fill(trade, API, RATES)
            self.assertIn("_rejected", result)
            save.assert_not_called()

    def test_missing_sizing_context_rejected(self):
        trade = pending_order_to_trade(create_pending_order(signal(), NOW), NOW)
        with patch("modules.trade_tracker.load_open_trades", return_value={}), patch("modules.trade_tracker.save_open_trades") as save:
            self.assertIn("_rejected", open_trade_from_pending_fill(trade))
            save.assert_not_called()

    def test_second_fill_sees_first_fills_reserved_risk(self):
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory)/"open.json")
            with patch("modules.trade_tracker.OPEN_TRADES_FILE",path), \
                 patch("modules.position_sizing.load_virtual_account",return_value=ACCOUNT):
                first = pending_order_to_trade(create_pending_order(signal(),NOW),NOW)
                second = pending_order_to_trade(create_pending_order(signal(pair="GBPJPY",price=200),NOW),NOW)
                self.assertNotIn("_rejected",open_trade_from_pending_fill(first,API,RATES))
                self.assertIn("_rejected",open_trade_from_pending_fill(second,API,RATES))
                self.assertEqual(len(json.loads(Path(path).read_text(encoding="utf-8"))["USDJPY"]),1)


class TimeAndIntegrationTests(unittest.TestCase):
    def test_walk_forward_purges_unsettled_training_positions(self):
        config = load_config()
        config.update(min_walk_forward_train_trades=4, walk_forward_folds=3,
                      min_walk_forward_test_trades=2, min_cohort_trades=2)
        rows = [closed(day=i) for i in range(12)]
        rows[0]["exit_time"] = rows[-1]["exit_time"]
        report = walk_forward(rows, config)
        self.assertEqual(len(report["folds"]), 3)
        self.assertEqual(report["folds"][0]["train_trades"], 3)
        for fold in report["folds"]:
            self.assertLess(fold["train_latest_exit"], fold["test_start"])
        self.assertFalse(report["promotion_eligible"])

    def test_history_after_cutoff_does_not_leak_into_current_report(self):
        past, future = closed(), closed()
        future["exit_time"] = (NOW+timedelta(days=1)).isoformat()
        candidate = signal()
        before = candidate["stars"]
        report = build_report([candidate], [past, future], NOW)
        self.assertEqual(report["ledger"]["input_trades"], 1)
        self.assertEqual(report["candidates"][0]["cohort_trades"], 1)
        self.assertEqual(candidate["stars"], before)
        self.assertFalse(report["promotion_eligible"])

    def test_insufficient_oos_remains_hold(self):
        report = walk_forward([closed(day=i) for i in range(10)])
        self.assertEqual(report["status"], "HOLD")
        self.assertFalse(report["folds"])

    def test_signal_lost_uses_supplied_scan_time(self):
        trade = {"direction":"LONG", "entry_price":150, "sl":149, "tp":151,
                 "entry_time": (NOW-timedelta(hours=1)).isoformat(), "tp_hit":False}
        self.assertIsNone(check_exit_condition(trade, 149.5, 1, "WAIT", now=NOW))

    def test_new_paper_close_records_net_costs_and_updates_account_once(self):
        trade = pending_order_to_trade(create_pending_order(signal(), NOW), NOW)
        trade["units"] = 1000
        results = [{"pair":"USDJPY", "price":146.0, "stars":4, "direction":"LONG"}]
        with patch("modules.trade_tracker.load_open_trades", return_value={"USDJPY":[trade]}), \
             patch("modules.trade_tracker.save_open_trades"), patch("modules.trade_tracker.prune_closed_trades"), \
             patch("modules.trade_tracker.append_closed_trade") as append, patch("modules.trade_tracker.record_trade_pnl") as record:
            result = update_trades(results, NOW+timedelta(hours=1), API, RATES)
            settled = result["newly_closed"][0]
            self.assertLess(settled["net_pnl_jpy_estimate_ex_swap"], settled["pnl_jpy"])
            record.assert_called_once_with(settled["net_pnl_jpy_estimate_ex_swap"])
            append.assert_called_once()
            self.assertIn("profit_metrics", settled)

    def test_impossible_positive_sl_loss_does_not_credit_account(self):
        trade = pending_order_to_trade(create_pending_order(signal(),NOW),NOW)
        trade.update(sl=151,initial_sl=151,units=1000)
        with patch("modules.trade_tracker.load_open_trades",return_value={"USDJPY":[trade]}), \
             patch("modules.trade_tracker.save_open_trades"),patch("modules.trade_tracker.prune_closed_trades"), \
             patch("modules.trade_tracker.append_closed_trade"),patch("modules.trade_tracker.record_trade_pnl") as record:
            result = update_trades([{"pair":"USDJPY","price":150,"stars":4,"direction":"LONG"}],NOW,API,RATES)
            self.assertTrue(result["newly_closed"][0]["account_update_held"])
            record.assert_not_called()

    def test_config_cannot_enable_unvalidated_live_selector(self):
        config = load_config()
        config["selection_mode"] = "live"
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/"config.json"
            path.write_text(json.dumps(config), encoding="utf-8")
            with patch("modules.profitability.CONFIG_FILE", path), self.assertRaises(ValueError):
                load_config()

    def test_scanner_writes_cost_report_and_valid_pending_offline(self):
        import signal_scanner as scanner
        class ScanClock(datetime):
            @classmethod
            def now(cls, tz=None):
                return NOW.astimezone(tz) if tz else NOW.replace(tzinfo=None)
        row = signal()
        row.update(ta_score=70, fa_score=80, label="USDJPY", fa_detail="fixture", rsi=50, verdict="fixture")
        values = {
            "fetch_latest_rates": {"pairs":{"USDJPY":150}},
            "fetch_live_central_bank_rates": {}, "fetch_us_treasury_yields": {},
            "evaluate_market_sentiment": {"vix":18,"risk_mode":"normal"},
            "fetch_obsidian": None, "fetch_history": [149.0+i*.01 for i in range(100)],
            "evaluate_full": row, "collect_scan_results": 0,
            "calc_global_analytics": {}, "calc_portfolio_analytics": {"warnings":[],"risk_level":"low"},
            "check_cb_meeting_blackout": {"active":False},
            "collect_ambush_alerts": {"high_confidence":[],"approaching":[]},
            "generate_market_commentary": None, "has_ai_key":False,
            "generate_html_report": "<html>fixture</html>", "send_discord":None, "send_email":None,
        }
        passthrough = ("run_advanced_analytics", "apply_performance_weighting", "evaluate_ambush",
                       "apply_boj_cycle_directional_filter", "apply_vix_regime_filter", "apply_spread_filter",
                       "apply_session_filter", "apply_seasonal_filter")
        previous = Path.cwd()
        with tempfile.TemporaryDirectory() as directory, contextlib.ExitStack() as stack:
            try:
                os.chdir(directory)
                stack.enter_context(patch.object(scanner,"datetime",ScanClock))
                stack.enter_context(patch.dict(os.environ,{"ENTRY_MODE":"limit"},clear=True))
                for name,value in values.items():
                    stack.enter_context(patch.object(scanner,name,return_value=value))
                for name in passthrough:
                    stack.enter_context(patch.object(scanner,name,side_effect=lambda result,*a,**k: result))
                for name in ("generate_rates_html","save_rates_snapshot"):
                    stack.enter_context(patch("modules.rate_fetcher."+name))
                stdout, stderr = io.StringIO(), io.StringIO()
                with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                    scanner.main()
                self.assertNotIn("[ERROR]",stdout.getvalue())
                self.assertEqual(stderr.getvalue(),"")
                report = json.loads(Path("docs/profitability_report.json").read_text(encoding="utf-8"))
                pending = json.loads(Path("data/pending_orders.json").read_text(encoding="utf-8"))
                self.assertFalse(report["promotion_eligible"])
                self.assertIn("execution_costs",pending["USDJPY"])
                self.assertEqual(pending["USDJPY"]["strategy_version"],"profitability_v1")
                self.assertEqual(row["stars"],4)
            finally:
                os.chdir(previous)


if __name__ == "__main__":
    unittest.main()
