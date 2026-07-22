#!/usr/bin/env python3
"""レバー1実験の補助: V0(現状設定)の180日/30日ペア別内訳を全表示する。
新規追加ペア(2026-07-20拡張分)に低勝率ペアが混ざっていないかの確認用。"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from signal_scanner import compute_ta_score, fetch_history, PAIR_API
from modules.rate_fetcher import fetch_live_central_bank_rates, compute_fa_score
from modules.backtest import run_backtest

LEGACY_22 = {
    "USDJPY", "EURJPY", "GBPJPY", "AUDJPY", "NZDJPY", "CADJPY", "CHFJPY",
    "SGDJPY", "HKDJPY", "CNYJPY", "MXNJPY", "TRYJPY", "ZARJPY", "INRJPY",
    "EURUSD", "GBPUSD", "AUDUSD", "NZDUSD", "USDCAD", "USDCHF", "EURGBP", "EURAUD",
}


def main():
    cb = fetch_live_central_bank_rates()
    hist = {}
    for pair in PAIR_API:
        p = fetch_history(pair, 280)
        if p and len(p) >= 60:
            hist[pair] = p

    for lookback in (180, 30):
        rows = []
        for pair, prices in hist.items():
            r = run_backtest(pair, prices, compute_ta_score, compute_fa_score,
                             cb, PAIR_API, lookback_days=lookback)
            if r.get("total", 0) > 0:
                tag = "  " if pair in LEGACY_22 else "★新"
                rows.append((r["win_rate"], pair, tag, r))
        print(f"\n【{lookback}日窓 ペア別 (勝率昇順)】 {len(rows)}ペアに取引あり")
        for wr, pair, tag, r in sorted(rows):
            print(f"  {tag}{pair:<8} 勝率{wr:>5}% PF{r['profit_factor']:<6} "
                  f"{r['total_pips']:+9.3f}pips {r['total']:>2}件 SL{r['sl_hits']}")


if __name__ == "__main__":
    main()
