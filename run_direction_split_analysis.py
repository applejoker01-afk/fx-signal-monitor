#!/usr/bin/env python3
"""
run_direction_split_analysis.py
LONG（買い）とSHORT（売り）を切り分けて、それぞれのPF・勝率・pipsを比較する。

背景: MACD＋RSI＋一目均衡表など追加指標をAND条件で積み増すと、機会損失に
ならないか？という検討の中で、「全トレードを一括りにしたPFでは、買い方向
と売り方向のどちらに本当のエッジがあるのか分からない」という指摘を受けて
作成（2026-08-11）。もし片方向だけが弱ければ、両方向に一律で指標を追加
するより、弱い方向だけ絞る方が機会損失が少ない可能性がある。

現状のエントリー閾値（TA>=60, FA>=55）のまま、方向別に集計する。
"""

import os

from signal_scanner import compute_ta_score, fetch_history, PAIR_API
from modules.rate_fetcher import fetch_live_central_bank_rates, compute_fa_score
from modules.backtest import run_full_backtest, summarize_by_direction
from modules.performance_intelligence import PAIR_EXCLUDE


def main():
    print("=" * 64)
    print("LONG / SHORT 方向別 PF比較（現状閾値 TA>=60 / FA>=55、除外ペア反映）")
    print("=" * 64)

    lookback = int(os.environ.get("BACKTEST_DAYS", "180"))
    cb_rates = fetch_live_central_bank_rates()

    # 2026-08-11: 実運用ではPAIR_EXCLUDE（EURUSD/USDCHF/NZDJPY/CADJPY/PLNJPY等）
    # がハードブロックされているが、run_full_backtestはPAIR_API全体を渡すと
    # 除外なしで計算してしまう。実際に取引され得るペアだけで数字を洗い直す。
    tradable_pairs = {p: v for p, v in PAIR_API.items() if p not in PAIR_EXCLUDE}
    print(f"[INFO] 除外リスト: {sorted(PAIR_EXCLUDE)}")
    print(f"[INFO] {len(PAIR_API)}ペア中 {len(tradable_pairs)}ペアが取引対象\n")

    print(f"[INFO] {len(tradable_pairs)}ペアの履歴取得中...")
    all_histories = {}
    for pair in tradable_pairs:
        prices = fetch_history(pair, 280)
        if prices and len(prices) >= 60:
            all_histories[pair] = prices
    print(f"[INFO] {len(all_histories)}ペア取得完了\n")

    bt = run_full_backtest(
        all_histories, compute_ta_score, compute_fa_score,
        cb_rates, tradable_pairs, lookback_days=lookback,
    )

    split = summarize_by_direction(bt["by_pair"])

    print("【全体】")
    ov = bt["overall"]
    print(f"  総{ov['total']}件 勝率{ov['win_rate']}% PF{ov['profit_factor']} "
          f"合計{ov['total_pips']}pips\n")

    print("【方向別】")
    for d in ("LONG", "SHORT"):
        s = split[d]
        if s.get("total", 0) == 0:
            print(f"  {d}: トレードなし")
            continue
        print(f"  {d}: 総{s['total']}件 勝率{s['win_rate']}% PF{s['profit_factor']} "
              f"合計{s['total_pips']}pips (勝{s['wins']}/負{s['losses']})")

    print()
    print("【ペア別・方向別 内訳（5件以上のみ）】")
    rows = []
    for pair, dirs in split["by_pair_direction"].items():
        for d, r in dirs.items():
            if r.get("total", 0) >= 5:
                rows.append((pair, d, r["total"], r["win_rate"], r["profit_factor"], r["total_pips"]))
    rows.sort(key=lambda x: -x[4])  # PF降順
    for pair, d, total, wr, pf, pips in rows:
        print(f"  {pair:8s} {d:5s} 総{total:3d}件 勝率{wr:5.1f}% PF{pf:5.2f} {pips:+8.2f}pips")

    print()
    print("=" * 64)
    print("判断の目安:")
    print("  ・LONG/SHORTでPFに大きな差 → 弱い方向だけ閾値を上げる/除外する方が")
    print("    両方向一律に指標を追加するより機会損失が少ない可能性")
    print("  ・両方向とも同程度 → 方向は関係なく、指標追加自体の効果を疑うべき")
    print("=" * 64)


if __name__ == "__main__":
    main()
