#!/usr/bin/env python3
"""
run_rsi_full_fa_score_validation.py
[[2026-08-11-rsi-reversal-filter]] の簡易FAスコア（金利差本体のみ）を、
本番compute_fa_score()によりに近い完全版（stance補正＋米国債補正込み）に
拡張して再検証する。

stance（利上げ/利下げ姿勢）の再現方法:
  本番は手動JSON管理の定性判断だが、過去の任意日付には使えない。
  代わりに「180日前と比べて政策金利がどう動いたか」から機械的に判定する:
    tighten: 180日前より+0.1%以上上昇
    ease:    180日前より-0.1%以上下降
    neutral: それ以外
  これは完璧な再現ではないが、本番のstance判定の意図（トレンド方向）を
  定量的に近似したもの。

米国債補正: FRED DGS10（米10年債利回り、日次）の180日トレンドで
  bond_trend up/down を判定し、USDJPYはfrom_ccy=USDなので該当ロジックを適用。
"""

from datetime import datetime, date, timedelta

from signal_scanner import compute_ta_score, atr_calc, rsi as rsi_fn

PRICE_FILE = "data/usdjpy_daily.csv"
USD_RATE_FILE = "data/fred_cache/usd_dff.csv"
JPY_RATE_FILE = "data/fred_cache/jpy_ir3tib.csv"
US10Y_FILE = "data/fred_cache/us10y_dgs10.csv"
ATR_MULT = (3.0, 3.0, 4.5, 6.0)
TA_THRESHOLD = 60
FA_THRESHOLD = 55
SPLIT_DATE = date(2020, 1, 1)


def load_usdjpy_daily():
    dates, closes = [], []
    with open(PRICE_FILE, "r", encoding="utf-8") as f:
        next(f)
        for line in f:
            d, p = line.strip().split(",")
            dates.append(datetime.strptime(d, "%Y-%m-%d").date())
            closes.append(float(p))
    return dates, closes


def load_fred_daily(path):
    out = {}
    with open(path, "r", encoding="utf-8") as f:
        next(f)
        for line in f:
            parts = line.strip().split(",")
            if len(parts) != 2:
                continue
            d, v = parts
            if v == "." or not v:
                continue
            try:
                out[datetime.strptime(d, "%Y-%m-%d").date()] = float(v)
            except ValueError:
                continue
    return out


def build_daily_series(raw, all_dates):
    sorted_known = sorted(raw.items())
    out, idx, last_val = {}, 0, None
    for d in all_dates:
        while idx < len(sorted_known) and sorted_known[idx][0] <= d:
            last_val = sorted_known[idx][1]
            idx += 1
        out[d] = last_val
    return out


def stance_at(series_sorted_items, d, lookback_days=180):
    """date d時点でのstanceを、lookback_days前との比較で機械的に判定"""
    target_prev = d - timedelta(days=lookback_days)
    cur_val = None
    prev_val = None
    for dt, v in series_sorted_items:
        if dt <= d:
            cur_val = v
        if dt <= target_prev:
            prev_val = v
        if dt > d:
            break
    if cur_val is None or prev_val is None:
        return "neutral"
    diff = cur_val - prev_val
    if diff >= 0.1:
        return "tighten"
    elif diff <= -0.1:
        return "ease"
    return "neutral"


def bond_trend_at(series_sorted_items, d, lookback_days=180):
    target_prev = d - timedelta(days=lookback_days)
    cur_val = prev_val = None
    for dt, v in series_sorted_items:
        if dt <= d:
            cur_val = v
        if dt <= target_prev:
            prev_val = v
        if dt > d:
            break
    if cur_val is None or prev_val is None:
        return None
    diff = cur_val - prev_val
    if diff >= 0.15:
        return "up"
    elif diff <= -0.15:
        return "down"
    return None


def full_fa_score(rate_diff, stance_from, stance_to, bond_trend, from_is_usd):
    diff_magnitude = min(abs(rate_diff) * 7.5, 30)
    diff_score = diff_magnitude if rate_diff > 0 else -diff_magnitude

    stance_bonus = 0
    if rate_diff > 0:
        if stance_from == "tighten" and stance_to == "ease":
            stance_bonus = 15
        elif stance_from == "ease":
            stance_bonus = -10
        elif stance_from == "tighten":
            stance_bonus = 5
    elif rate_diff < 0:
        if stance_from == "ease" and stance_to == "tighten":
            stance_bonus = -15
        elif stance_from == "tighten":
            stance_bonus = 10

    bond_bonus = 0
    if bond_trend in ("up", "down") and from_is_usd:
        bond_bonus = 5 if bond_trend == "up" else -5

    final = max(0, min(100, 50 + diff_score + stance_bonus + bond_bonus))
    if final >= 60:
        direction = "buy"
    elif final <= 40:
        direction = "sell"
    else:
        direction = "neutral"
    return final, direction


def rsi_oversold_reversal(hist, dip_threshold=40, lookback=10):
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
    dates, closes = load_usdjpy_daily()
    print(f"[INFO] USDJPY日足 {len(dates)}件")

    usd_raw = load_fred_daily(USD_RATE_FILE)
    jpy_raw = load_fred_daily(JPY_RATE_FILE)
    us10y_raw = load_fred_daily(US10Y_FILE)
    usd_sorted = sorted(usd_raw.items())
    jpy_sorted = sorted(jpy_raw.items())
    us10y_sorted = sorted(us10y_raw.items())

    usd_series = build_daily_series(usd_raw, dates)
    jpy_series = build_daily_series(jpy_raw, dates)

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
        rate_diff = u - j
        stance_usd = stance_at(usd_sorted, d)
        stance_jpy = stance_at(jpy_sorted, d)
        bond_trend = bond_trend_at(us10y_sorted, d)
        fa_score, fa_dir = full_fa_score(rate_diff, stance_usd, stance_jpy, bond_trend, from_is_usd=True)

        ta_sign = 1 if ta["ta_score"] > 50 else (-1 if ta["ta_score"] < 50 else 0)
        fa_sign = 1 if fa_dir == "buy" else (-1 if fa_dir == "sell" else 0)
        agree = ta_sign == fa_sign and ta_sign != 0

        if not (agree and ta["ta_score"] >= TA_THRESHOLD and fa_score >= FA_THRESHOLD and fa_sign > 0):
            continue

        last_entry_idx = i
        atr = atr_calc(hist, 14) or (closes[i] * 0.005)
        trade = simulate_long(closes, i, atr)
        trade["date"] = d
        trade["is_train"] = d < SPLIT_DATE
        rsi_ok = rsi_oversold_reversal(hist)
        candidates.append({"trade": trade, "rsi_ok": rsi_ok, "is_train": trade["is_train"]})

    print(f"[INFO] 完全版FA+TAゲート通過候補: {len(candidates)}件\n")

    train = [c for c in candidates if c["is_train"]]
    test = [c for c in candidates if not c["is_train"]]

    print("=" * 70)
    print("完全版FAスコア（stance+米国債補正込み）での結果")
    print("=" * 70)
    print(f"訓練期間 ベースライン: {stats([c['trade'] for c in train])}")
    print(f"訓練期間 RSI追加後: {stats([c['trade'] for c in train if c['rsi_ok']])}")
    print(f"訓練期間 弾かれた分: {stats([c['trade'] for c in train if not c['rsi_ok']])}")
    print()
    print(f"テスト期間 ベースライン: {stats([c['trade'] for c in test])}")
    print(f"テスト期間 RSI追加後: {stats([c['trade'] for c in test if c['rsi_ok']])}")
    print(f"テスト期間 弾かれた分: {stats([c['trade'] for c in test if not c['rsi_ok']])}")


if __name__ == "__main__":
    main()
