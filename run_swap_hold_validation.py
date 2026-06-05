#!/usr/bin/env python3
"""
run_swap_hold_validation.py
「P5で2ロット仕込み→1ロット利確＋1ロットをトレール＋スワップで超長期保有」が
現状の1ロット運用より儲かるかを過去データで検証する。

戦略:
  P5閾値(ta68/fa60)でエントリー。主要国ペアのみ。
  スワップ＋方向（高金利通貨買い）のシグナルのみ2ロット目を超長期保有。
  ・1ロット目: TP1で利確（短中期利益を確保）
  ・2ロット目: トレール（利を伸ばす）＋スワップ加算（保有日数×日次スワップ）

比較:
  A. 現状: P5で1ロット、TP/SL固定（TP1/TP2/TP3 or SL）
  B. 新案: P5で2ロット（1利確 + 1トレール超長期+スワップ）

注意:
  ・スワップは「金利差ベースの理論値」で近似（実際の業者スワップとは異なる）
  ・トレールはバー終値ベースの簡易シミュレーション
  ・あくまで傾向を見る検証（厳密な実損益ではない）
"""

import os
from datetime import datetime, timezone

from signal_scanner import compute_ta_score, fetch_history, PAIR_API
from modules.rate_fetcher import fetch_live_central_bank_rates, compute_fa_score

# 主要国のみ（新興国・流動性低い通貨を除外）
MAJOR_CCY = {"USD", "EUR", "JPY", "GBP", "AUD", "NZD", "CAD", "CHF"}

# P5閾値
TA_LONG, FA_LONG = 68, 60
TA_SHORT, FA_SHORT = 100 - TA_LONG, 100 - FA_LONG

# 検証パラメータ
SL_MULT = 2.5
TP1_MULT = 2.5
TRAIL_MULT = 3.0       # トレール幅（ATR×3）
MAX_HOLD_BARS = 120    # 超長期保有の最大バー数（日足なら約半年）
PIP = {}  # ペアごとのpip単位


def pip_size(pair):
    return 0.01 if pair.endswith("JPY") else 0.0001


def is_major(pair):
    f, t = PAIR_API[pair]
    return f in MAJOR_CCY and t in MAJOR_CCY


def daily_swap_pips(pair, direction, cb_rates):
    """
    金利差から日次スワップを理論値で近似（pips/日）。
    direction: "LONG"=FROM買いTO売り, "SHORT"=逆
    年間金利差% を pips換算して365で割る簡易計算。
    """
    f, t = PAIR_API[pair]
    rf = cb_rates.get(f, {}).get("rate")
    rt = cb_rates.get(t, {}).get("rate")
    if rf is None or rt is None:
        return 0.0
    diff = (rf - rt) if direction == "LONG" else (rt - rf)
    # 金利差1% ≒ 価格の1%/年。pips換算して日割り
    # 近似: 1ロット(10万通貨)で 金利差% × 価格 / 365 を pips化
    # ここでは「価格に対する%」をpips比率で近似するため、価格基準で算出
    return diff  # 年間%。実際の日次pipsは後でレートを掛けて計算


def simulate(pairs_hist, cb_rates, use_two_lot):
    """
    バックテスト本体。use_two_lot=Falseなら現状1ロット、Trueなら2ロット戦略。
    返り値: dict(total, total_pips, swap_pips, win, loss, ...)
    """
    total = 0
    total_pips = 0.0
    swap_pips_sum = 0.0
    wins = 0
    losses = 0
    long_term_held = 0
    lookback = int(os.environ.get("BACKTEST_DAYS", "180"))

    for pair, prices in pairs_hist.items():
        if not is_major(pair):
            continue
        ps = pip_size(pair)
        n = len(prices)
        start = max(60, n - lookback)
        i = start
        while i < n - 1:
            hist = prices[:i + 1]
            cur = prices[i]
            ta = compute_ta_score(cur, hist)
            fa = compute_fa_score(pair, PAIR_API, cb_rates)
            ta_score = ta["ta_score"]
            fa_score = fa["score"]
            ta_sign = 1 if ta_score > 50 else (-1 if ta_score < 50 else 0)
            fa_sign = 1 if fa["direction"] == "buy" else (-1 if fa["direction"] == "sell" else 0)
            agree = ta_sign == fa_sign and ta_sign != 0

            entry_dir = None
            if agree and ta_score >= TA_LONG and fa_score >= FA_LONG:
                entry_dir = "LONG" if fa_sign > 0 else "SHORT"
            elif agree and ta_score <= TA_SHORT and fa_score <= FA_SHORT:
                entry_dir = "SHORT"

            if not entry_dir:
                i += 1
                continue

            atr = ta.get("atr") or (cur * 0.005)
            entry = cur
            total += 1

            # スワップ方向判定（年間金利差%）
            swap_dir_pct = daily_swap_pips(pair, entry_dir, cb_rates)
            swap_positive = swap_dir_pct > 0

            if entry_dir == "LONG":
                sl = entry - atr * SL_MULT
                tp1 = entry + atr * TP1_MULT
            else:
                sl = entry + atr * SL_MULT
                tp1 = entry - atr * TP1_MULT

            # --- 1ロット目（または現状の1ロット）: TP1/SL固定 ---
            lot1_pips = 0.0
            exit_idx = None
            for j in range(i + 1, min(n, i + 1 + MAX_HOLD_BARS)):
                p = prices[j]
                if entry_dir == "LONG":
                    if p <= sl:
                        lot1_pips = (sl - entry) / ps; exit_idx = j; break
                    if p >= tp1:
                        lot1_pips = (tp1 - entry) / ps; exit_idx = j; break
                else:
                    if p >= sl:
                        lot1_pips = (sl - entry) * -1 / ps; exit_idx = j; break
                    if p <= tp1:
                        lot1_pips = (entry - tp1) / ps; exit_idx = j; break
            if exit_idx is None:
                # 期間内に決済されず: 最終価格で評価
                p = prices[min(n - 1, i + MAX_HOLD_BARS)]
                lot1_pips = ((p - entry) if entry_dir == "LONG" else (entry - p)) / ps
                exit_idx = min(n - 1, i + MAX_HOLD_BARS)

            total_pips += lot1_pips
            if lot1_pips > 0: wins += 1
            else: losses += 1

            # --- 2ロット目（2ロット戦略 かつ スワップ+方向のみ）---
            if use_two_lot and swap_positive:
                long_term_held += 1
                # トレール超長期保有
                trail = atr * TRAIL_MULT
                if entry_dir == "LONG":
                    trail_stop = entry - trail
                    best = entry
                else:
                    trail_stop = entry + trail
                    best = entry
                lot2_exit = None
                hold_bars = 0
                for j in range(i + 1, min(n, i + 1 + MAX_HOLD_BARS)):
                    p = prices[j]
                    hold_bars = j - i
                    if entry_dir == "LONG":
                        if p > best:
                            best = p
                            trail_stop = max(trail_stop, best - trail)
                        if p <= trail_stop:
                            lot2_exit = (trail_stop - entry) / ps; break
                    else:
                        if p < best:
                            best = p
                            trail_stop = min(trail_stop, best + trail)
                        if p >= trail_stop:
                            lot2_exit = (entry - trail_stop) / ps; break
                if lot2_exit is None:
                    p = prices[min(n - 1, i + MAX_HOLD_BARS)]
                    lot2_exit = ((p - entry) if entry_dir == "LONG" else (entry - p)) / ps
                    hold_bars = min(n - 1, i + MAX_HOLD_BARS) - i

                # スワップ加算（保有日数 × 日次スワップpips）
                # 年間金利差% を 価格基準でpips換算: entry * diff% / 100 / ps / 365 * 日数
                daily_swap_p = (entry * abs(swap_dir_pct) / 100.0) / ps / 365.0
                swap_gain = daily_swap_p * hold_bars
                swap_pips_sum += swap_gain
                total_pips += lot2_exit + swap_gain

            i = exit_idx + 1  # 次のエントリーは決済後

    return {
        "total": total,
        "total_pips": round(total_pips, 1),
        "swap_pips": round(swap_pips_sum, 1),
        "wins": wins,
        "losses": losses,
        "long_term_held": long_term_held,
        "win_rate": round(wins / total * 100, 1) if total else 0,
    }


def main():
    print("=" * 64)
    print("2ロット超長期スワップ戦略の検証（主要国のみ・P5閾値）")
    print("=" * 64)

    cb_rates = fetch_live_central_bank_rates()
    print(f"[INFO] {len(PAIR_API)}ペアの履歴取得中...")
    pairs_hist = {}
    for pair in PAIR_API:
        if not is_major(pair):
            continue
        prices = fetch_history(pair, 280)
        if prices and len(prices) >= 60:
            pairs_hist[pair] = prices
    print(f"[INFO] 主要国{len(pairs_hist)}ペア取得完了")
    print(f"[INFO] 対象ペア: {list(pairs_hist.keys())}\n")

    # スワップ方向の確認
    print("=== 各ペアのスワップ方向（年間金利差%）===")
    for pair in pairs_hist:
        for d in ["LONG", "SHORT"]:
            pct = daily_swap_pips(pair, d, cb_rates)
            if pct > 0:
                print(f"  {pair} {d}: スワップ+ ({pct:+.2f}%/年)")
    print()

    a = simulate(pairs_hist, cb_rates, use_two_lot=False)
    b = simulate(pairs_hist, cb_rates, use_two_lot=True)

    print("=" * 64)
    print("【結果比較】")
    print("=" * 64)
    print(f"\nA. 現状(1ロット固定):")
    print(f"   {a['total']}件 勝率{a['win_rate']}% 合計{a['total_pips']}pips")
    print(f"\nB. 2ロット戦略(1利確+1トレール超長期+スワップ):")
    print(f"   {b['total']}件 勝率{b['win_rate']}% 合計{b['total_pips']}pips")
    print(f"   うち超長期保有: {b['long_term_held']}件")
    print(f"   スワップ利益: {b['swap_pips']}pips")

    diff = b['total_pips'] - a['total_pips']
    print(f"\n→ 差: {diff:+.1f}pips")
    if diff > 10:
        print("  ✅ 2ロット戦略で改善。超長期保有に価値あり")
    elif diff < -10:
        print("  ❌ 2ロット戦略で悪化。トレール往復負けの可能性")
    else:
        print("  ➖ ほぼ変化なし。スワップ分だけ僅かに有利かも")
    print("\n  ※スワップは金利差の理論値。実際の業者スワップとは異なります。")
    print("  ※トレールはバー終値ベースの簡易シミュレーションです。")


if __name__ == "__main__":
    main()
