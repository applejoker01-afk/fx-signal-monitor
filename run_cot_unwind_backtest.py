#!/usr/bin/env python3
"""
run_cot_unwind_backtest.py
COT円ショート巻き戻しシグナルをSHORTエントリーの独立トリガーとして
追加した場合のバックテスト。

設計方針（2026-08-11、検証の末に採用）:
  当初は「COT巻き戻し時にFAスコアへ固定ペナルティを引く」という設計を
  試みたが、現状のJPYクロスFAスコアは56〜86点に分布しており（金利差が
  支配的なため）、現実的なペナルティ幅ではSHORT閾値(<=45)まで押し下げ
  きれないペアが大半だった。そのため、COT巻き戻しを既存の金利差ゲートの
  「補正」ではなく、TAが十分弱い時に限定してレート差ゲートを迂回できる
  「独立した第二の入場経路」として設計し直した。

  V0 現状（COT不使用、rate-diff FA/TAゲートのみ）
  V1 COT迂回あり: ta_score<=45 かつ unwind_active(z>=1.5) → SHORT許可
  V2 COT迂回あり: ta_score<=40 かつ unwind_active(z>=1.5) → SHORT許可（TA厳しめ）
  V3 COT迂回あり: ta_score<=45 かつ unwind_active(z>=2.5) → SHORT許可（COT厳しめ）

  いずれもLONG側のロジックは一切変更しない（V0と完全に同一）。
  SHORT許可条件が追加されるかどうかだけの違い。

先読みバイアス対策: 各バックテスト日について、その日までに実際に公表
済み（release_date <= 当日）のCOTレポートのみを参照する。
"""

import os
import statistics
import urllib.request
import json as _json
from datetime import datetime, timedelta, timezone, date

from signal_scanner import compute_ta_score, PAIR_API
from modules.rate_fetcher import fetch_live_central_bank_rates, compute_fa_score
from modules.performance_intelligence import PAIR_EXCLUDE
from modules.cot_analysis import load_cot_jpy, get_unwind_signal

ATR_MULT = (3.0, 3.0, 4.5, 6.0)  # SL, TP1, TP2, TP3
TA_THRESHOLDS = (60, 55)  # 現状の本番閾値


def fetch_history_with_dates(pair, days=280):
    """signal_scanner.fetch_historyと同じAPIだが、日付も一緒に返す
    （COTシグナルを日付で引き当てるために必要）。"""
    frm, to = PAIR_API[pair]
    end = datetime.now(timezone.utc).date()
    start = end - timedelta(days=days)
    url = f"https://api.frankfurter.dev/v1/{start}..{end}?base={frm}&symbols={to}"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        data = _json.loads(urllib.request.urlopen(req, timeout=15).read())
        if not data or "rates" not in data:
            return None, None
        series = sorted((d, v[to]) for d, v in data["rates"].items() if to in v)
        dates = [datetime.strptime(d, "%Y-%m-%d").date() for d, _ in series]
        closes = [v for _, v in series]
        return dates, closes
    except Exception as e:
        print(f"[WARN] history failed for {pair}: {e}")
        return None, None


def run_pair_backtest(pair, dates, closes, cb_rates, cot_data, lookback_days,
                       cot_mode, ta_short_max=45, z_threshold=1.5):
    """
    1ペアのバックテスト。modules/backtest.run_backtestを土台に、
    JPYクロス限定でCOT迂回SHORTを追加できるようにしたもの。

    cot_mode: None（V0=無効）または {"ta_max": int, "z": float}（迂回ON）
    """
    n = len(closes)
    if n < 60:
        return {"pair": pair, "trades": [], "total": 0}

    start_idx = max(50, n - lookback_days)
    sl_mult, tp1_mult, tp2_mult, tp3_mult = ATR_MULT
    ta_long, fa_long = TA_THRESHOLDS
    ta_short_rate = 100 - ta_long
    fa_short_rate = 100 - fa_long

    trades = []
    open_trade = None
    cot_short_count = 0

    for i in range(start_idx, n):
        hist = closes[:i + 1]
        current_price = closes[i]
        current_date = dates[i]

        if open_trade:
            direction = open_trade["direction"]
            entry = open_trade["entry_price"]
            sl, tp1, tp2, tp3 = open_trade["sl"], open_trade["tp1"], open_trade["tp2"], open_trade["tp3"]
            is_long = direction == "LONG"
            exit_reason = None
            exit_price = current_price
            if is_long:
                if current_price <= sl: exit_reason, exit_price = "SL_HIT", sl
                elif current_price >= tp3: exit_reason, exit_price = "TP3_HIT", tp3
                elif current_price >= tp2: exit_reason, exit_price = "TP2_HIT", tp2
                elif current_price >= tp1: exit_reason, exit_price = "TP1_HIT", tp1
            else:
                if current_price >= sl: exit_reason, exit_price = "SL_HIT", sl
                elif current_price <= tp3: exit_reason, exit_price = "TP3_HIT", tp3
                elif current_price <= tp2: exit_reason, exit_price = "TP2_HIT", tp2
                elif current_price <= tp1: exit_reason, exit_price = "TP1_HIT", tp1

            if exit_reason:
                pips = (exit_price - entry) if is_long else (entry - exit_price)
                trades.append({
                    "entry_idx": open_trade["entry_idx"], "exit_idx": i,
                    "direction": direction, "entry_price": round(entry, 5),
                    "exit_price": round(exit_price, 5), "exit_reason": exit_reason,
                    "pips": round(pips, 5),
                    "result": "WIN" if exit_reason.startswith("TP") else "LOSS",
                    "hold_days": i - open_trade["entry_idx"],
                    "via_cot": open_trade.get("via_cot", False),
                })
                open_trade = None

        if open_trade is None:
            ta = compute_ta_score(current_price, hist)
            fa = compute_fa_score(pair, PAIR_API, cb_rates)
            ta_sign = 1 if ta["ta_score"] > 50 else (-1 if ta["ta_score"] < 50 else 0)
            fa_sign = 1 if fa["direction"] == "buy" else (-1 if fa["direction"] == "sell" else 0)
            agree = ta_sign == fa_sign and ta_sign != 0

            entry_dir = None
            via_cot = False
            if agree and ta["ta_score"] >= ta_long and fa["score"] >= fa_long:
                entry_dir = "LONG" if fa_sign > 0 else "SHORT"
            elif agree and ta["ta_score"] <= ta_short_rate and fa["score"] <= fa_short_rate:
                entry_dir = "SHORT"
            elif cot_mode is not None and ta["ta_score"] <= cot_mode["ta_max"]:
                # COT迂回経路: レート差FAが買い方向でも、投機筋の急速な円ショート
                # 巻き戻しが検知されていればSHORTを許可する
                sig = get_unwind_signal(current_date, cot_data, z_threshold=cot_mode["z"])
                if sig.get("unwind_active"):
                    entry_dir = "SHORT"
                    via_cot = True
                    cot_short_count += 1

            if entry_dir:
                atr = ta.get("atr") or (current_price * 0.005)
                if entry_dir == "LONG":
                    sl = current_price - atr * sl_mult
                    tp1, tp2, tp3 = (current_price + atr * tp1_mult,
                                     current_price + atr * tp2_mult,
                                     current_price + atr * tp3_mult)
                else:
                    sl = current_price + atr * sl_mult
                    tp1, tp2, tp3 = (current_price - atr * tp1_mult,
                                      current_price - atr * tp2_mult,
                                      current_price - atr * tp3_mult)
                open_trade = {
                    "entry_idx": i, "entry_price": current_price, "direction": entry_dir,
                    "sl": sl, "tp1": tp1, "tp2": tp2, "tp3": tp3, "via_cot": via_cot,
                }

    if not trades:
        return {"pair": pair, "trades": [], "total": 0, "cot_short_signals_seen": cot_short_count}

    total = len(trades)
    wins = sum(1 for t in trades if t["result"] == "WIN")
    gross_p = sum(t["pips"] for t in trades if t["pips"] > 0)
    gross_l = abs(sum(t["pips"] for t in trades if t["pips"] < 0))
    pf = round(gross_p / gross_l, 2) if gross_l > 0 else 999.0
    return {
        "pair": pair, "trades": trades, "total": total, "wins": wins,
        "win_rate": round(wins / total * 100, 1), "profit_factor": pf,
        "total_pips": round(sum(t["pips"] for t in trades), 4),
        "cot_short_signals_seen": cot_short_count,
    }


def main():
    lookback = int(os.environ.get("BACKTEST_DAYS", "180"))
    cb_rates = fetch_live_central_bank_rates()
    cot_data = load_cot_jpy()
    print(f"[INFO] COTデータ {len(cot_data)}件 ({cot_data[0]['report_date']} - {cot_data[-1]['report_date']})")

    jpy_pairs = [p for p in PAIR_API if p.endswith("JPY") and p not in PAIR_EXCLUDE]
    print(f"[INFO] 対象JPYクロス {len(jpy_pairs)}ペア: {jpy_pairs}\n")

    histories = {}
    for pair in jpy_pairs:
        dates, closes = fetch_history_with_dates(pair, 280)
        if dates and len(dates) >= 60:
            histories[pair] = (dates, closes)
    print(f"[INFO] {len(histories)}ペア取得完了\n")

    variants = {
        "V0_現状(COT無し)": None,
        "V1_ta45/z1.5": {"ta_max": 45, "z": 1.5},
        "V2_ta40/z1.5": {"ta_max": 40, "z": 1.5},
        "V3_ta45/z2.5": {"ta_max": 45, "z": 2.5},
    }

    for name, cot_mode in variants.items():
        print("=" * 64)
        print(name)
        print("=" * 64)
        all_trades = []
        cot_short_seen_total = 0
        for pair, (dates, closes) in histories.items():
            r = run_pair_backtest(pair, dates, closes, cb_rates, cot_data,
                                   lookback, cot_mode)
            cot_short_seen_total += r.get("cot_short_signals_seen", 0)
            all_trades.extend(r.get("trades", []))

        if not all_trades:
            print("  トレードなし\n")
            continue

        total = len(all_trades)
        wins = sum(1 for t in all_trades if t["result"] == "WIN")
        gross_p = sum(t["pips"] for t in all_trades if t["pips"] > 0)
        gross_l = abs(sum(t["pips"] for t in all_trades if t["pips"] < 0))
        pf = round(gross_p / gross_l, 2) if gross_l > 0 else 999.0
        total_pips = round(sum(t["pips"] for t in all_trades), 4)

        longs = [t for t in all_trades if t["direction"] == "LONG"]
        shorts = [t for t in all_trades if t["direction"] == "SHORT"]
        cot_shorts = [t for t in shorts if t.get("via_cot")]

        print(f"  全体: 総{total}件 勝率{round(wins/total*100,1)}% PF{pf} 合計{total_pips}pips")
        print(f"  LONG: {len(longs)}件")
        print(f"  SHORT: {len(shorts)}件（うちCOT迂回発動: {len(cot_shorts)}件）")
        if cot_shorts:
            cs_wins = sum(1 for t in cot_shorts if t["result"] == "WIN")
            cs_pips = round(sum(t["pips"] for t in cot_shorts), 4)
            cs_gp = sum(t["pips"] for t in cot_shorts if t["pips"] > 0)
            cs_gl = abs(sum(t["pips"] for t in cot_shorts if t["pips"] < 0))
            cs_pf = round(cs_gp / cs_gl, 2) if cs_gl > 0 else 999.0
            print(f"    COT迂回SHORTのみ: 勝率{round(cs_wins/len(cot_shorts)*100,1)}% "
                  f"PF{cs_pf} 合計{cs_pips}pips")
            for t in cot_shorts:
                print(f"      idx{t['entry_idx']} {t['result']} {t['exit_reason']} pips={t['pips']}")
        print()


if __name__ == "__main__":
    main()
