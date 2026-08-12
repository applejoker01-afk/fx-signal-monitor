#!/usr/bin/env python3
"""
run_cot_unwind_longhistory.py
COT円ショート巻き戻しシグナルを、20年分のUSDJPY日足データ(data/usdjpy_daily.csv)
とCOT全履歴(data/cot_jpy.csv, 2005年〜)で検証する。

前回(run_cot_unwind_backtest.py)の反省:
  直近180日では巻き戻しイベントが実質1〜2回しかなく、"SHORT全敗"という
  結果が統計的に意味を持つか判断できなかった。また、金利差FAゲートは
  現在の金利スナップショットを過去全期間に一律適用する設計のため、
  20年前の金利環境を再現できず、長期バックテストには使えない。

  そこで本スクリプトでは、FAの金利差ゲートを経由せず、
  「COT巻き戻し検知 + TA弱気」の組み合わせ単体が、その後のUSDJPYの
  値動きに対して有効かどうかを、20年分・数十回の巻き戻しイベントで
  直接検証する。

比較対象:
  A: COT巻き戻し検知時点でSHORTエントリー（TA条件なし・COT単体の力を見る）
  B: COT巻き戻し検知 かつ TA弱気(ta_score<=45)でSHORTエントリー
  C: ランダム基準（巻き戻し検知の有無を無視し、同数のサンプルを全期間から
     無作為抽出した場合の平均pipsとの比較。COTシグナルに意味があるかの目安）
"""

import random
import statistics
from datetime import datetime

from signal_scanner import compute_ta_score, atr_calc
from modules.cot_analysis import load_cot_jpy, get_unwind_signal

PRICE_FILE = "data/usdjpy_daily.csv"
ATR_MULT = (3.0, 3.0, 4.5, 6.0)  # SL, TP1, TP2, TP3


def load_usdjpy_daily():
    dates, closes = [], []
    with open(PRICE_FILE, "r", encoding="utf-8") as f:
        next(f)  # header
        for line in f:
            d, p = line.strip().split(",")
            dates.append(datetime.strptime(d, "%Y-%m-%d").date())
            closes.append(float(p))
    return dates, closes


def simulate_short(closes, entry_idx, atr):
    """entry_idxでSHORTエントリーした場合のTP/SL到達をシミュレーション。
    最大60営業日(約3ヶ月)保有して未到達なら時価決済。"""
    sl_mult, tp1_mult, tp2_mult, tp3_mult = ATR_MULT
    entry = closes[entry_idx]
    sl = entry + atr * sl_mult
    tp1, tp2, tp3 = entry - atr * tp1_mult, entry - atr * tp2_mult, entry - atr * tp3_mult
    for j in range(entry_idx + 1, min(entry_idx + 60, len(closes))):
        p = closes[j]
        if p >= sl:
            return {"result": "LOSS", "exit_reason": "SL_HIT", "pips": entry - sl, "hold": j - entry_idx}
        if p <= tp3:
            return {"result": "WIN", "exit_reason": "TP3_HIT", "pips": entry - tp3, "hold": j - entry_idx}
        if p <= tp2:
            return {"result": "WIN", "exit_reason": "TP2_HIT", "pips": entry - tp2, "hold": j - entry_idx}
        if p <= tp1:
            return {"result": "WIN", "exit_reason": "TP1_HIT", "pips": entry - tp1, "hold": j - entry_idx}
    # 未到達 → 60日後の時価で決済
    end_idx = min(entry_idx + 59, len(closes) - 1)
    exit_p = closes[end_idx]
    pips = entry - exit_p
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


def main():
    dates, closes = load_usdjpy_daily()
    print(f"[INFO] USDJPY日足 {len(dates)}件 ({dates[0]} - {dates[-1]})")
    cot_data = load_cot_jpy()
    print(f"[INFO] COTデータ {len(cot_data)}件 ({cot_data[0]['report_date']} - {cot_data[-1]['report_date']})\n")

    # 200日目以降（ATR/TA計算に必要な最低履歴を確保）から検証開始
    start_idx = 200

    trades_a = []  # COT単体
    trades_b = []  # COT + TA弱気
    unwind_dates = []

    for i in range(start_idx, len(closes) - 1):
        d = dates[i]
        sig = get_unwind_signal(d, cot_data)
        if not sig.get("unwind_active"):
            continue
        unwind_dates.append(d)

        hist = closes[:i + 1]
        atr = atr_calc(hist, 14) or (closes[i] * 0.005)

        # A: COT単体
        trades_a.append({**simulate_short(closes, i, atr), "date": d})

        # B: COT + TA弱気
        ta = compute_ta_score(closes[i], hist)
        if ta["ta_score"] <= 45:
            trades_b.append({**simulate_short(closes, i, atr), "date": d, "ta_score": ta["ta_score"]})

    print(f"[INFO] 巻き戻し検知回数（延べ日数、連続日はほぼ同一イベント）: {len(unwind_dates)}")
    if unwind_dates:
        # 連続する検知日をイベント単位にまとめる（1週間以内の連続検知は同一イベント扱い）
        events = []
        cur = [unwind_dates[0]]
        for d in unwind_dates[1:]:
            if (d - cur[-1]).days <= 10:
                cur.append(d)
            else:
                events.append(cur)
                cur = [d]
        events.append(cur)
        print(f"[INFO] 実質的な巻き戻しイベント数: {len(events)}")
        print(f"[INFO] イベント開始日一覧: {[e[0].isoformat() for e in events]}\n")

    print("=" * 64)
    print("結果")
    print("=" * 64)
    summarize(trades_a, "A: COT単体トリガー")
    summarize(trades_b, "B: COT + TA弱気(ta<=45)")

    # C: ランダム基準（同期間・同回数を無作為抽出してSHORTした場合の比較）
    random.seed(42)
    n_sample = max(len(trades_a), 1)
    random_trades = []
    candidates = list(range(start_idx, len(closes) - 60))
    for idx in random.sample(candidates, min(n_sample, len(candidates))):
        hist = closes[:idx + 1]
        atr = atr_calc(hist, 14) or (closes[idx] * 0.005)
        random_trades.append(simulate_short(closes, idx, atr))
    summarize(random_trades, f"C: ランダム基準(n={len(random_trades)}、同数を無作為抽出)")

    print()
    print("判断の目安:")
    print("  ・A/BがCより明確に良い → COTシグナルに本当のエッジがある可能性")
    print("  ・A/BがCと同程度以下 → 前回の「全敗」は本物のエッジ無し（もしくは逆エッジ）を示している")


if __name__ == "__main__":
    main()
