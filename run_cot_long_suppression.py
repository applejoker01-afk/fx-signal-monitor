#!/usr/bin/env python3
"""
run_cot_long_suppression.py
COT円ショート巻き戻しシグナルを「新規LONGを抑制する早期警戒」として使えるか検証する。

これまでの検証（run_cot_unwind_backtest.py / run_cot_unwind_longhistory.py）で
「COT巻き戻し検知→SHORTエントリー」は2回とも不採用と判定された（ランダム基準にも
劣る）。本スクリプトは逆に「COT巻き戻し検知時は新規LONGを見送る」という、
新しい方向への賭けではなく既存の得意方向を一時停止するだけの、より保守的な
使い方を検証する。

方法:
  20年分のUSDJPY日足でTA単体のLONGエントリー条件（ta_score>=60、既存の
  評価ロジックと同じ閾値）を全期間で洗い出す。
  ベースライン: 全LONGシグナルをそのまま採用
  抑制あり: COT巻き戻し検知中(unwind_active)に出たLONGシグナルだけ見送る
  →「見送られたトレードだけ」を単体で集計し、それが平均より悪い成績だったか
    （＝抑制が正しい判断だったか）を直接確認する。
"""

from datetime import datetime

from signal_scanner import compute_ta_score, atr_calc
from modules.cot_analysis import load_cot_jpy, get_unwind_signal

PRICE_FILE = "data/usdjpy_daily.csv"
ATR_MULT = (3.0, 3.0, 4.5, 6.0)  # SL, TP1, TP2, TP3
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


def simulate_long(closes, entry_idx, atr):
    sl_mult, tp1_mult, tp2_mult, tp3_mult = ATR_MULT
    entry = closes[entry_idx]
    sl = entry - atr * sl_mult
    tp1, tp2, tp3 = entry + atr * tp1_mult, entry + atr * tp2_mult, entry + atr * tp3_mult
    for j in range(entry_idx + 1, min(entry_idx + 60, len(closes))):
        p = closes[j]
        if p <= sl:
            return {"result": "LOSS", "exit_reason": "SL_HIT", "pips": p - entry, "hold": j - entry_idx}
        if p >= tp3:
            return {"result": "WIN", "exit_reason": "TP3_HIT", "pips": tp3 - entry, "hold": j - entry_idx}
        if p >= tp2:
            return {"result": "WIN", "exit_reason": "TP2_HIT", "pips": tp2 - entry, "hold": j - entry_idx}
        if p >= tp1:
            return {"result": "WIN", "exit_reason": "TP1_HIT", "pips": tp1 - entry, "hold": j - entry_idx}
    end_idx = min(entry_idx + 59, len(closes) - 1)
    exit_p = closes[end_idx]
    pips = exit_p - entry
    return {"result": "WIN" if pips > 0 else "LOSS", "exit_reason": "TIME_EXIT",
            "pips": pips, "hold": end_idx - entry_idx}


def summarize(trades, label):
    if not trades:
        print(f"  {label}: トレードなし")
        return
    total = len(trades)
    wins = sum(1 for t in trades if t["result"] == "WIN")
    gross_p = sum(t["pips"] for t in trades if t["pips"] > 0)
    gross_l = abs(sum(t["pips"] for t in trades if t["pips"] < 0))
    pf = round(gross_p / gross_l, 2) if gross_l > 0 else 999.0
    total_pips = round(sum(t["pips"] for t in trades), 3)
    avg_pips = round(total_pips / total, 4)
    print(f"  {label}: 総{total}件 勝率{round(wins/total*100,1)}% PF{pf} "
          f"合計{total_pips}pips 平均{avg_pips}pips/件")
    return {"total": total, "win_rate": round(wins/total*100,1), "pf": pf,
            "total_pips": total_pips, "avg_pips": avg_pips}


def main():
    dates, closes = load_usdjpy_daily()
    cot_data = load_cot_jpy()
    print(f"[INFO] USDJPY日足 {len(dates)}件 ({dates[0]} - {dates[-1]})")
    print(f"[INFO] COTデータ {len(cot_data)}件\n")

    start_idx = 200
    all_long_trades = []
    suppressed_trades = []   # COT巻き戻し中に出たが、もし取っていたら…（検証用に実際にシミュレート）
    kept_trades = []         # 抑制フィルター適用後も残るトレード

    # 直近シグナルからクールダウン（同一トレンドの重複シグナルを除外、簡易的に10日空ける）
    last_entry_idx = -999

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

        all_long_trades.append(trade)

        sig = get_unwind_signal(dates[i], cot_data)
        if sig.get("unwind_active"):
            suppressed_trades.append(trade)
        else:
            kept_trades.append(trade)

    print("=" * 64)
    print(f"TA単体LONG条件(ta_score>={TA_LONG_THRESHOLD})での全シグナル: {len(all_long_trades)}件")
    print("=" * 64)
    summarize(all_long_trades, "ベースライン（COT考慮なし・全件）")
    print()
    summarize(kept_trades, "抑制あり（COT巻き戻し中のLONGを除外した残り）")
    print()
    summarize(suppressed_trades, "除外された分だけ（もし取っていたら…の成績）")

    print()
    print("判断の目安:")
    print("  ・「除外された分」が「ベースライン」より明確に悪い → 抑制は有効")
    print("  ・「除外された分」がベースラインと同程度 → 抑制しても意味がない")
    print("  ・「除外された分」の方が良い → 抑制は逆効果（巻き戻し中でもLONGは機能する）")


if __name__ == "__main__":
    main()
