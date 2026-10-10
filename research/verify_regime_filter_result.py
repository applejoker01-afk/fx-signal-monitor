"""Reconcile saved EXP-003 results; also check actual prices if cache is present.

No network or writes. CI can verify the portable ledger without cached prices.
"""

import gzip
import hashlib
import json
import math
import statistics
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RESULTS = ROOT / "research" / "results"
PREFIX = "2026-10-01-regime-filter-backtest"
BASE_PREFIX = "2026-10-01-initial-stop-backtest"


def source_hash_matches(path, expected):
    # Git's text=auto changes checkout CRLF/LF across platforms. Accept only
    # exact bytes or a newline-only conversion of the captured source bytes.
    # Compressed ledger and cached input hashes remain strictly byte-for-byte.
    raw = path.read_bytes()
    lf = raw.replace(b"\r\n", b"\n")
    return expected in {hashlib.sha256(value).hexdigest()
                        for value in (raw, lf, lf.replace(b"\n", b"\r\n"))}


def close(actual, expected):
    assert math.isclose(actual, expected, rel_tol=1e-11, abs_tol=1e-11), (actual, expected)


def metrics_check(trades, expected):
    for scenario in ("standard", "adverse"):
        values = [trade[f"{scenario}_net_r"] for trade in trades]
        metrics = expected[scenario]
        assert metrics["trades"] == len(values)
        close(metrics["total_net_r"], math.fsum(values))
        if values:
            close(metrics["mean_net_r"], statistics.mean(values))
            close(metrics["win_rate_pct"], 100 * sum(value > 0 for value in values) / len(values))
            losses = -math.fsum(min(0, value) for value in values)
            if losses:
                close(metrics["profit_factor"], math.fsum(max(0, value) for value in values) / losses)


def verify():
    report = json.loads((RESULTS / f"{PREFIX}.json").read_text(encoding="utf-8"))
    ledger_path = RESULTS / report["simulated_ledger"]["file"]
    assert hashlib.sha256(ledger_path.read_bytes()).hexdigest() == report["simulated_ledger"]["sha256"]
    ledger = json.loads(gzip.decompress(ledger_path.read_bytes()))
    previous_report = json.loads((RESULTS / f"{BASE_PREFIX}.json").read_text(encoding="utf-8"))
    baseline_path = RESULTS / previous_report["simulated_ledger"]["file"]
    assert hashlib.sha256(baseline_path.read_bytes()).hexdigest() == previous_report["simulated_ledger"]["sha256"]
    baseline = json.loads(gzip.decompress(baseline_path.read_bytes()))
    for name, expected in report["input_sha256"].items():
        path = {"protocol": ROOT / "research" / "regime_filter_protocol_20261001.md",
                "configuration": ROOT / "profitability_config.json",
                "previous_result": RESULTS / f"{BASE_PREFIX}.json"}.get(name)
        if path is None and (ROOT / name).is_file():
            path = ROOT / name
        if path is not None:
            assert source_hash_matches(path, expected), name
    price_path = ROOT / "research" / "cache" / "dukascopy_daily_2016_2025" / "daily_mid.json"
    prices = None
    if price_path.exists():
        assert hashlib.sha256(price_path.read_bytes()).hexdigest() == report["input_sha256"]["prices"]
        prices = json.loads(price_path.read_text(encoding="utf-8"))["results"]
    total = 0
    oos = {strategy: [] for strategy in report["oos_overall"]}
    for pair, periods in ledger.items():
        for period_name, strategies in periods.items():
            observed_baseline = [{k: v for k, v in trade.items() if k != "strategy"}
                                 for trade in strategies["no_regime_filter"]["trades"]]
            assert observed_baseline == baseline[pair][period_name]["fixed_initial_atr"]["trades"]
            for strategy, simulation in strategies.items():
                trades = simulation["trades"]
                metrics_check(trades, report["periods"][period_name]["by_pair"][pair][strategy])
                previous_exit = -1
                for trade in trades:
                    assert trade["strategy"] == strategy and trade["pair"] == pair
                    assert trade["signal_date"] < trade["entry_date"] <= trade["exit_date"]
                    assert trade["entry_index"] > previous_exit
                    previous_exit = trade["exit_index"]
                    assert 1 <= trade["holding_bars"] == trade["exit_index"] - trade["entry_index"] + 1 <= 20
                    sign = 1 if trade["direction"] == "LONG" else -1
                    assert sign * (trade["entry_price"] - trade["initial_sl"]) > 0
                    assert sign * (trade["target"] - trade["entry_price"]) > 0
                    close(trade["planned_standard_risk_price"],
                          abs(trade["entry_price"] - trade["initial_sl"]) + trade["costs"]["standard_price_cost"])
                    close(trade["gross_price_change"], sign * (trade["exit_price"] - trade["entry_price"]))
                    for scenario in ("standard", "adverse"):
                        close(trade[f"{scenario}_net_r"],
                              (trade["gross_price_change"] - trade["costs"][f"{scenario}_price_cost"])
                              / trade["planned_standard_risk_price"])
                    assert trade["standard_net_r"] >= trade["adverse_net_r"]
                    if prices:
                        bars = prices[pair]
                        entry, exit_bar = bars[trade["entry_index"]], bars[trade["exit_index"]]
                        signal_bar = bars[trade["entry_index"] - 1]
                        assert signal_bar["date"] == trade["signal_date"]
                        assert entry["date"] == trade["entry_date"] and exit_bar["date"] == trade["exit_date"]
                        assert entry["open"] == trade["entry_price"]
                        if strategy == "dma50_200_filter":
                            history = [bar["close"] for bar in bars[:trade["entry_index"]]]
                            fast, slow = math.fsum(history[-50:]) / 50, math.fsum(history[-200:]) / 200
                            close(trade["dma50"], fast)
                            close(trade["dma200"], slow)
                            close(trade["signal_close"], signal_bar["close"])
                            assert sign * (fast - slow) > 0
                            assert sign * (signal_bar["close"] - fast) > 0
                            assert sign * (signal_bar["close"] - slow) > 0
                    total += 1
                if period_name.startswith("test"):
                    oos[strategy].extend(trades)
    assert total == report["simulated_ledger"]["total_trades"]
    for period_name, period in report["periods"].items():
        for strategy in oos:
            trades = [trade for pair in ledger.values() for trade in pair[period_name][strategy]["trades"]]
            metrics_check(trades, period["overall"][strategy])
    decomposition = {}
    for strategy, trades in oos.items():
        metrics_check(trades, report["oos_overall"][strategy])
        values = [trade["standard_net_r"] for trade in trades]
        win = statistics.mean(value for value in values if value > 0)
        loss = -statistics.mean(value for value in values if value < 0)
        decomposition[strategy] = {
            "avg_net_win_r": win, "avg_net_loss_r": loss,
            "conditional_breakeven_win_rate": loss / (win + loss),
            "gross_mean_r": statistics.mean(t["gross_price_change"] / t["planned_standard_risk_price"] for t in trades),
            "mean_standard_cost_r": statistics.mean(t["costs"]["standard_price_cost"] / t["planned_standard_risk_price"] for t in trades)}
    return {"verified_trades": total, "baseline_exact_match": True,
            "price_and_past_only_filter_verified": prices is not None, "oos_decomposition": decomposition}


if __name__ == "__main__":
    print(json.dumps(verify(), ensure_ascii=False, indent=2))
