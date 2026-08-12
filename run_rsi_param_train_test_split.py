#!/usr/bin/env python3
"""
run_rsi_param_train_test_split.py
run_rsi_param_sensitivity.py のグリッドサーチには見落としがあった。
20通りのパラメータ候補を同じ20年分のデータに当てはめて「良さそうな
組み合わせ」を選ぶ行為自体が、軽度のデータスヌーピング（多重比較による
後知恵バイアス）になる。細かいグリッドを同じ全期間に当て直しても
この問題は解決しない。

正しい詰め方: 訓練期間でパラメータの当たりをつけ、完全に触っていない
テスト期間で初めて検証する（walk-forwardの簡易版）。

分割:
  訓練期間: 2004-12-31 〜 2019-12-31（約15年、2008年GFC含む）
  テスト期間: 2020-01-01 〜 2026-08-07（約6.5年、コロナ急落・
              グローバル利上げ・2024年8月キャリー巻き戻しを含む）
  → テスト期間は訓練期間より短いが、レジームの多様性は高い
"""

from datetime import datetime, date

from signal_scanner import compute_ta_score, atr_calc, rsi as rsi_fn

PRICE_FILE = "data/usdjpy_daily.csv"
ATR_MULT = (3.0, 3.0, 4.5, 6.0)
TA_LONG_THRESHOLD = 60
SPLIT_DATE = date(2020, 1, 1)

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

    start_idx = 200
    candidates = []
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
        trade["date"] = dates[i]
        trade["is_train"] = dates[i] < SPLIT_DATE

        r_now = rsi_fn(hist, 14)
        r_prev3 = rsi_fn(hist[:-3], 14) if len(hist) > 3 else None
        rising = (r_now is not None and r_prev3 is not None and r_now > r_prev3)

        max_lb = max(LOOKBACKS)
        rsi_recent = []
        for back in range(0, max_lb):
            sub = hist[:len(hist) - back] if back > 0 else hist
            r = rsi_fn(sub, 14)
            rsi_recent.append(r if r is not None else 100)
        min_rsi_by_lb = {lb: min(rsi_recent[:lb]) for lb in LOOKBACKS}

        candidates.append({"trade": trade, "rising": rising, "min_rsi_by_lb": min_rsi_by_lb,
                           "is_train": trade["is_train"]})

    train = [c for c in candidates if c["is_train"]]
    test = [c for c in candidates if not c["is_train"]]
    print(f"[INFO] 訓練期間候補: {len(train)}件 / テスト期間候補: {len(test)}件\n")

    print(f"訓練期間ベースライン: {stats([c['trade'] for c in train])}")
    print(f"テスト期間ベースライン: {stats([c['trade'] for c in test])}\n")

    print("=" * 90)
    print("訓練期間でのグリッドサーチ（ここでパラメータを選ぶ）")
    print("=" * 90)
    results_train = {}
    for dip in DIP_THRESHOLDS:
        for lb in LOOKBACKS:
            trades = [c["trade"] for c in train if c["rising"] and c["min_rsi_by_lb"][lb] <= dip]
            s = stats(trades)
            results_train[(dip, lb)] = s
            print(f"  dip={dip:2d} lb={lb:2d}: n={s['total']:3d} wr={s['win_rate']:5.1f}% "
                  f"PF={s['pf']:5.2f} pips={s['total_pips']:+8.2f}")

    # 訓練期間でPF最良（かつn>=20で信頼できる）パラメータを選定
    valid = {k: v for k, v in results_train.items() if v["total"] >= 20}
    best_param = max(valid.items(), key=lambda kv: kv[1]["pf"])[0] if valid else (40, 10)
    print(f"\n[SELECTED] 訓練期間で最良（n>=20条件付き）: dip={best_param[0]} lb={best_param[1]}")
    print(f"  訓練期間での成績: {results_train[best_param]}")

    print()
    print("=" * 90)
    print(f"選定パラメータ(dip={best_param[0]}, lb={best_param[1]})をテスト期間に適用（out-of-sample）")
    print("=" * 90)
    dip_sel, lb_sel = best_param
    test_trades = [c["trade"] for c in test if c["rising"] and c["min_rsi_by_lb"][lb_sel] <= dip_sel]
    print(f"  テスト期間ベースライン: {stats([c['trade'] for c in test])}")
    print(f"  テスト期間・選定パラメータ適用後: {stats(test_trades)}")

    # 参考: 元々の採用値(40,10)もテスト期間でどうなるか
    orig_trades = [c["trade"] for c in test if c["rising"] and c["min_rsi_by_lb"][10] <= 40]
    print(f"  参考: 元の採用値(dip=40,lb=10)をテスト期間に適用: {stats(orig_trades)}")


if __name__ == "__main__":
    main()
