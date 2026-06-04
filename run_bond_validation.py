#!/usr/bin/env python3
"""
run_bond_validation.py
米10年債（FRED DGS10）をFAスコアに統合した場合の効果を検証する。

検証方法:
  バックテストの各エントリー時点で、その日の米10年債トレンド（up/down）を判定し、
  FAスコアに±5点の債券補正を加味する。
  「債券補正あり」vs「補正なし」でPF・勝率・合計pipsを比較。

債券トレンド判定:
  各時点で、米10年債の直近10営業日の傾きを見る。
  上昇傾向→"up"、下降傾向→"down"。

GitHub Actionsから実行。FRED APIキー（環境変数 FRED）が必要。
"""

import os
from datetime import datetime, timezone, date, timedelta

from signal_scanner import compute_ta_score, fetch_history, PAIR_API
from modules.rate_fetcher import (
    fetch_live_central_bank_rates, compute_fa_score,
    fetch_fred_series_history,
)
from modules.backtest import run_full_backtest


def build_bond_trend_map(history, window=10):
    """
    米10年債の時系列(date_str→値)から、各日付のトレンド(up/down/None)を作る。
    直近window営業日の傾きで判定。
    """
    dates = sorted(history.keys())
    trend_map = {}
    for i, d in enumerate(dates):
        if i < window:
            trend_map[d] = None
            continue
        recent = [history[dates[j]] for j in range(i - window, i + 1)]
        slope = recent[-1] - recent[0]
        if slope > 0.05:
            trend_map[d] = "up"
        elif slope < -0.05:
            trend_map[d] = "down"
        else:
            trend_map[d] = None
    return trend_map


def main():
    print("=" * 64)
    print("米10年債 FA統合 効果検証（統合あり vs なし）")
    print("=" * 64)

    api_key = os.environ.get("FRED")
    if not api_key:
        print("[ERROR] FRED APIキーが必要です（環境変数 FRED）")
        return

    lookback = int(os.environ.get("BACKTEST_DAYS", "180"))
    cb_rates = fetch_live_central_bank_rates()

    # 米10年債の過去履歴を取得
    print("[INFO] 米10年債(DGS10)の履歴を取得中...")
    dgs10 = fetch_fred_series_history("DGS10", api_key, days=lookback + 60)
    if not dgs10:
        print("[ERROR] 米10年債データ取得失敗")
        return
    print(f"[INFO] {len(dgs10)}営業日分の米10年債データを取得")
    trend_map = build_bond_trend_map(dgs10)

    # 現在のトレンド（参考表示）
    latest_dates = sorted(trend_map.keys())[-5:]
    print(f"[INFO] 直近の米10年債トレンド: "
          f"{[(d, trend_map[d]) for d in latest_dates]}")

    print(f"\n[INFO] {len(PAIR_API)}ペアの履歴取得中...")
    all_histories = {}
    for pair in PAIR_API:
        prices = fetch_history(pair, 280)
        if prices and len(prices) >= 60:
            all_histories[pair] = prices
    print(f"[INFO] {len(all_histories)}ペア取得完了\n")

    # FAスコア関数のラッパー: 債券補正あり/なし
    def fa_without_bond(pair, pair_api, cb):
        return compute_fa_score(pair, pair_api, cb, bond_trend=None)

    # 債券補正あり: 「現在のトレンド」を全期間に適用（近似）
    # ※厳密には各時点の日付が必要だが、backtestは時点別FAを渡せないため
    #   直近の支配的トレンドで近似する
    recent_trends = [trend_map[d] for d in sorted(trend_map.keys())[-30:]
                     if trend_map[d]]
    dominant = None
    if recent_trends:
        ups = recent_trends.count("up")
        downs = recent_trends.count("down")
        dominant = "up" if ups > downs else ("down" if downs > ups else None)
    print(f"[INFO] 直近30営業日の支配的トレンド: {dominant}\n")

    def fa_with_bond(pair, pair_api, cb):
        return compute_fa_score(pair, pair_api, cb, bond_trend=dominant)

    # バックテスト実行
    results = {}
    for label, fa_fn in [("債券補正なし", fa_without_bond),
                         ("債券補正あり", fa_with_bond)]:
        bt = run_full_backtest(
            all_histories, compute_ta_score, fa_fn,
            cb_rates, PAIR_API, lookback_days=lookback,
        )
        ov = bt.get("overall", {})
        results[label] = ov
        if ov:
            print(f"  {label}: 総{ov['total']}件 勝率{ov['win_rate']}% "
                  f"PF{ov['profit_factor']} 合計{ov['total_pips']}pips "
                  f"(TP1/2/3/SL: {ov['tp1_hits']}/{ov['tp2_hits']}/"
                  f"{ov['tp3_hits']}/{ov['sl_hits']})")

    # 比較
    print("\n" + "=" * 64)
    print("【比較】")
    print("=" * 64)
    a = results.get("債券補正なし", {})
    b = results.get("債券補正あり", {})
    if a and b:
        d_pips = b.get("total_pips", 0) - a.get("total_pips", 0)
        d_pf = b.get("profit_factor", 0) - a.get("profit_factor", 0)
        d_wr = b.get("win_rate", 0) - a.get("win_rate", 0)
        print(f"  合計pips: {a.get('total_pips')} → {b.get('total_pips')} ({d_pips:+.1f})")
        print(f"  PF:       {a.get('profit_factor')} → {b.get('profit_factor')} ({d_pf:+.2f})")
        print(f"  勝率:     {a.get('win_rate')}% → {b.get('win_rate')}% ({d_wr:+.1f})")
        if d_pips > 5 and d_pf >= 0:
            print("\n  ✅ 債券補正で改善。統合する価値あり")
        elif d_pips < -5 or d_pf < -0.1:
            print("\n  ❌ 債券補正で悪化。統合は見送り推奨")
        else:
            print("\n  ➖ ほぼ変化なし（誤差レベル）。現状維持でもOK")
        print("\n  ※注: 債券トレンドは『直近の支配的トレンド』で近似。")
        print("       厳密な時点別検証ではないため、参考値として解釈してください。")


if __name__ == "__main__":
    main()
