#!/usr/bin/env python3
"""
run_rsi_filter_longhistory.py
RSIオーバーソールド反発フィルターの頑健性を、20年分のUSDJPY日足
(data/usdjpy_daily.csv, 2004年〜) で検証する。

[[2026-08-11-rsi-reversal-filter]] で直近180日・27ペアの検証では
勝率59%→74.2%・PF1.57→3.48という有望な結果が出たが、サンプルが
31件と小さい。COT検証（run_cot_unwind_longhistory.py）と同じ手法で
サンプルを増やし、フルークかどうかを判定する。

金利差FAゲートは過去に遡及できないため、run_cot_long_suppression.py と
同じくTA単独条件（ta_score>=60）をベースラインとし、そこにRSI反発条件を
追加した場合の変化を見る。
"""

from datetime import datetime

from signal_scanner import compute_ta_score, atr_calc, rsi as rsi_fn

PRICE_FILE = "data/usdjpy_daily.csv"
ATR_MULT = (3.0, 3.0, 4.5, 6.0)
TA_LONG_THRESHOLD = 60


def load_usdjpy_daily():
    dates, closes = [], []
    with open(PRICE_FILE, "r", encoding="utf-8") as f:
        next(f)
        for line in f:
            d, p = line.strip().split(",")
            dates.append(datetime.strptime(d, "%Y-%m-%d").date())
            closes.append(float(p))
    return dates, closes


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
            return {"result": "LOSS", "exit_reason": "SL_HIT", "pips": p - entry}
        if p >= tp3:
            return {"result": "WIN", "exit_reason": "TP3_HIT", "pips": tp3 - entry}
        if p >= tp2:
            return {"result": "WIN", "exit_reason": "TP2_HIT", "pips": tp2 - entry}
        if p >= tp1:
            return {"result": "WIN", "exit_reason": "TP1_HIT", "pips": tp1 - entry}
    end_idx = min(entry_idx + 59, len(closes) - 1)
    pips = closes[end_idx] - entry
    return {"result": "WIN" if pips > 0 else "LOSS", "exit_reason": "TIME_EXIT", "pips": pips}


def summarize(trades, label):
    if not trades:
        print(f"  {label}: トレードなし")
        return None
    total = len(trades)
    wins = sum(1 for t in trades if t["result"] == "WIN")
    gross_p = sum(t["pips"] for t in trades if t["pips"] > 0)
    gross_l = abs(sum(t["pips"] for t in trades if t["pips"] < 0))
    pf = round(gross_p / gross_l, 2) if gross_l > 0 else 999.0
    total_pips = round(sum(t["pips"] for t in trades), 3)
    print(f"  {label}: 総{total}件 勝率{round(wins/total*100,1)}% PF{pf} 合計{total_pips}pips")
    return {"total": total, "win_rate": round(wins/total*100,1), "pf": pf, "total_pips": total_pips}


def main():
    dates, closes = load_usdjpy_daily()
    print(f"[INFO] USDJPY日足 {len(dates)}件 ({dates[0]} - {dates[-1]})\n")

    start_idx = 200
    last_entry_idx = -999
    baseline_trades = []
    rsi_filtered_trades = []
    excluded_trades = []  # ベースラインでは取ったがRSI条件で弾かれた分

    for i in range(start_idx, len(closes) - 1):
        if i - last_entry_idx < 10:
            continue
        hist = closes[:i + 1]
        ta = compute_ta_score(closes[i], hist)
        if ta["ta_score"] < TA_LONG_THRESHOLD:
            continue

        atr = atr_calc(hist, 14) or (closes[i] * 0.005)
        trade = simulate_long(closes, i, atr)
        trade["date"] = dates[i]
        last_entry_idx = i
        baseline_trades.append(trade)

        if rsi_oversold_reversal(hist):
            rsi_filtered_trades.append(trade)
        else:
            excluded_trades.append(trade)

    print("=" * 64)
    print(f"20年間・TA単独LONG条件(ta_score>={TA_LONG_THRESHOLD})での検証")
    print("=" * 64)
    summarize(baseline_trades, "ベースライン（RSI条件なし・全件）")
    print()
    summarize(rsi_filtered_trades, "RSI反発フィルター適用後（採用される分）")
    print()
    summarize(excluded_trades, "RSI条件で弾かれた分（除外される分）")

    print()
    print("判断の目安:")
    print("  ・「RSI適用後」が「ベースライン」より明確に良い → フィルターに本物のエッジ")
    print("  ・「除外分」が「RSI適用後」より明確に悪い → 弾く判断が正しい")
    print("  ・180日検証と同じ傾向が20年でも出るか → 直近結果がフルークでないことの確認")


if __name__ == "__main__":
    main()
