#!/usr/bin/env python3
"""
research/scripts/exp001_data_availability_probe.py
2026-08-24

Diagnostic only (not a performance claim). Checks whether the repo's existing
data holdings can support a cost-inclusive rolling out-of-sample (OOS) test of
EXP-001 (RSI oversold reversal + MACD crossover confirmation) from
research/EXPERIMENT_REGISTRY.md.

It reuses read-only helpers/data already present in the repo:
  - signal_scanner.compute_ta_score / rsi / macd_now (imported, not modified)
  - data/usdjpy_daily.csv (the only >1-year price history cached in this repo)
  - data/fred_cache/usd_dff.csv, data/fred_cache/jpy_ir3tib.csv (the only
    pairs with a cached historical rate-differential series for a real,
    point-in-time FA gate)

It does not touch cost adjustment, IS/OOS folding, or Reality Check — those
are irrelevant until the base sample-size question is answered. See
research/results/2026-08-24-EXP-001.md for the conclusion drawn from this
probe's output.
"""

import os
import sys
from datetime import datetime, date

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from signal_scanner import compute_ta_score, rsi as rsi_fn, macd_now

PRICE_FILE = "data/usdjpy_daily.csv"
USD_RATE_FILE = "data/fred_cache/usd_dff.csv"
JPY_RATE_FILE = "data/fred_cache/jpy_ir3tib.csv"
SPLIT_DATE = date(2020, 1, 1)  # matches the pre-existing 2004-2019 / 2020-2026 split
TA_LONG, FA_LONG = 60, 55
TA_SHORT, FA_SHORT = 40, 45


def load_usdjpy_daily():
    dates, closes = [], []
    with open(PRICE_FILE) as f:
        next(f)
        for line in f:
            d, p = line.strip().split(",")
            dates.append(datetime.strptime(d, "%Y-%m-%d").date())
            closes.append(float(p))
    return dates, closes


def load_fred_daily(path):
    out = {}
    with open(path) as f:
        next(f)
        for line in f:
            parts = line.strip().split(",")
            if len(parts) != 2:
                continue
            d, v = parts
            if v in (".", ""):
                continue
            try:
                out[datetime.strptime(d, "%Y-%m-%d").date()] = float(v)
            except ValueError:
                continue
    return out


def build_daily_rate_series(raw, all_dates):
    sorted_known = sorted(raw.items())
    out, idx, last_val = {}, 0, None
    for d in all_dates:
        while idx < len(sorted_known) and sorted_known[idx][0] <= d:
            last_val = sorted_known[idx][1]
            idx += 1
        out[d] = last_val
    return out


def fa_score_from_rate_diff(rate_diff):
    diff_magnitude = min(abs(rate_diff) * 7.5, 30)
    diff_score = diff_magnitude if rate_diff > 0 else -diff_magnitude
    score = max(0, min(100, 50 + diff_score))
    if score >= 60:
        direction = "buy"
    elif score <= 40:
        direction = "sell"
    else:
        direction = "neutral"
    return score, direction


def rsi_oversold_reversal(hist, dip_threshold=40, lookback=10):
    """Exact rule text: RSI recovers above dip_threshold after dipping at or
    below it within `lookback` bars. Mirrors signal_scanner.rsi_oversold_reversal_signal."""
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


def macd_cross(hist, direction):
    """True MACD-line-crosses-signal-line event (not just current state)."""
    if len(hist) < 36:
        return False
    macd_now_v, sig_now = macd_now(hist)
    macd_prev_v, sig_prev = macd_now(hist[:-1])
    if None in (macd_now_v, sig_now, macd_prev_v, sig_prev):
        return False
    if direction == "LONG":
        return macd_prev_v <= sig_prev and macd_now_v > sig_now
    return macd_prev_v >= sig_prev and macd_now_v < sig_now


def main():
    dates, closes = load_usdjpy_daily()
    usd_series = build_daily_rate_series(load_fred_daily(USD_RATE_FILE), dates)
    jpy_series = build_daily_rate_series(load_fred_daily(JPY_RATE_FILE), dates)
    print(f"[INFO] USDJPY daily bars: {len(dates)} ({dates[0]} .. {dates[-1]})")

    start_idx = 200
    candidates = []
    last_entry_idx = -999
    for i in range(start_idx, len(closes) - 1):
        if i - last_entry_idx < 10:
            continue
        d = dates[i]
        u, j = usd_series.get(d), jpy_series.get(d)
        if u is None or j is None:
            continue
        hist = closes[:i + 1]
        ta = compute_ta_score(closes[i], hist)
        fa_score, fa_dir = fa_score_from_rate_diff(u - j)
        ta_sign = 1 if ta["ta_score"] > 50 else (-1 if ta["ta_score"] < 50 else 0)
        fa_sign = 1 if fa_dir == "buy" else (-1 if fa_dir == "sell" else 0)
        agree = ta_sign == fa_sign and ta_sign != 0
        long_ok = agree and ta["ta_score"] >= TA_LONG and fa_score >= FA_LONG and fa_sign > 0
        short_ok = agree and ta["ta_score"] <= TA_SHORT and fa_score <= FA_SHORT and fa_sign < 0
        if not (long_ok or short_ok):
            continue
        last_entry_idx = i
        direction = "LONG" if long_ok else "SHORT"
        rsi_ok = rsi_oversold_reversal(hist)
        macd_ok = macd_cross(hist, direction)
        candidates.append({
            "date": d, "direction": direction,
            "rsi_ok": rsi_ok, "macd_ok": macd_ok, "both": rsi_ok and macd_ok,
            "is_train": d < SPLIT_DATE,
        })

    both = [c for c in candidates if c["both"]]
    train_both = [c for c in both if c["is_train"]]
    test_both = [c for c in both if not c["is_train"]]

    print(f"[INFO] baseline TA+FA-agree candidates (both directions, 2004-2026): {len(candidates)}")
    print(f"[INFO] RSI-recovery-only matches: {sum(1 for c in candidates if c['rsi_ok'])}")
    print(f"[INFO] MACD-cross-only matches:   {sum(1 for c in candidates if c['macd_ok'])}")
    print(f"[INFO] RSI+MACD (EXP-001 rule) matches: {len(both)}"
          f"  train(2004-2019)={len(train_both)}  OOS(2020-2026)={len(test_both)}")
    for c in both:
        print(f"    {c['date']}  {c['direction']}  train={c['is_train']}")


if __name__ == "__main__":
    main()
