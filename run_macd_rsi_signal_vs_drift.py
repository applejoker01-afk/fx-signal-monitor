#!/usr/bin/env python3
"""
run_macd_rsi_signal_vs_drift.py
「MACDゴールデンクロス+RSI反発」でロングした場合のフォワードリターンが、
対JPY通貨の構造的な上昇ドリフト（単純に買い持ちしているだけの平均リターン）
を上回るかどうかを切り分ける検証。

2026-08-26、前段の「デッドクロス+RSI反発でショートすると負ける」検証結果に
対し、ユーザーから「むしろ逆（ロング）に効いているのでは」という指摘があり、
それを検証したところ対JPY14ペア中13ペアが単純買い持ちでも+180日プラス
だったことが判明（地合いの影響が濃厚）。本スクリプトはその切り分けを厳密化する:

  signal群: その日にMACDゴールデンクロス(直近3営業日以内)かつRSI反発
            (直近10営業日以内にRSI<=40→直近RSIが3日前より上昇)が
            同時に成立した日
  baseline群: それ以外も含む全営業日（=「特に理由なくいつ買っても」の代理）

同一ペア・同一期間内で両群のNフォワードリターンを比較する。signal群が
baseline群を明確に上回れば「シグナル自体に情報価値がある」と言え、
差がなければ「ただの地合い（ドリフト）を掴んでいるだけ」と判断できる。
"""

import sys
import statistics as st
from signal_scanner import fetch_history_with_dates, PAIR_API, rsi as rsi_fn, macd_now

JPY_PAIRS = [p for p in PAIR_API if p.endswith("JPY")]
HORIZONS = [5, 10, 20]
MIN_HIST = 40  # MACD計算に必要な最低本数


def macd_golden_cross_recent(hist, lookback=3):
    if len(hist) < MIN_HIST:
        return False
    states = []
    for back in range(lookback, -1, -1):
        sub = hist[:len(hist) - back] if back > 0 else hist
        m, s = macd_now(sub)
        if m is None or s is None:
            return False
        states.append(m > s)
    return (not states[0]) and states[-1] and any(states)


def rsi_oversold_reversal(hist, dip_threshold=40, lookback=10):
    if len(hist) < 30:
        return False
    r_now = rsi_fn(hist, 14)
    r_prev3 = rsi_fn(hist[:-3], 14) if len(hist) > 3 else None
    if r_now is None or r_prev3 is None:
        return False
    if r_now <= r_prev3:
        return False
    min_r = 100
    for back in range(0, lookback):
        sub = hist[:len(hist) - back] if back > 0 else hist
        r = rsi_fn(sub, 14)
        if r is not None:
            min_r = min(min_r, r)
    return min_r <= dip_threshold


def analyze_pair(pair, dates, closes):
    n = len(closes)
    signal_days, all_days = [], []
    start = max(MIN_HIST, 30)
    max_h = max(HORIZONS)
    for i in range(start, n - max_h):  # フォワード検証に十分な余白がある日のみ対象
        hist = closes[:i + 1]
        fwd = {h: (closes[i + h] - closes[i]) / closes[i] * 100 for h in HORIZONS}
        all_days.append(fwd)
        is_signal = macd_golden_cross_recent(hist) and rsi_oversold_reversal(hist)
        if is_signal:
            signal_days.append({"date": dates[i], **fwd})
    return signal_days, all_days


def summarize(label, records, horizons=HORIZONS):
    if not records:
        print(f"    {label}: n=0")
        return {}
    out = {}
    for h in horizons:
        rets = [r[h] for r in records if h in r]
        if not rets:
            continue
        wins = sum(1 for r in rets if r > 0)
        mean = st.mean(rets)
        out[h] = mean
        print(f"    {label} {h}日後: n={len(rets)}  平均{mean:+.3f}%  "
              f"中央値{st.median(rets):+.3f}%  勝率{wins/len(rets)*100:.0f}%")
    return out


def main():
    all_signal, all_baseline = [], []
    print(f"[INFO] 対象ペア: {JPY_PAIRS}\n")
    per_pair_summary = []

    for pair in JPY_PAIRS:
        res = fetch_history_with_dates(pair, days=280)
        if not res:
            print(f"{pair}: 取得失敗")
            continue
        dates, closes = res
        if len(closes) < 100:
            print(f"{pair}: データ不足({len(closes)}日)")
            continue
        sig, base = analyze_pair(pair, dates, closes)
        print(f"=== {pair} (全{len(closes)}日, シグナル発生{len(sig)}日 / 全営業日{len(base)}日) ===")
        sig_means = summarize("シグナル", sig)
        base_means = summarize("全営業日(参考=ドリフト)", base)
        if sig and 10 in sig_means and 10 in base_means:
            excess = sig_means[10] - base_means[10]
            print(f"    → 10日後の超過リターン(シグナル-ドリフト): {excess:+.3f}%")
            per_pair_summary.append((pair, len(sig), sig_means.get(10), base_means.get(10), excess))
        print()
        all_signal.extend(sig)
        all_baseline.extend(base)

    print("=" * 70)
    print(f"全ペア合算: シグナル{len(all_signal)}日 / 全営業日{len(all_baseline)}日")
    print("=" * 70)
    print("--- シグナル群 ---")
    sig_means = summarize("シグナル", all_signal)
    print("--- 全営業日(ドリフトの代理) ---")
    base_means = summarize("ドリフト", all_baseline)

    print("\n--- 超過リターン（シグナル − ドリフト） ---")
    for h in HORIZONS:
        if h in sig_means and h in base_means:
            print(f"  {h}日後: {sig_means[h] - base_means[h]:+.3f}%")

    print("\n--- ペア別 10日後 超過リターン ---")
    for pair, n, sm, bm, ex in sorted(per_pair_summary, key=lambda x: -x[4]):
        print(f"  {pair}: n={n}  シグナル{sm:+.2f}% vs ドリフト{bm:+.2f}%  超過{ex:+.2f}%")


if __name__ == "__main__":
    main()
