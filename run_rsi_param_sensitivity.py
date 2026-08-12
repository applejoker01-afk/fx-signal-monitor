#!/usr/bin/env python3
"""
run_rsi_param_sensitivity.py
[[2026-08-11-rsi-reversal-filter]] で採用したRSI反発条件のパラメータ
(dip_threshold=40, lookback=10日) が、たまたま良い数字を出しただけの
過学習でないか、20年分のUSDJPY日足でグリッドサーチして確認する。

dip_threshold: {30, 35, 40, 45, 50}
lookback:      {5, 10, 15, 20}

判断基準:
  ・採用値(40,10)の周辺（隣接する値）でも同程度に改善していれば頑健
  ・採用値だけが突出して良く、周辺は改善しない/悪化するなら過学習の疑い
"""

from datetime import datetime

from signal_scanner import compute_ta_score, atr_calc, rsi as rsi_fn

PRICE_FILE = "data/usdjpy_daily.csv"
ATR_MULT = (3.0, 3.0, 4.5, 6.0)
TA_LONG_THRESHOLD = 60

DIP_THRESHOLDS = [30, 35, 40, 45, 50]
LOOKBACKS = [5, 10, 15, 20]


def load_usdjpy_daily():
    dates, closes = [], []
    with open(PRICE_FILE, "r", encoding="utf-8") as f:
        next(f)
        for line in f:
            d, p = line.strip().split(",")
            dates.append(datetime.strptime(d, "%Y-%m-%d").date())
            closes.append(float(p))
    return dates, closes


def rsi_oversold_reversal(hist, dip_threshold, lookback):
    if len(hist) < 30:
        return False
    r_now = rsi_fn(hist, 14)
    r_prev3 = rsi_fn(hist[:-3], 14) if len(hist) > 3 else None
    if r_now is None or r_prev3 is None or r_now <= r_prev3:
        return False
    min_r = 100
    for back in range(0, lookback):
        sub = hist[:len(hist) - back] if back > 0 else hist
        r = rsi_fn(sub, 14)
        if r is not None:
            min_r = min(min_r, r)
    return min_r <= dip_threshold


def simulate_long(closes, entry_idx, atr):
    sl_mult, tp1_mult, tp2_mult, tp3_mult = ATR_MULT
    entry = closes[entry_idx]
    sl = entry - atr * sl_mult
    tp1, tp2, tp3 = entry + atr * tp1_mult, entry + atr * tp2_mult, entry + atr * tp3_mult
    for j in range(entry_idx + 1, min(entry_idx + 60, len(closes))):
        p = closes[j]
        if p <= sl:
            return {"result": "LOSS", "pips": p - entry}
        if p >= tp3:
            return {"result": "WIN", "pips": tp3 - entry}
        if p >= tp2:
            return {"result": "WIN", "pips": tp2 - entry}
        if p >= tp1:
            return {"result": "WIN", "pips": tp1 - entry}
    end_idx = min(entry_idx + 59, len(closes) - 1)
    pips = closes[end_idx] - entry
    return {"result": "WIN" if pips > 0 else "LOSS", "pips": pips}


def main():
    dates, closes = load_usdjpy_daily()
    print(f"[INFO] USDJPY日足 {len(dates)}件\n")

    # 事前計算: TA単独LONG条件を満たす全エントリー候補とその時点のhist/RSIを一括計算
    start_idx = 200
    candidates = []  # (idx, hist, rsi_now, min_rsi_by_lookback dict, trade_result)
    last_entry_idx = -999
    for i in range(start_idx, len(closes) - 1):
        if i - last_entry_idx < 10:
            continue
        hist = closes[:i + 1]
        ta = compute_ta_score(closes[i], hist)
        if ta["ta_score"] < TA_LONG_THRESHOLD:
            continue
        last_entry_idx = i
        atr = atr_calc(hist, 14) or (closes[i] * 0.005)
        trade = simulate_long(closes, i, atr)
        r_now = rsi_fn(hist, 14)
        r_prev3 = rsi_fn(hist[:-3], 14) if len(hist) > 3 else None
        rising = (r_now is not None and r_prev3 is not None and r_now > r_prev3)
        # 各lookbackでの最小RSIを事前計算
        min_rsi_by_lookback = {}
        max_lb = max(LOOKBACKS)
        rsi_series_recent = []
        for back in range(0, max_lb):
            sub = hist[:len(hist) - back] if back > 0 else hist
            r = rsi_fn(sub, 14)
            rsi_series_recent.append(r if r is not None else 100)
        for lb in LOOKBACKS:
            min_rsi_by_lookback[lb] = min(rsi_series_recent[:lb])
        candidates.append({
            "trade": trade, "rising": rising, "min_rsi_by_lookback": min_rsi_by_lookback,
        })

    print(f"[INFO] TA単独LONG候補: {len(candidates)}件\n")

    baseline_total = len(candidates)
    baseline_wins = sum(1 for c in candidates if c["trade"]["result"] == "WIN")
    baseline_gp = sum(c["trade"]["pips"] for c in candidates if c["trade"]["pips"] > 0)
    baseline_gl = abs(sum(c["trade"]["pips"] for c in candidates if c["trade"]["pips"] < 0))
    baseline_pf = round(baseline_gp / baseline_gl, 2) if baseline_gl else 999
    print(f"ベースライン: 総{baseline_total}件 勝率{round(baseline_wins/baseline_total*100,1)}% PF{baseline_pf}\n")

    header_label = "dip/lb"
    print(f"{header_label:>8}", end="")
    for lb in LOOKBACKS:
        print(f"{'lb='+str(lb):>18}", end="")
    print()

    for dip in DIP_THRESHOLDS:
        print(f"{dip:>8}", end="")
        for lb in LOOKBACKS:
            trades = [c["trade"] for c in candidates
                      if c["rising"] and c["min_rsi_by_lookback"][lb] <= dip]
            if not trades:
                print(f"{'n=0':>18}", end="")
                continue
            total = len(trades)
            wins = sum(1 for t in trades if t["result"] == "WIN")
            gp = sum(t["pips"] for t in trades if t["pips"] > 0)
            gl = abs(sum(t["pips"] for t in trades if t["pips"] < 0))
            pf = round(gp / gl, 2) if gl else 999
            wr = round(wins / total * 100, 1)
            print(f"{f'n={total} wr{wr} PF{pf}':>18}", end="")
        print()


if __name__ == "__main__":
    main()
