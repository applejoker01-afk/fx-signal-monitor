"""EXP-003: one fixed daily 50/200 SMA entry gate; offline research only."""

import argparse
import gzip
import hashlib
import json
import math
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from modules.profitability import load_config
from research.backtest_initial_stop import (
    PERIODS, SCENARIOS, bootstrap_difference, daily_differences, digest, fmt,
    prepare_signals, simulate_period, summarize,
)
from research.download_daily_history import CACHE, PAIRS

STRATEGIES = ("no_regime_filter", "dma50_200_filter")
LEVEL_VARIANT = "fixed_initial_atr"
KNOWN_COMPARISONS = 33
PROTOCOL = ROOT / "research" / "regime_filter_protocol_20261001.md"
PREVIOUS = ROOT / "research" / "results" / "2026-10-01-initial-stop-backtest.json"
OUTPUT = ROOT / "research" / "results" / "2026-10-01-regime-filter-backtest"


def regime_evidence(closes):
    """Use only the caller's completed history, including the signal close."""
    if len(closes) < 200:
        return None
    window = closes[-200:]
    if any(not math.isfinite(value) or value <= 0 for value in window):
        raise ValueError("SMA inputs must be finite positive prices")
    slow, fast = math.fsum(window) / 200, math.fsum(window[-50:]) / 50
    price = window[-1]
    return {"dma50": fast, "dma200": slow, "signal_close": price,
            "LONG": fast > slow and price > fast and price > slow,
            "SHORT": fast < slow and price < fast and price < slow}


def build_signal_streams(pair, bars, config):
    baseline = prepare_signals(pair, bars, config)
    candidate, closes = [], []
    counts = {"ta_signals": 0, "allowed": 0, "blocked": 0,
              "allowed_by_direction": {"LONG": 0, "SHORT": 0},
              "blocked_by_direction": {"LONG": 0, "SHORT": 0}}
    for bar, signal in zip(bars, baseline):
        closes.append(bar["close"])
        evidence = regime_evidence(closes) if signal else None
        if signal is None:
            candidate.append(None)
            continue
        counts["ta_signals"] += 1
        direction = signal["levels"][LEVEL_VARIANT]["direction"]
        if evidence is None or not evidence[direction]:
            candidate.append(None)
            counts["blocked"] += 1
            counts["blocked_by_direction"][direction] += 1
        else:
            counts["allowed"] += 1
            counts["allowed_by_direction"][direction] += 1
            # Separate level dictionaries: never alter the no-filter baseline.
            candidate.append({**signal, "levels": {
                **signal["levels"], LEVEL_VARIANT: {
                    **signal["levels"][LEVEL_VARIANT],
                    **{name: evidence[name] for name in ("dma50", "dma200", "signal_close")},
                    "entry_filter": STRATEGIES[1]}}})
    return {STRATEGIES[0]: baseline, STRATEGIES[1]: candidate}, counts


def adjusted_bootstrap(values, iterations=5000):
    result = bootstrap_difference(values, iterations=iterations)
    result.pop("p_bonferroni_11", None)
    p = result["p_one_sided"]
    result.update({"p_bonferroni_33": min(1.0, KNOWN_COMPARISONS * p) if p is not None else None,
                   "known_comparisons": KNOWN_COMPARISONS,
                   "ci95_multiple_testing_adjusted": False})
    return result


def verify_baseline(periods, previous):
    for name, period in periods.items():
        expected = previous["periods"][name]
        if period["overall"][STRATEGIES[0]] != expected["overall"][LEVEL_VARIANT]:
            raise ValueError(f"No-filter overall baseline changed: {name}")
        for pair in PAIRS:
            if period["by_pair"][pair][STRATEGIES[0]] != expected["by_pair"][pair][LEVEL_VARIANT]:
                raise ValueError(f"No-filter pair baseline changed: {name}/{pair}")
        if name.startswith("test"):
            if period["expanding_train_metrics"][STRATEGIES[0]] != expected["expanding_train_metrics"][LEVEL_VARIANT]:
                raise ValueError(f"No-filter training baseline changed: {name}")


def evaluate_gates(periods, tests):
    failures = []
    for name, period in periods.items():
        if not name.startswith("test"):
            continue
        for scenario in SCENARIOS:
            baseline = period["overall"][STRATEGIES[0]][scenario]
            candidate = period["overall"][STRATEGIES[1]][scenario]
            if candidate["trades"] < 30:
                failures.append(f"{name}/{scenario}: 候補が30取引未満。")
            if candidate["total_net_r"] <= 0:
                failures.append(f"{name}/{scenario}: 候補のコスト後合計Rが正ではない。")
            if candidate["total_net_r"] <= baseline["total_net_r"]:
                failures.append(f"{name}/{scenario}: 基準の合計Rを改善していない。")
    for name in ("improvement", "absolute_candidate"):
        primary = tests[name]["overall"]["adverse"]
        if (primary["p_bonferroni_33"] is None or primary["p_bonferroni_33"] >= 0.05
                or primary["ci95"] is None or primary["ci95"][0] <= 0):
            failures.append(f"{name}: 悪化コストの全体検定が補正pと区間下限の条件を満たさない。")
    return failures


def markdown(report):
    candidate = report["oos_overall"][STRATEGIES[1]]["standard"]
    lines = ["# EXP-003 移動平均の入口条件の試験", "",
             f"実行日: {report['generated_at']} / 研究判定: {report['research_status']} / 本番採用: HOLD", "",
             f"50/200日移動平均の条件を加えた候補は、2022〜2025年で{candidate['trades']:,}取引、"
             f"通常コスト後の平均{fmt(candidate['mean_net_r'])}R、PF {fmt(candidate['profit_factor'], 2)}。", "",
             "同じ固定ATR初期SLの基準と比べ、入口だけに追加条件を適用した。"
             "LONGはSMA50>SMA200かつ終値が両方より上、SHORTは逆。"
             "出口・コスト・対象10ペアは固定し、見送りによる取引回数と再エントリー機会の変化を含む。", "",
             "[結果を見る前に固定した条件](../regime_filter_protocol_20261001.md)", "",
             "## 年度別の結果（全10ペア）", "",
             "|期間|条件|取引数|通常 平均R|通常 合計R|通常 PF|悪化 平均R|悪化 合計R|悪化 PF|",
             "|---|---|---:|---:|---:|---:|---:|---:|---:|---:|"]
    names = {STRATEGIES[0]: "条件なし", STRATEGIES[1]: "50/200条件あり"}
    for label, period in report["periods"].items():
        for strategy in STRATEGIES:
            s, a = (period["overall"][strategy][scenario] for scenario in SCENARIOS)
            lines.append(f"|{label}|{names[strategy]}|{s['trades']}|{fmt(s['mean_net_r'])}|"
                         f"{fmt(s['total_net_r'])}|{fmt(s['profit_factor'], 2)}|"
                         f"{fmt(a['mean_net_r'])}|{fmt(a['total_net_r'])}|{fmt(a['profit_factor'], 2)}|")
    lines.extend(["", "## 2022〜2025年合計", "",
                  "|条件|取引数|通常 平均R|通常 合計R|通常 PF|通常 勝率|悪化 平均R|悪化 合計R|悪化 PF|",
                  "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|"])
    for strategy in STRATEGIES:
        s, a = (report["oos_overall"][strategy][scenario] for scenario in SCENARIOS)
        lines.append(f"|{names[strategy]}|{s['trades']}|{fmt(s['mean_net_r'])}|{fmt(s['total_net_r'])}|"
                     f"{fmt(s['profit_factor'], 2)}|{fmt(s['win_rate_pct'], 1)}%|"
                     f"{fmt(a['mean_net_r'])}|{fmt(a['total_net_r'])}|{fmt(a['profit_factor'], 2)}|")
    lines.extend(["", "## 主評価（悪化コスト）", "",
                  "全体および10ペアの差と絶対成績を、同日相関を保持した20取引日ブロックで5,000回再抽出。"
                  "直近2実験の既知33比較を補正。95%区間自体は未補正の診断値。過去の探索すべてを補正した意味ではない。", ""])
    for name, label in (("improvement", "候補−基準"), ("absolute_candidate", "候補−ゼロ")):
        test = report["tests"][name]["overall"]["adverse"]
        ci = test["ci95"]
        lines.append(f"- {label}: 合計{fmt(test['total_delta_r'])}R、1日平均{fmt(test['mean_delta_r_per_day'], 5)}R、"
                     f"未補正95%区間 [{fmt(ci[0], 5)}, {fmt(ci[1], 5)}]、"
                     f"片側p={fmt(test['p_one_sided'], 4)}、33比較補正p={fmt(test['p_bonferroni_33'], 4)}。")
    lines.extend(["", "## 判定", ""])
    if report["gate_failures"]:
        lines.extend(f"- {reason}" for reason in report["gate_failures"])
    else:
        lines.append("研究条件を通過した。ただし部品試験だけの結果のため本番採用はHOLD。")
    lines.extend(["", "## 検証範囲と限界", "",
                  "2016年は準備、2017〜2021年は基準、2022〜2025年は年ごとの擬似OOS。"
                  "既に前回の基準成績を見ているため、今回は完全未見・独立検証ではない。"
                  "2026年の価格は取得・評価していない。基準は前回の固定ATR群と全ペア・全期間で一致を確認した。", "",
                  "日足TA/OCOの入口条件だけを試した。FA、ニュース、イベント、指値、TP後トレーリング、"
                  "反転/消失決済、通貨別リスク上限、証拠金、最小ロット、スワップは再現していない。"
                  "価格はBID/ASK日足各値の平均で高安と足内順序に制限がある。コストはシグナル終値時点で固定したモデル。", "",
                  "1R=翌始値から初期SLまでの距離+標準往復コスト。悪化コストでも数量は同じ。"
                  "合計R・決済DDは円利益・口座利回り・含み損込みDDを表さない。"
                  "仮説を落とした場合は同じ結果に合わせて条件を変更しない。", "",
                  "## 再現", "", "```powershell",
                  "rtk proxy python -B -X utf8 research/test_regime_filter.py",
                  "rtk proxy python -B -X utf8 research/backtest_regime_filter.py", "```", "",
                  f"価格SHA-256: `{report['input_sha256']['prices']}`", "",
                  f"プロトコルSHA-256: `{report['input_sha256']['protocol']}`", "",
                  "入力はEXP-005と同じ26,068本、200元ファイル。取得元は同じ"
                  " `2026-10-01-initial-stop-backtest-sources.json` に保存済み。"
                  "今回の集計は `2026-10-01-regime-filter-backtest.json`、全仮想取引は"
                  " `2026-10-01-regime-filter-backtest-trades.json.gz` に保存。元価格はGitに追加しない。", "",
                  "Dukascopy Bank SA, [Historical Price Data](https://www.dukascopy.com/wiki/en/development/data-export/)", ""])
    return "\n".join(lines)


def run(iterations=5000):
    started = datetime.now(timezone.utc).isoformat()
    data_path, manifest_path = CACHE / "daily_mid.json", CACHE / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    previous = json.loads(PREVIOUS.read_text(encoding="utf-8"))
    if digest(data_path) != manifest["data_sha256"] or digest(data_path) != previous["input_sha256"]["prices"]:
        raise ValueError("Price SHA-256 differs from manifest or registered baseline")
    if digest(ROOT / "profitability_config.json") != previous["input_sha256"]["configuration"]:
        raise ValueError("Cost/risk configuration differs from registered baseline")
    data = json.loads(data_path.read_text(encoding="utf-8"))
    config = load_config()
    files = ("signal_scanner.py", "modules/advanced_analytics.py", "modules/profitability.py",
             "modules/spread_monitor.py", "research/download_daily_history.py",
             "research/backtest_initial_stop.py", "research/backtest_regime_filter.py")
    hashes = {"prices": digest(data_path), "manifest": digest(manifest_path),
              "previous_result": digest(PREVIOUS), "protocol": digest(PROTOCOL),
              "configuration": digest(ROOT / "profitability_config.json"),
              **{name: digest(ROOT / name) for name in files}}
    print(json.dumps({"run_started_at": started, "protocol_sha256": hashes["protocol"],
                      "experiment": "EXP-003", "iterations": iterations}), flush=True)
    periods = {name: {"range": bounds, "by_pair": {}, "overall": {}, "expanding_train_range":
                     ["2017-01-01", str(int(bounds[0][:4]) - 1) + "-12-31"] if name.startswith("test") else None}
               for name, bounds in PERIODS.items()}
    ledger, prepared, diagnostics = {}, {}, {}
    oos = {strategy: [] for strategy in STRATEGIES}
    for pair in PAIRS:
        bars = data["results"][pair]
        if any(bars[index]["date"] <= bars[index - 1]["date"] for index in range(1, len(bars))):
            raise ValueError("Price timestamps not unique and increasing")
        streams, diagnostics[pair] = build_signal_streams(pair, bars, config)
        prepared[pair], ledger[pair] = streams, {}
        for period_name, (start, end) in PERIODS.items():
            ledger[pair][period_name] = {}
            periods[period_name]["by_pair"][pair] = {}
            for strategy in STRATEGIES:
                simulation = simulate_period(pair, bars, streams[strategy], LEVEL_VARIANT, start, end)
                for trade in simulation["trades"]:
                    trade["strategy"] = strategy
                ledger[pair][period_name][strategy] = simulation
                periods[period_name]["by_pair"][pair][strategy] = {
                    **{scenario: summarize(simulation["trades"], scenario) for scenario in SCENARIOS},
                    "skipped_geometry": simulation["skipped_geometry"],
                    "ambiguous_stop_target_bars": simulation["ambiguous_stop_target_bars"]}
                if period_name.startswith("test"):
                    oos[strategy].extend(simulation["trades"])
        print(f"{pair}: prepared {len(bars)} bars; matched simulation rules", flush=True)
    for name, period in periods.items():
        for strategy in STRATEGIES:
            trades = [trade for pair in PAIRS for trade in ledger[pair][name][strategy]["trades"]]
            period["overall"][strategy] = {scenario: summarize(trades, scenario) for scenario in SCENARIOS}
        if name.startswith("test"):
            period["expanding_train_metrics"] = {}
            for strategy in STRATEGIES:
                training = [trade for pair in PAIRS for trade in simulate_period(
                    pair, data["results"][pair], prepared[pair][strategy], LEVEL_VARIANT,
                    "2017-01-01", period["expanding_train_range"][1])["trades"]]
                period["expanding_train_metrics"][strategy] = {
                    scenario: summarize(training, scenario) for scenario in SCENARIOS}
    verify_baseline(periods, previous)
    print("Baseline matches EXP-005 at every pair, period and expanding training window", flush=True)
    tests = {"improvement": {}, "absolute_candidate": {}}
    for group in ("overall", *PAIRS):
        dates = sorted({bar["date"] for pair in PAIRS if group == "overall" or pair == group
                        for bar in data["results"][pair] if "2022-01-01" <= bar["date"] <= "2025-12-31"})
        baseline = [trade for trade in oos[STRATEGIES[0]] if group == "overall" or trade["pair"] == group]
        candidate = [trade for trade in oos[STRATEGIES[1]] if group == "overall" or trade["pair"] == group]
        for name, reference in (("improvement", baseline), ("absolute_candidate", [])):
            tests[name][group] = {scenario: adjusted_bootstrap(
                daily_differences(reference, candidate, dates, scenario), iterations=iterations)
                for scenario in SCENARIOS}
        print(f"{group}: fixed bootstrap diagnostics complete", flush=True)
    failures = evaluate_gates(periods, tests)
    oos_summary = {strategy: {scenario: summarize(oos[strategy], scenario) for scenario in SCENARIOS}
                   for strategy in STRATEGIES}
    report = {"experiment": "EXP-003", "run_started_at": started,
              "generated_at": datetime.now(timezone.utc).isoformat(),
              "research_status": "REJECTED" if failures else "COMPONENT_PASS_ONLY",
              "promotion_status": "HOLD", "scope": "Daily TA/OCO entry-filter component; NOT full production strategy",
              "unseen_independent_test": False, "baseline_reconciled": True,
              "known_comparison_family": {"count": KNOWN_COMPARISONS,
                                          "prior_EXP_005_primary": 11, "current_primary": 22,
                                          "covers_all_historical_search": False},
              "input_sha256": hashes, "config": config, "data_quality": data["quality"],
              "signal_diagnostics": diagnostics, "periods": periods, "oos_overall": oos_summary,
              "tests": tests, "gate_failures": failures}
    ledger_path = OUTPUT.with_name(OUTPUT.name + "-trades.json.gz")
    ledger_path.write_bytes(gzip.compress(json.dumps(ledger, sort_keys=True, allow_nan=False,
                                                    separators=(",", ":")).encode("utf-8"), mtime=0))
    report["simulated_ledger"] = {"file": ledger_path.name, "sha256": digest(ledger_path),
                                  "total_trades": sum(len(result["trades"]) for pairs in ledger.values()
                                                      for period in pairs.values() for result in period.values())}
    OUTPUT.with_suffix(".json").write_text(json.dumps(report, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    OUTPUT.with_suffix(".md").write_text(markdown(report), encoding="utf-8")
    print(json.dumps({"status": report["research_status"], "oos": oos_summary, "failures": failures},
                     ensure_ascii=False, indent=2), flush=True)
    return report


if __name__ == "__main__":
    argparse.ArgumentParser(description=__doc__).parse_args()
    run()
