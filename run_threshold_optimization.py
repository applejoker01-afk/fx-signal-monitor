#!/usr/bin/env python3
"""
run_threshold_optimization.py
中長期シグナルのエントリー閾値（★判定のTA/FAスコア下限）を比較最適化する。

現状: ta>=60 AND fa>=55（★4以上）→ SL率44%（91/3/0/75）
問い: 閾値を上げて精度を高めれば、SLが減ってPFが上がるか？
      それとも機会損失で総利益が減るか？

複数の閾値パターンを同じ過去データで検証し、
トレード数・勝率・PF・SL率・合計pipsを比較する。
"""

import os
from datetime import datetime, timezone

from signal_scanner import compute_ta_score, fetch_history, PAIR_API
from modules.rate_fetcher import fetch_live_central_bank_rates, compute_fa_score
from modules.backtest import run_full_backtest

# 検証する閾値パターン (TAロング下限, FAロング下限)
THRESHOLD_PATTERNS = {
    "P1_現状(ta60/fa55)": (60, 55),
    "P2_★5相当(ta75/fa65)": (75, 65),
    "P3_TA厳格(ta70/fa55)": (70, 55),
    "P4_FA厳格(ta60/fa65)": (60, 65),
    "P5_両やや厳(ta68/fa60)": (68, 60),
}


def main():
    print("=" * 64)
    print("中長期 エントリー閾値最適化（★精度を上げてSLを減らせるか）")
    print("=" * 64)

    lookback = int(os.environ.get("BACKTEST_DAYS", "180"))
    cb_rates = fetch_live_central_bank_rates()

    print(f"[INFO] {len(PAIR_API)}ペアの履歴取得中...")
    all_histories = {}
    for pair in PAIR_API:
        prices = fetch_history(pair, 280)
        if prices and len(prices) >= 60:
            all_histories[pair] = prices
    print(f"[INFO] {len(all_histories)}ペア取得完了\n")

    results = {}
    for name, th in THRESHOLD_PATTERNS.items():
        bt = run_full_backtest(
            all_histories, compute_ta_score, compute_fa_score,
            cb_rates, PAIR_API, lookback_days=lookback,
            ta_thresholds=th,
        )
        ov = bt.get("overall", {})
        if not ov or ov.get("total", 0) == 0:
            print(f"  {name}: トレードなし")
            continue
        sl_rate = ov["sl_hits"] / ov["total"] * 100 if ov["total"] else 0
        results[name] = {**ov, "sl_rate": round(sl_rate, 1)}
        print(f"  {name}")
        print(f"    総{ov['total']}件 勝率{ov['win_rate']}% PF{ov['profit_factor']} "
              f"合計{ov['total_pips']}pips SL率{sl_rate:.1f}%")
        print(f"    TP1/TP2/TP3/SL: {ov['tp1_hits']}/{ov['tp2_hits']}/"
              f"{ov['tp3_hits']}/{ov['sl_hits']}")

    # ランキング
    print("\n" + "=" * 64)
    print("【比較】")
    print("=" * 64)
    if results:
        print("\nプロフィットファクター順:")
        for name, ov in sorted(results.items(), key=lambda x: x[1]["profit_factor"], reverse=True):
            print(f"  PF{ov['profit_factor']:<5} {name}  "
                  f"(勝率{ov['win_rate']}% SL率{ov['sl_rate']}% {ov['total']}件 {ov['total_pips']}pips)")

        print("\n合計pips順:")
        for name, ov in sorted(results.items(), key=lambda x: x[1]["total_pips"], reverse=True):
            print(f"  {ov['total_pips']:+8.1f}pips {name}  (PF{ov['profit_factor']} {ov['total']}件)")

        cur = results.get("P1_現状(ta60/fa55)")
        best_pf = max(results.items(), key=lambda x: x[1]["profit_factor"])
        best_pips = max(results.items(), key=lambda x: x[1]["total_pips"])
        print(f"\n→ PF最高: 【{best_pf[0]}】 PF{best_pf[1]['profit_factor']}")
        print(f"→ pips最高: 【{best_pips[0]}】 {best_pips[1]['total_pips']}pips")
        if cur:
            print(f"\n現状(P1): PF{cur['profit_factor']} 勝率{cur['win_rate']}% "
                  f"SL率{cur['sl_rate']}% {cur['total']}件 {cur['total_pips']}pips")
            print("\n判断の目安:")
            print("  ・閾値を上げてPF・勝率が上がり、pipsも維持/増 → 厳格化する価値あり")
            print("  ・PFは上がるがpips激減（機会損失） → 現状維持が無難")
            print("  ・ほぼ変化なし → 現状でOK")


if __name__ == "__main__":
    main()
