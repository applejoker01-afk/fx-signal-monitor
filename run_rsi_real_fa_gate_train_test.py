#!/usr/bin/env python3
"""
run_rsi_real_fa_gate_train_test.py
前回の指摘（「TA単独で試していて本番のFA/TAゲートではなかった」）を受けて、
FREDの実際の過去金利データ（USD: DFF、JPY: IR3TIB01JPM156N、いずれも
キー不要のfredgraph.csv経由で取得済み・data/fred_cache/）を使い、
20年分の**史実に基づいたFA（金利差）ゲート**を初めて再現する。

その上で、本番と同じTA+FA一致ゲート（TA>=60, FA>=55）に、RSI反発条件を
追加した場合の効果を、正しく訓練/テスト分離して検証する。

FAスコアの簡略化について:
  本番のcompute_fa_score()は金利差本体・利上下げスタンス・米国債補正・
  金利サイクルモメンタムの4要素で構成されるが、後三者は「その時点でのスタンス」
  という定性判断が必要で、任意の過去日付について自動再現するのは困難。
  本スクリプトでは金利差本体のみ（diff_score、最大±30点）を使う簡略版
  FAスコアを用いる。これは本番より弱いシグナルだが、金利差という核心部分は
  史実通りに再現できている。
"""

import csv
from datetime import datetime, date, timedelta

from signal_scanner import compute_ta_score, atr_calc, rsi as rsi_fn

PRICE_FILE = "data/usdjpy_daily.csv"
USD_RATE_FILE = "data/fred_cache/usd_dff.csv"
JPY_RATE_FILE = "data/fred_cache/jpy_ir3tib.csv"
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
    """日次FREDシリーズを {date: value} で返す（欠損値'.'はスキップ）"""
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


def build_daily_rate_series(raw, all_dates):
    """月次/欠損ありのシリーズを、価格データの日付列に前方補完(forward-fill)する"""
    sorted_known = sorted(raw.items())
    out = {}
    idx = 0
    last_val = None
    for d in all_dates:
        while idx < len(sorted_known) and sorted_known[idx][0] <= d:
            last_val = sorted_known[idx][1]
            idx += 1
        out[d] = last_val
    return out


def fa_score_from_rate_diff(rate_diff):
    """本番compute_fa_scoreの「金利差本体」部分のみを再現した簡易FAスコア"""
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
    print(f"[INFO] USDJPY日足 {len(dates)}件 ({dates[0]} - {dates[-1]})")

    usd_raw = load_fred_daily(USD_RATE_FILE)
    jpy_raw = load_fred_daily(JPY_RATE_FILE)
    print(f"[INFO] USD金利データ {len(usd_raw)}件, JPY金利データ {len(jpy_raw)}件（月次）")

    usd_series = build_daily_rate_series(usd_raw, dates)
    jpy_series = build_daily_rate_series(jpy_raw, dates)

    # サニティチェック: 2006年利上げ・2024年マイナス金利解除が反映されているか
    check_dates = [date(2006, 1, 1), date(2007, 6, 1), date(2024, 1, 1), date(2024, 6, 1), date(2026, 8, 1)]
    print("\n[サニティチェック] 日付 / USD金利 / JPY金利 / 差")
    for cd in check_dates:
        nearest = min(dates, key=lambda d: abs((d - cd).days))
        u, j = usd_series.get(nearest), jpy_series.get(nearest)
        if u is not None and j is not None:
            print(f"  {nearest}: USD={u:.2f}% JPY={j:.3f}% diff={u-j:+.2f}")

    start_idx = 200
    candidates = []
    last_entry_idx = -999
    skipped_no_rate = 0
    for i in range(start_idx, len(closes) - 1):
        if i - last_entry_idx < 10:
            continue
        d = dates[i]
        u, j = usd_series.get(d), jpy_series.get(d)
        if u is None or j is None:
            skipped_no_rate += 1
            continue

        hist = closes[:i + 1]
        ta = compute_ta_score(closes[i], hist)
        rate_diff = u - j
        fa_score, fa_dir = fa_score_from_rate_diff(rate_diff)

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

    print(f"\n[INFO] 金利データ欠損でスキップ: {skipped_no_rate}件")
    print(f"[INFO] 本物のTA+FA一致ゲートを通過した候補: {len(candidates)}件\n")

    train = [c for c in candidates if c["is_train"]]
    test = [c for c in candidates if not c["is_train"]]

    print("=" * 70)
    print("本物のFAゲート採用後の結果")
    print("=" * 70)
    print(f"訓練期間(2004-2019) 全体ベースライン: {stats([c['trade'] for c in train])}")
    print(f"訓練期間 RSI追加後: {stats([c['trade'] for c in train if c['rsi_ok']])}")
    print(f"訓練期間 RSI条件で弾かれた分: {stats([c['trade'] for c in train if not c['rsi_ok']])}")
    print()
    print(f"テスト期間(2020-2026) 全体ベースライン: {stats([c['trade'] for c in test])}")
    print(f"テスト期間 RSI追加後（訓練で選んだ基準(40,10)をそのまま適用）: "
          f"{stats([c['trade'] for c in test if c['rsi_ok']])}")
    print(f"テスト期間 RSI条件で弾かれた分: {stats([c['trade'] for c in test if not c['rsi_ok']])}")


if __name__ == "__main__":
    main()
