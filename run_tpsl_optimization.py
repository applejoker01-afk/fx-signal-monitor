#!/usr/bin/env python3
"""
run_tpsl_optimization.py
中長期シグナルのTP/SL設定（ATR乗数）を複数パターンで比較最適化する。

現状のTP/SL内訳の問題:
  TP1=93件, TP2=3件, TP3=0件 → 利を伸ばせていない（TP3が遠すぎる）

複数の (SL, TP1, TP2, TP3) ATR乗数を同じ過去データで検証し、
プロフィットファクター・合計pips・TP内訳のバランスが最良の設定を探す。

GitHub Actionsから実行。signal_scanner.py のロジックを再利用。
"""

import os
from datetime import datetime, timezone

from signal_scanner import (
    compute_ta_score, fetch_history, PAIR_API,
)
from modules.rate_fetcher import load_central_bank_rates, compute_fa_score
from modules.backtest import run_full_backtest

# 検証するTP/SL設定（SL, TP1, TP2, TP3 のATR乗数）
TPSL_PATTERNS = {
    "A_現状(2.5/2.5/5.0/8.5)": (2.5, 2.5, 5.0, 8.5),
    "B_SL広め(3.5/2.5/5.0/8.5)": (3.5, 2.5, 5.0, 8.5),
    "C_TP近め(2.5/2.0/3.5/5.0)": (2.5, 2.0, 3.5, 5.0),
    "D_損小利大(2.0/3.0/5.0/7.0)": (2.0, 3.0, 5.0, 7.0),
    "E_バランス(3.0/3.0/4.5/6.0)": (3.0, 3.0, 4.5, 6.0),
    "F_利伸ばし(3.0/2.5/4.0/6.0)": (3.0, 2.5, 4.0, 6.0),
}


def main():
    print("=" * 64)
    print("中長期 TP/SL最適化検証（複数のATR乗数を比較）")
    print("=" * 64)

    lookback = int(os.environ.get("BACKTEST_DAYS", "180"))
    cb_rates = load_central_bank_rates()

    print(f"[INFO] {len(PAIR_API)}ペアの履歴取得中...")
    all_histories = {}
    for pair in PAIR_API:
        prices = fetch_history(pair, 280)
        if prices and len(prices) >= 60:
            all_histories[pair] = prices
    print(f"[INFO] {len(all_histories)}ペア取得完了\n")

    results = {}
    for name, mult in TPSL_PATTERNS.items():
        bt = run_full_backtest(
            all_histories, compute_ta_score, compute_fa_score,
            cb_rates, PAIR_API, lookback_days=lookback,
            atr_multipliers=mult,
        )
        ov = bt.get("overall", {})
        if not ov:
            print(f"  {name}: 結果なし")
            continue
        results[name] = ov
        print(f"  {name}")
        print(f"    総{ov['total']}件 勝率{ov['win_rate']}% "
              f"PF{ov['profit_factor']} 合計{ov['total_pips']}pips")
        print(f"    TP1/TP2/TP3/SL: {ov['tp1_hits']}/{ov['tp2_hits']}/"
              f"{ov['tp3_hits']}/{ov['sl_hits']}")

    # 最良設定の判定（合計pips と PF の両方で）
    print("\n" + "=" * 64)
    print("【ランキング】")
    print("=" * 64)
    if results:
        by_pips = sorted(results.items(), key=lambda x: x[1]["total_pips"], reverse=True)
        by_pf = sorted(results.items(), key=lambda x: x[1]["profit_factor"], reverse=True)
        print("\n合計pips順:")
        for name, ov in by_pips:
            print(f"  {ov['total_pips']:+8.1f}pips  {name}  (PF{ov['profit_factor']})")
        print("\nプロフィットファクター順:")
        for name, ov in by_pf:
            print(f"  PF{ov['profit_factor']:<5}  {name}  ({ov['total_pips']:+.1f}pips)")

        best = by_pips[0]
        print(f"\n→ 合計pips最大: 【{best[0]}】 {best[1]['total_pips']:+.1f}pips・PF{best[1]['profit_factor']}")
        cur = results.get("A_現状(2.5/2.5/5.0/8.5)")
        if cur:
            improve = best[1]["total_pips"] - cur["total_pips"]
            print(f"  現状比: {improve:+.1f}pips の改善")


if __name__ == "__main__":
    main()
