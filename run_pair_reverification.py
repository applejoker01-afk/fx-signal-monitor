#!/usr/bin/env python3
"""
run_pair_reverification.py
2026-08-25: AUDJPYの三重ペナルティ（過熱降格/8月季節性/実運用実績調整）の再検証と、
JPYクロス以外のペア（EURUSD, GBPUSD, AUDUSD, USDCAD, EURGBP, EURAUD等）の
バックテスト未実施状態の解消を目的としたアドホック検証スクリプト。

run_pair_tuning_experiment.py と同じ compute_ta_score/compute_fa_score/run_backtest
（本番と同一のTA/FAロジック、ただしTP1/2/3多段階の簡略バックテストエンジン）を再利用する。

出力:
  1. 全ペア V0(ta60/fa55) 180日窓バックテストを JPYクロス/非JPYクロスに分けて一覧
  2. AUDJPY: 280日全体・直近60日・月別（LONGのみ）の内訳
     -> 8月季節性フィルタの根拠（過去20-24年の外部統計）が、この戦略のシグナル
        条件下でのバックテストでも再現するかを確認する
"""

import json
from collections import defaultdict
from datetime import datetime, timedelta, timezone

from signal_scanner import compute_ta_score, fetch_history, PAIR_API
from modules.rate_fetcher import fetch_live_central_bank_rates, compute_fa_score
from modules.backtest import run_backtest

DEFAULT_TH = (60, 55)
JPY_PAIRS = sorted(p for p in PAIR_API if p.endswith("JPY"))
NON_JPY_PAIRS = sorted(p for p in PAIR_API if not p.endswith("JPY"))


def fmt_row(pair, r):
    if not r or r.get("total", 0) == 0:
        return f"    {pair:<8} トレードなし"
    sl_rate = round(r.get("sl_hits", 0) / r["total"] * 100, 1)
    return (f"    {pair:<8} 勝率{r['win_rate']:>5}% PF{r['profit_factor']:<6} "
            f"{r['total_pips']:+8.2f}pips {r['total']:>3}件 SL率{sl_rate}%")


def aggregate(trades):
    if not trades:
        return {}
    total = len(trades)
    wins = sum(1 for t in trades if t["result"] == "WIN")
    gross_p = sum(t["pips"] for t in trades if t["pips"] > 0)
    gross_l = abs(sum(t["pips"] for t in trades if t["pips"] < 0))
    return {
        "total": total,
        "wins": wins,
        "win_rate": round(wins / total * 100, 1),
        "profit_factor": round(gross_p / gross_l, 2) if gross_l > 0 else 999.0,
        "total_pips": round(sum(t["pips"] for t in trades), 2),
        "sl_rate": round(sum(1 for t in trades if t["exit_reason"] == "SL_HIT") / total * 100, 1),
    }


def main():
    print("=" * 72)
    print("ペア再検証: AUDJPY三重ペナルティ + 非JPYクロス初回バックテスト")
    print("=" * 72)

    cb_rates = fetch_live_central_bank_rates()

    print(f"[INFO] {len(PAIR_API)}ペアの履歴取得中(280日)...")
    histories = {}
    for pair in PAIR_API:
        prices = fetch_history(pair, 280)
        if prices and len(prices) >= 60:
            histories[pair] = prices
        else:
            print(f"  [SKIP] {pair}: 履歴不足")
    print(f"[INFO] {len(histories)}/{len(PAIR_API)}ペア取得完了")

    # ------------------------------------------------------------
    # 1) 全ペア V0(180日窓) 一覧、JPYクロス/非JPYクロスに分割
    # ------------------------------------------------------------
    print("\n" + "─" * 72)
    print("【全ペア 180日窓・V0(ta60/fa55) 一覧】")
    by_pair = {}
    for pair, prices in histories.items():
        r = run_backtest(pair, prices, compute_ta_score, compute_fa_score,
                          cb_rates, PAIR_API, lookback_days=180, ta_thresholds=DEFAULT_TH)
        by_pair[pair] = r

    print("\n  -- JPYクロス --")
    for p in JPY_PAIRS:
        print(fmt_row(p, by_pair.get(p)))

    print("\n  -- 非JPYクロス（新規検証） --")
    for p in NON_JPY_PAIRS:
        print(fmt_row(p, by_pair.get(p)))

    jpy_trades = [t for p in JPY_PAIRS for t in by_pair.get(p, {}).get("trades", [])]
    non_jpy_trades = [t for p in NON_JPY_PAIRS for t in by_pair.get(p, {}).get("trades", [])]
    print("\n  -- グループ集計 --")
    jpy_agg = aggregate(jpy_trades)
    non_jpy_agg = aggregate(non_jpy_trades)
    print(f"    JPYクロス計    : {jpy_agg}")
    print(f"    非JPYクロス計  : {non_jpy_agg}")

    # ------------------------------------------------------------
    # 2) AUDJPY再検証: 期間別・月別（LONGのみ）
    # ------------------------------------------------------------
    print("\n" + "─" * 72)
    print("【AUDJPY再検証】")
    aud_prices = histories.get("AUDJPY")
    if not aud_prices:
        print("  [ERROR] AUDJPY履歴取得失敗")
    else:
        for lookback, label in [(280, "全期間(280日)"), (90, "直近90日"), (30, "直近30日")]:
            r = run_backtest("AUDJPY", aud_prices, compute_ta_score, compute_fa_score,
                              cb_rates, PAIR_API, lookback_days=lookback, ta_thresholds=DEFAULT_TH)
            print(f"  {label:<12}: " + fmt_row("AUDJPY", r).strip())

        # 月別内訳（LONGのみ、8月季節性フィルタの検証対象）
        r_full = run_backtest("AUDJPY", aud_prices, compute_ta_score, compute_fa_score,
                               cb_rates, PAIR_API, lookback_days=280, ta_thresholds=DEFAULT_TH)
        trades = r_full.get("trades", [])
        n = len(aud_prices)
        end_date = datetime.now(timezone.utc).date()
        start_date = end_date - timedelta(days=280)

        def idx_to_date(idx):
            # full_prices は日次終値の連番なので、start_dateからのオフセットで近似
            return start_date + timedelta(days=idx)

        month_bucket = defaultdict(list)
        for t in trades:
            if t["direction"] != "LONG":
                continue
            d = idx_to_date(t["entry_idx"])
            month_bucket[d.month].append(t)

        print("\n  [AUDJPY LONGのみ・月別内訳(エントリー月ベース、近似日付)]")
        for month in sorted(month_bucket):
            mt = month_bucket[month]
            agg = aggregate(mt)
            marker = " ← 8月季節性フィルタ対象" if month == 8 else ""
            print(f"    {month:>2}月: 勝率{agg.get('win_rate','-')}% "
                  f"{agg.get('total_pips',0):+7.2f}pips {agg.get('total',0)}件{marker}")

        # LONG vs SHORT 比較（BOJ局面フィルタがLONGのみを狙い撃ちしている前提の検証）
        long_trades = [t for t in trades if t["direction"] == "LONG"]
        short_trades = [t for t in trades if t["direction"] == "SHORT"]
        print("\n  [AUDJPY 280日 LONG vs SHORT]")
        print(f"    LONG : {aggregate(long_trades)}")
        print(f"    SHORT: {aggregate(short_trades)}")

    # ------------------------------------------------------------
    # 3) FA中立帯でのTA単独運用検証（非JPYクロスの継続検証）
    #    2026-08-25: 上記①でトレードなしだった非JPY非CHFペアについて、
    #    FAがneutralの間だけTAスコア単独でエントリーを許可した場合の成績を見る。
    # ------------------------------------------------------------
    print("\n" + "─" * 72)
    print("【FA中立帯 TA単独運用 検証（非JPYクロス）】")
    zero_trade_pairs = [p for p in NON_JPY_PAIRS if not by_pair.get(p, {}).get("total")]
    print(f"  対象（①でトレードなしだったペア）: {zero_trade_pairs}")

    ta_only_results = {}
    for pair in zero_trade_pairs:
        prices = histories.get(pair)
        if not prices:
            continue
        r = run_backtest(pair, prices, compute_ta_score, compute_fa_score,
                          cb_rates, PAIR_API, lookback_days=180, ta_thresholds=DEFAULT_TH,
                          allow_ta_only_when_fa_neutral=True)
        ta_only_results[pair] = r
        print(fmt_row(pair, r))

    all_ta_only_trades = [t for r in ta_only_results.values() for t in r.get("trades", [])]
    print(f"\n  -- TA単独運用 グループ集計 --\n    {aggregate(all_ta_only_trades)}")

    # 比較用: 既に取引が出ているCHF系ペアも同条件で参考表示（FA一致 vs TA単独混在時の目安）
    print("\n  [参考: 既存CHF系ペアの同条件(allow_ta_only_when_fa_neutral=True)再実行]")
    for pair in ["AUDCHF", "EURCHF", "GBPCHF", "USDCHF"]:
        prices = histories.get(pair)
        if not prices:
            continue
        r = run_backtest(pair, prices, compute_ta_score, compute_fa_score,
                          cb_rates, PAIR_API, lookback_days=180, ta_thresholds=DEFAULT_TH,
                          allow_ta_only_when_fa_neutral=True)
        print(fmt_row(pair, r))

    print("\n[OK] 検証完了")


if __name__ == "__main__":
    main()
