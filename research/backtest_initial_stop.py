"""EXP-005, an offline initial-stop component test, NOT a live-system backtest.

Run from the repository root after download_daily_history.py. All outputs stay
under research/. No network, production state writes, notifications or orders.
"""

import argparse
import gzip
import hashlib
import json
import math
import random
import statistics
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from modules.advanced_analytics import calc_staged_tp, detect_volatility_regime
from modules.profitability import cost_snapshot, load_config, pip_size, validate_geometry
from modules.spread_monitor import SPREAD_PIPS_BASE
from signal_scanner import compute_ta_score
from research.download_daily_history import CACHE, PAIRS


OUTPUT = ROOT / "research" / "results" / "2026-10-01-initial-stop-backtest"
VARIANTS = ("legacy_chandelier", "fixed_initial_atr")
PERIODS = {"reference_2017_2021": ("2017-01-01", "2021-12-31"),
           **{f"test_{year}": (f"{year}-01-01", f"{year}-12-31") for year in range(2022, 2026)}}
SCENARIOS = ("standard", "adverse")


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def make_levels(pair, closes, ta, variant):
    direction = "LONG" if ta["ta_score"] >= 65 else (
        "SHORT" if ta["ta_score"] <= 35 else None)
    if direction is None:
        return None
    decimals = 3 if pair.endswith("JPY") else 6
    atr = round(ta.get("atr") or 0, decimals)
    if atr <= 0:
        return None
    price = round(closes[-1], decimals)
    regime = detect_volatility_regime(closes, atr)
    staged = calc_staged_tp(price, direction, atr, regime, prices=closes, pair=pair)
    stop = staged["sl"]
    chandelier_used = False
    if variant == "legacy_chandelier" and len(closes) >= 22:
        width = atr * regime["sl_multiplier"]
        # Exactly the pre-PR initial narrowing condition, before rounding.
        fixed_stop = price - width if direction == "LONG" else price + width
        ce = max(closes[-22:]) - width if direction == "LONG" else min(closes[-22:]) + width
        if ((direction == "LONG" and fixed_stop < ce < price)
                or (direction == "SHORT" and price < ce < fixed_stop)):
            stop, chandelier_used = round(ce, decimals), True
    return {"direction": direction, "stop": stop, "target": staged["tp"],
            "chandelier_used": chandelier_used, "ta_score": ta["ta_score"],
            "atr": atr, "regime": regime["regime"]}


def prepare_signals(pair, bars, config):
    """Every feature and cost comes only from this or earlier completed bars."""
    closes, signals = [], [None] * len(bars)
    for index, bar in enumerate(bars):
        closes.append(bar["close"])
        if index < 279:
            continue
        window = closes[-280:]
        ta = compute_ta_score(window[-1], window)
        levels = {variant: make_levels(pair, window, ta, variant) for variant in VARIANTS}
        if levels[VARIANTS[0]] is None:
            continue
        spread_pips = max(SPREAD_PIPS_BASE.get(pair, 5.0), bar["spread_close_price"] / pip_size(pair))
        costs = cost_snapshot(pair, spread_pips=spread_pips,
                              now=datetime.fromisoformat(bar["date"]).replace(tzinfo=timezone.utc),
                              config=config)
        signals[index] = {"date": bar["date"], "levels": levels, "costs": costs}
    return signals


def simulate_period(pair, bars, signals, variant, start, end, max_hold=20):
    selected = [index for index, bar in enumerate(bars) if start <= bar["date"] <= end]
    if not selected:
        return {"trades": [], "skipped_geometry": 0, "ambiguous_stop_target_bars": 0}
    results, active, skipped, ties = [], None, 0, 0
    for index in selected:
        bar = bars[index]
        if active is None and index > 0 and signals[index - 1]:
            signal = signals[index - 1]
            levels = signal["levels"][variant]
            entry = bar["open"]
            if validate_geometry(levels["direction"], entry, levels["stop"], levels["target"], True):
                skipped += 1
            else:
                standard_cost = signal["costs"]["standard_price_cost"]
                active = {"pair": pair, "variant": variant, "signal_date": signal["date"],
                          "entry_date": bar["date"], "entry_index": index,
                          "entry_price": entry, "initial_sl": levels["stop"],
                          "target": levels["target"], **levels, "costs": signal["costs"],
                          "planned_standard_risk_price": abs(entry - levels["stop"]) + standard_cost}
        if active is None:
            continue
        sign = 1 if active["direction"] == "LONG" else -1
        stop, target = active["stop"], active["target"]
        hit_stop = bar["low"] <= stop if sign == 1 else bar["high"] >= stop
        hit_target = bar["high"] >= target if sign == 1 else bar["low"] <= target
        exit_price, reason = None, None
        if hit_stop:
            # A stop order cannot fill at an unavailable better price after a gap.
            exit_price = min(stop, bar["open"]) if sign == 1 else max(stop, bar["open"])
            reason = "SL_GAP" if exit_price != stop else "SL"
            ties += int(hit_target)
        elif hit_target:
            exit_price, reason = target, "TP"
        elif index - active["entry_index"] + 1 >= max_hold:
            exit_price, reason = bar["close"], "TIME_20_BARS"
        elif index == selected[-1]:
            exit_price, reason = bar["close"], "PERIOD_END"
        if reason:
            risk = active["planned_standard_risk_price"]
            gross = sign * (exit_price - active["entry_price"])
            active.update({"exit_date": bar["date"], "exit_index": index,
                           "exit_price": exit_price, "exit_reason": reason,
                           "holding_bars": index - active["entry_index"] + 1,
                           "gross_price_change": gross,
                           **{f"{scenario}_net_r": (gross - active["costs"][f"{scenario}_price_cost"]) / risk
                              for scenario in SCENARIOS}})
            results.append(active)
            active = None
    return {"trades": results, "skipped_geometry": skipped,
            "ambiguous_stop_target_bars": ties}


def summarize(trades, scenario):
    ordered = sorted(trades, key=lambda trade: (trade["exit_date"], trade["pair"], trade["entry_date"]))
    values = [trade[f"{scenario}_net_r"] for trade in ordered]
    gains = sum(max(0, value) for value in values)
    losses = -sum(min(0, value) for value in values)
    by_date = {}
    for trade in ordered:
        by_date[trade["exit_date"]] = by_date.get(trade["exit_date"], 0) + trade[f"{scenario}_net_r"]
    equity, peak, worst = 0.0, 0.0, 0.0
    for value in by_date.values():
        equity += value
        peak = max(peak, equity)
        worst = min(worst, equity - peak)
    return {"trades": len(values), "mean_net_r": statistics.mean(values) if values else None,
            "total_net_r": sum(values), "profit_factor": gains / losses if losses else None,
            "win_rate_pct": 100 * sum(value > 0 for value in values) / len(values) if values else None,
            "max_realized_drawdown_r": worst,
            "period_end_trades": sum(trade["exit_reason"] == "PERIOD_END" for trade in ordered),
            "exit_reasons": {reason: sum(trade["exit_reason"] == reason for trade in ordered)
                             for reason in sorted({trade["exit_reason"] for trade in ordered})}}


def daily_differences(baseline, candidate, dates, scenario):
    differences = dict.fromkeys(dates, 0.0)
    for sign, trades in ((-1, baseline), (1, candidate)):
        for trade in trades:
            differences[trade["exit_date"]] += sign * trade[f"{scenario}_net_r"]
    return [differences[date] for date in dates]


def bootstrap_difference(values, iterations=5000, block=20, seed=20261001):
    n = len(values)
    if not n:
        return {"days": 0, "p_one_sided": None, "mean_delta_r_per_day": None, "ci95": None}
    observed = statistics.mean(values)
    rng = random.Random(seed)
    boot = []
    block = min(block, n)
    for _ in range(iterations):
        total, used = 0.0, 0
        while used < n:
            start = rng.randrange(n)
            length = min(block, n - used)
            total += sum(values[(start + offset) % n] for offset in range(length))
            used += length
        boot.append(total / n)
    # Recenter resampled differences under H0: population mean <= 0.
    p = (1 + sum(value - observed >= observed for value in boot)) / (iterations + 1)
    boot.sort()
    return {"days": n, "mean_delta_r_per_day": observed, "total_delta_r": sum(values),
            "ci95": [boot[int(0.025 * iterations)], boot[min(iterations - 1, int(0.975 * iterations))]],
            "p_one_sided": p, "p_bonferroni_11": min(1.0, 11 * p),
            "block_trading_days": block, "iterations": iterations, "seed": seed}


def fmt(value, places=3):
    return "—" if value is None else f"{value:.{places}f}"


def markdown(report):
    candidate = report["oos_overall"][VARIANTS[1]]["standard"]
    lines = ["# EXP-005 初期SLの長期部品試験", "",
             f"実行日: {report['generated_at']} / 状態: {report['research_status']} / 本番採用: HOLD", "",
             f"検証期間の固定ATR案は{candidate['trades']:,}取引、通常コスト後の平均{fmt(candidate['mean_net_r'])}R、"
             f"PF {fmt(candidate['profit_factor'], 2)}。事前に定めた採用条件を満たしていない。", "",
             "今回のATR固定初期SLが、旧Chandelier初期SLより収益を改善するかを検証した。"
             "これはFA・トレーリング・指値・ポートフォリオ配分を省いたTA/OCOの部品試験であり、本番全体の成績ではない。", "",
             "## 固定条件とデータ", "",
             "Dukascopyの2016〜2025年UTC日足BID/ASK。2016年は準備、2017〜2021年は基準期間、"
             "2022〜2025年は年ごとの擬似OOS。10ペアを事前固定し、パラメータ探索やペア選別は行っていない。", "",
             "現行TA>=65/<=35、当日までの280終値、翌足始値エントリー、SL/TPのOCO、20取引日上限。"
             "同日SL/TP到達はSL優先、損切りギャップは始値決済、年末で閉じて翌年にリセットする。", "",
             "1R=初期SLまでの値幅+標準往復コスト。悪化コストでも数量を変えない。"
             "以下のDDは決済済みRの累積で、含み損・証拠金を含む口座DDではない。スワップは含まない。", "",
             "[結果を見る前の全条件](../initial_stop_protocol_20261001.md)", "",
             "## 年度別の比較（全10ペア）", "",
             "|期間|方式|件数|通常 平均R|通常 合計R|通常 PF|悪化 平均R|悪化 合計R|悪化 PF|悪化 最大DD(R)|",
             "|---|---|---:|---:|---:|---:|---:|---:|---:|---:|"]
    for label, period in report["periods"].items():
        for variant in VARIANTS:
            standard = period["overall"][variant]["standard"]
            adverse = period["overall"][variant]["adverse"]
            name = "旧Chandelier" if variant == VARIANTS[0] else "固定ATR"
            lines.append(f"|{label}|{name}|{standard['trades']}|{fmt(standard['mean_net_r'])}|"
                         f"{fmt(standard['total_net_r'])}|{fmt(standard['profit_factor'], 2)}|"
                         f"{fmt(adverse['mean_net_r'])}|{fmt(adverse['total_net_r'])}|"
                         f"{fmt(adverse['profit_factor'], 2)}|{fmt(adverse['max_realized_drawdown_r'], 2)}|")
    lines.extend(["", "## 2022〜2025年合計", "",
                  "|方式|件数|通常 平均R|通常 合計R|通常 PF|悪化 平均R|悪化 合計R|悪化 PF|", "|---|---:|---:|---:|---:|---:|---:|---:|"])
    for variant in VARIANTS:
        s, a = (report["oos_overall"][variant][scenario] for scenario in SCENARIOS)
        lines.append(f"|{variant}|{s['trades']}|{fmt(s['mean_net_r'])}|{fmt(s['total_net_r'])}|"
                     f"{fmt(s['profit_factor'], 2)}|{fmt(a['mean_net_r'])}|{fmt(a['total_net_r'])}|{fmt(a['profit_factor'], 2)}|")
    lines.extend(["", "## 同日差のブロック検定", "",
                  "通常/悪化ごとに、同日の全ペアの候補−基準Rをまとめ、20取引日ブロックで5,000回再抽出。"
                  "10ペア個別と全体の11比較を考慮し、Bonferroni補正。ペア別結果で対象を変更しない。", ""])
    for scenario in SCENARIOS:
        test = report["difference_tests"]["overall"][scenario]
        ci = test["ci95"]
        lines.append(f"- {scenario}: 合計差 {fmt(test['total_delta_r'])}R、1日平均差 {fmt(test['mean_delta_r_per_day'], 5)}R、"
                     f"95%区間 [{fmt(ci[0], 5)}, {fmt(ci[1], 5)}] R/日、片側p={fmt(test['p_one_sided'], 4)}、"
                     f"11比較補正p={fmt(test['p_bonferroni_11'], 4)}。")
    lines.extend(["", "## 判定と限界", ""])
    lines.extend(f"- {reason}" for reason in report["gate_failures"])
    lines.extend(["", "各年の学習期間を拡大して集計したが、しきい値や方式を学習成績で選び直していない。"
                  "2026年に書かれたルールを過去年で区切った擬似OOSで、将来の完全未見データとは異なる。", "",
                  "BID/ASK高安の平均は厳密な中値高安ではない。スプレッドは指示時点の終値で観測・固定し、"
                  "決済時の急拡大を完全には再現しない。滑り/手数料はモデル、スワップは未検証。"
                  "日足では実際の足内価格順序は不明。", "",
                  "本番のFA/ニュース/イベント/指値/トレーリング/反転/シグナル消失/リスク6%・3%上限を再現していない。"
                  "損切り価格の逆転や不正な建玉を防ぐ修正の妥当性と、利益が増える戦略の証明は分けて扱う。", "",
                  "## 再現と入力の同一性", "",
                  "```powershell", "rtk proxy python -B -X utf8 research/download_daily_history.py",
                  "rtk proxy python -B -X utf8 research/test_initial_stop.py",
                  "rtk proxy python -B -X utf8 research/backtest_initial_stop.py", "```", "",
                  f"価格SHA-256: `{report['input_sha256']['prices']}`", "",
                  f"取得元200ファイルのmanifest SHA-256: `{report['input_sha256']['manifest']}`", "",
                  "取得URL/時刻/個別SHA-256は `2026-10-01-initial-stop-backtest-sources.json`、"
                  "集計は `2026-10-01-initial-stop-backtest.json`、全仮想取引は容量を抑えた"
                  " `2026-10-01-initial-stop-backtest-trades.json.gz` に保存。元価格はGitに追加しない。", "",
                  "Dukascopy Bank SA, [Historical Price Data](https://www.dukascopy.com/wiki/en/development/data-export/)", ""])
    return "\n".join(lines)


def run(iterations=5000):
    data_path, manifest_path = CACHE / "daily_mid.json", CACHE / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if digest(data_path) != manifest["data_sha256"]:
        raise ValueError("Normalized price input failed SHA-256 verification")
    data = json.loads(data_path.read_text(encoding="utf-8"))
    config = load_config()
    periods = {name: {"range": bounds, "by_pair": {}, "overall": {}, "expanding_train_range":
                     ["2017-01-01", str(int(bounds[0][:4]) - 1) + "-12-31"] if name.startswith("test") else None}
               for name, bounds in PERIODS.items()}
    ledger, prepared, oos = {}, {}, {variant: [] for variant in VARIANTS}
    for pair in PAIRS:
        bars = data["results"][pair]
        if any(bars[index]["date"] <= bars[index - 1]["date"] for index in range(1, len(bars))):
            raise ValueError("Price timestamps not unique and increasing")
        signals = prepare_signals(pair, bars, config)
        prepared[pair] = signals
        ledger[pair] = {}
        for period_name, (start, end) in PERIODS.items():
            ledger[pair][period_name] = {}
            periods[period_name]["by_pair"][pair] = {}
            for variant in VARIANTS:
                simulation = simulate_period(pair, bars, signals, variant, start, end)
                ledger[pair][period_name][variant] = simulation
                periods[period_name]["by_pair"][pair][variant] = {
                    **{scenario: summarize(simulation["trades"], scenario) for scenario in SCENARIOS},
                    "skipped_geometry": simulation["skipped_geometry"],
                    "ambiguous_stop_target_bars": simulation["ambiguous_stop_target_bars"]}
                if period_name.startswith("test"):
                    oos[variant].extend(simulation["trades"])
        print(f"{pair}: {len(bars)} bars, signals {sum(signal is not None for signal in signals)}", flush=True)
    for name, period in periods.items():
        for variant in VARIANTS:
            trades = [trade for pair in PAIRS for trade in ledger[pair][name][variant]["trades"]]
            period["overall"][variant] = {scenario: summarize(trades, scenario) for scenario in SCENARIOS}
        if name.startswith("test"):
            train_end = period["expanding_train_range"][1]
            # Training periods are evaluated from empty state independently of
            # the held-out year; their positions are purged at train_end.
            period["expanding_train_metrics"] = {}
            for variant in VARIANTS:
                training = []
                for pair in PAIRS:
                    bars = data["results"][pair]
                    signals = prepared[pair]
                    training.extend(simulate_period(pair, bars, signals, variant,
                                                    "2017-01-01", train_end)["trades"])
                period["expanding_train_metrics"][variant] = {scenario: summarize(training, scenario)
                                                            for scenario in SCENARIOS}
    oos_summary = {variant: {scenario: summarize(oos[variant], scenario) for scenario in SCENARIOS}
                   for variant in VARIANTS}
    tests = {}
    for group in ("overall", *PAIRS):
        dates = sorted({bar["date"] for pair in PAIRS if group == "overall" or pair == group
                        for bar in data["results"][pair] if "2022-01-01" <= bar["date"] <= "2025-12-31"})
        baseline = [trade for trade in oos[VARIANTS[0]] if group == "overall" or trade["pair"] == group]
        candidate = [trade for trade in oos[VARIANTS[1]] if group == "overall" or trade["pair"] == group]
        tests[group] = {scenario: bootstrap_difference(daily_differences(baseline, candidate, dates, scenario),
                                                       iterations=iterations) for scenario in SCENARIOS}
    failures = []
    for name, period in periods.items():
        if not name.startswith("test"):
            continue
        for scenario in SCENARIOS:
            baseline = period["overall"][VARIANTS[0]][scenario]
            candidate = period["overall"][VARIANTS[1]][scenario]
            if candidate["trades"] < 30:
                failures.append(f"{name}/{scenario}: 候補の決済が30件未満。")
            if candidate["total_net_r"] <= 0:
                failures.append(f"{name}/{scenario}: 固定ATRのコスト後合計Rが正ではない。")
            if candidate["total_net_r"] <= baseline["total_net_r"]:
                failures.append(f"{name}/{scenario}: 基準の合計Rを改善していない。")
    adverse_test = tests["overall"]["adverse"]
    if adverse_test["p_bonferroni_11"] >= 0.05 or adverse_test["ci95"][0] <= 0:
        failures.append("悪化コストの全体差が、多重比較補正と95%区間の条件を満たさない。")
    report = {"experiment": "EXP-005", "generated_at": datetime.now(timezone.utc).isoformat(),
              "research_status": "REJECTED" if failures else "COMPONENT_PASS_ONLY",
              "promotion_status": "HOLD", "scope": "TA-only initial-stop/OCO component; NOT full production strategy",
              "input_sha256": {"prices": digest(data_path), "manifest": digest(manifest_path),
                               "protocol": digest(ROOT / "research" / "initial_stop_protocol_20261001.md"),
                               "configuration": digest(ROOT / "profitability_config.json"),
                               **{name: digest(ROOT / name) for name in ("signal_scanner.py", "modules/advanced_analytics.py",
                                                                         "modules/profitability.py", "modules/spread_monitor.py",
                                                                         "research/download_daily_history.py", "research/backtest_initial_stop.py")}},
              "config": config, "data_quality": data["quality"], "periods": periods,
              "oos_overall": oos_summary, "difference_tests": tests, "gate_failures": failures}
    ledger_path = OUTPUT.with_name(OUTPUT.name + "-trades.json.gz")
    ledger_path.write_bytes(gzip.compress(json.dumps(ledger, sort_keys=True, allow_nan=False,
                                                    separators=(",", ":")).encode("utf-8"), mtime=0))
    report["simulated_ledger"] = {"file": ledger_path.name, "sha256": digest(ledger_path),
                                  "total_trades": sum(len(result["trades"]) for pairs in ledger.values()
                                                      for period in pairs.values() for result in period.values())}
    OUTPUT.with_suffix(".json").write_text(json.dumps(report, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    OUTPUT.with_suffix(".md").write_text(markdown(report), encoding="utf-8")
    OUTPUT.with_name(OUTPUT.name + "-sources.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"status": report["research_status"], "oos": oos_summary, "failures": failures},
                     ensure_ascii=False, indent=2), flush=True)
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args()
    run()
