#!/usr/bin/env python3
"""
run_rsi_gbpjpy_param_search.py
[[2026-08-11-rsi-reversal-filter]]のペア別内訳で、GBPJPYは(dip=40,lb=10)を
適用すると好調な9件が全部弾かれて0件になった。「反発のタイミングが違う
だけで、GBPJPY用に別のdip/lookbackがあるのでは」という仮説を検証する。

USDJPYのような20年日足CSVがGBPJPYには無いため、Frankfurter APIで取得できる
直近280日で、本番と同じ現在レートのFA+TAゲートを使い、dip/lookbackの
グリッドサーチを行う。サンプルはUSDJPYの20年検証より遥かに少ないため、
参考程度の結果になる点に注意。
"""

import os
from datetime import datetime, timedelta, timezone

from signal_scanner import compute_ta_score, compute_fa_score, PAIR_API, atr_calc, rsi as rsi_fn
from modules.rate_fetcher import fetch_live_central_bank_rates

PAIR = "GBPJPY"
ATR_MULT = (3.0, 3.0, 4.5, 6.0)
TA_THRESHOLD = 60
FA_THRESHOLD = 55
DIP_THRESHOLDS = [30, 35, 40, 45, 50, 55]
LOOKBACKS = [5, 10, 15, 20, 25]


def fetch_history_with_dates(pair, days=280):
    import urllib.request, json as _json
    frm, to = PAIR_API[pair]
    end = datetime.now(timezone.utc).date()
    start = end - timedelta(days=days)
    url = f"https://api.frankfurter.dev/v1/{start}..{end}?base={frm}&symbols={to}"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    data = _json.loads(urllib.request.urlopen(req, timeout=15).read())
    series = sorted((d, v[to]) for d, v in data["rates"].items() if to in v)
    dates = [datetime.strptime(d, "%Y-%m-%d").date() for d, _ in series]
    closes = [v for _, v in series]
    return dates, closes


def rsi_reversal(hist, dip_threshold, lookback):
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


def stats(trades):
    if not trades:
        return {"total": 0, "win_rate": 0, "pf": 0, "total_pips": 0}
    total = len(trades)
    wins = sum(1 for t in trades if t["result"] == "WIN")
    gp = sum(t["pips"] for t in trades if t["pips"] > 0)
    gl = abs(sum(t["pips"] for t in trades if t["pips"] < 0))
    pf = round(gp / gl, 2) if gl else 999
    return {"total": total, "win_rate": round(wins / total * 100, 1), "pf": pf,
            "total_pips": round(sum(t["pips"] for t in trades), 2)}


def main():
    lookback_days = int(os.environ.get("BACKTEST_DAYS", "180"))
    cb_rates = fetch_live_central_bank_rates()
    dates, closes = fetch_history_with_dates(PAIR, 280)
    print(f"[INFO] {PAIR} 日足 {len(dates)}件 ({dates[0]} - {dates[-1]})\n")

    n = len(closes)
    start_idx = max(90, n - lookback_days)

    baseline_trades = []
    baseline_entries = []  # (idx, rsi_now, rsi_min10)
    candidates_by_idx = {}

    last_entry_idx = -999
    for i in range(start_idx, n):
        if i - last_entry_idx < 10:
            continue
        hist = closes[:i + 1]
        price = closes[i]
        ta = compute_ta_score(price, hist)
        fa = compute_fa_score(PAIR, PAIR_API, cb_rates)
        ta_sign = 1 if ta["ta_score"] > 50 else (-1 if ta["ta_score"] < 50 else 0)
        fa_sign = 1 if fa["direction"] == "buy" else (-1 if fa["direction"] == "sell" else 0)
        agree = ta_sign == fa_sign and ta_sign != 0
        if not (agree and ta["ta_score"] >= TA_THRESHOLD and fa["score"] >= FA_THRESHOLD and fa_sign > 0):
            continue
        last_entry_idx = i
        atr = ta.get("atr") or (price * 0.005)
        trade = simulate_long(closes, i, atr)
        r_now = rsi_fn(hist, 14)
        min_r_by_lb = {}
        for lb in LOOKBACKS:
            vals = []
            for back in range(0, lb):
                sub = hist[:len(hist) - back] if back > 0 else hist
                r = rsi_fn(sub, 14)
                vals.append(r if r is not None else 100)
            min_r_by_lb[lb] = min(vals)
        r_prev3 = rsi_fn(hist[:-3], 14) if len(hist) > 3 else None
        rising = (r_now is not None and r_prev3 is not None and r_now > r_prev3)

        baseline_trades.append(trade)
        baseline_entries.append({"idx": i, "date": dates[i], "rsi_now": r_now,
                                  "min_r_by_lb": min_r_by_lb, "rising": rising, "trade": trade})

    print(f"[INFO] ベースライン候補: {len(baseline_trades)}件")
    print(f"[INFO] ベースライン成績: {stats(baseline_trades)}\n")

    print("--- 各ベースライントレードのエントリー時RSI状況 ---")
    for e in baseline_entries:
        print(f"  {e['date']} {e['trade']['result']:4s} pips={e['trade']['pips']:+.3f} "
              f"RSI_now={e['rsi_now']:.1f} rising={e['rising']} "
              f"min10日={e['min_r_by_lb'][10]:.1f} min20日={e['min_r_by_lb'][20]:.1f}")

    print()
    header_label = "dip/lb"
    print(f"{header_label:>8}", end="")
    for lb in LOOKBACKS:
        print(f"{'lb='+str(lb):>20}", end="")
    print()
    for dip in DIP_THRESHOLDS:
        print(f"{dip:>8}", end="")
        for lb in LOOKBACKS:
            trades = [e["trade"] for e in baseline_entries
                      if e["rising"] and e["min_r_by_lb"][lb] <= dip]
            s = stats(trades)
            cell_label = f"n={s['total']} wr{s['win_rate']} PF{s['pf']}"
            print(f"{cell_label:>20}", end="")
        print()


if __name__ == "__main__":
    main()
