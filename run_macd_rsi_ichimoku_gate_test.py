#!/usr/bin/env python3
"""
run_macd_rsi_ichimoku_gate_test.py
「MACD golden cross + RSI oversold reversal + 一目均衡表 雲回帰」を
既存のTA/FAゲートに追加のAND条件として重ねた場合、機会損失になるか検証する。

2026-08-11、GBPJPY 3連敗直後にユーザーから出た当初の疑問への直接検証。
これまで「閾値を上げると機会損失+質も悪化する」パターンを何度も確認したが、
このMACD/RSI/一目均衡表の追加自体は一度もバックテストしていなかった。

指標の定義（ユーザーの実際の観察パターンに合わせる）:
  - MACD golden cross: 直近3営業日以内にMACD線がシグナル線を下から上に
    交差した（=状態ではなくイベントとして検知。「ずっとMACD>signal」なら
    対象外）
  - RSI oversold reversal: 直近10営業日以内にRSIが40以下まで下がり、
    かつ直近時点のRSIが3営業日前より上昇している（=底からの反発局面）
  - 一目均衡表 雲回帰: 終値ベースの近似計算（真のOHLCデータ無し、既知の
    制約）で、価格が雲の上限からの±0.3%以内、または雲を上抜けている

variants:
  V0 現状（追加条件なし）
  V1 +MACDのみ
  V2 +RSIのみ
  V3 +一目均衡表のみ
  V4 +全部（当初提案そのもの）

いずれもLONG判定に対してのみ追加条件を課す（このシステムはSHORTが構造的に
ほぼゼロなので、SHORT側の検証は意味を持たない）。
"""

import os
import statistics
from datetime import datetime, timedelta, timezone

from signal_scanner import (
    compute_ta_score, compute_fa_score, PAIR_API,
    ema_series, rsi as rsi_fn, macd_now,
)
from modules.rate_fetcher import fetch_live_central_bank_rates
from modules.performance_intelligence import PAIR_EXCLUDE

ATR_MULT = (3.0, 3.0, 4.5, 6.0)
TA_THRESHOLDS = (60, 55)


def fetch_history(pair, days=280):
    import urllib.request, json as _json
    frm, to = PAIR_API[pair]
    end = datetime.now(timezone.utc).date()
    start = end - timedelta(days=days)
    url = f"https://api.frankfurter.dev/v1/{start}..{end}?base={frm}&symbols={to}"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        data = _json.loads(urllib.request.urlopen(req, timeout=15).read())
        if not data or "rates" not in data:
            return None
        series = sorted((d, v[to]) for d, v in data["rates"].items() if to in v)
        return [v for _, v in series]
    except Exception:
        return None


def macd_golden_cross_recent(hist, lookback=3):
    """直近lookback営業日以内にMACD>signalへ切り替わったか（イベント検知）"""
    if len(hist) < 40:
        return False
    states = []
    for back in range(lookback, -1, -1):
        sub = hist[:len(hist) - back] if back > 0 else hist
        m, s = macd_now(sub)
        if m is None or s is None:
            return False
        states.append(m > s)
    # 過去は False（signal以下）で、直近が True（signal超え）に切り替わった瞬間があるか
    return (not states[0]) and states[-1] and any(states)


def rsi_oversold_reversal(hist, dip_threshold=40, lookback=10):
    """直近lookback日以内にRSIがdip_threshold以下まで下がり、
    かつ直近RSIが3日前より上昇しているか"""
    if len(hist) < 30:
        return False
    r_now = rsi_fn(hist, 14)
    r_prev3 = rsi_fn(hist[:-3], 14) if len(hist) > 3 else None
    if r_now is None or r_prev3 is None:
        return False
    if r_now <= r_prev3:
        return False
    # 直近lookback日以内の最小RSIがdip_threshold以下か
    min_r = 100
    for back in range(0, lookback):
        sub = hist[:len(hist) - back] if back > 0 else hist
        r = rsi_fn(sub, 14)
        if r is not None:
            min_r = min(min_r, r)
    return min_r <= dip_threshold


def ichimoku_cloud_position(hist, tolerance_pct=0.3):
    """終値ベースの近似一目均衡表。雲の上限±tolerance_pct%以内、または上抜けか"""
    n = len(hist)
    if n < 78:  # 52 + 26 の投影に必要な最低本数
        return None
    def hh_ll(prices, i, period):
        w = prices[max(0, i - period + 1):i + 1]
        return max(w), min(w)
    i = n - 1
    proj_i = i - 26
    if proj_i < 51:
        return None
    h9, l9 = hh_ll(hist, proj_i, 9)
    h26, l26 = hh_ll(hist, proj_i, 26)
    h52, l52 = hh_ll(hist, proj_i, 52)
    tenkan = (h9 + l9) / 2
    kijun = (h26 + l26) / 2
    senkouA = (tenkan + kijun) / 2
    senkouB = (h52 + l52) / 2
    cloud_top = max(senkouA, senkouB)
    cloud_bottom = min(senkouA, senkouB)
    price = hist[i]
    if price >= cloud_top * (1 - tolerance_pct / 100):
        return True  # 雲の上限付近〜上抜け
    return False


def run_pair(pair, closes, cb_rates, lookback_days, gate_mode):
    n = len(closes)
    if n < 100:
        return {"pair": pair, "trades": []}
    start_idx = max(90, n - lookback_days)
    sl_mult, tp1_mult, tp2_mult, tp3_mult = ATR_MULT
    ta_long, fa_long = TA_THRESHOLDS

    trades = []
    open_trade = None

    for i in range(start_idx, n):
        hist = closes[:i + 1]
        price = closes[i]

        if open_trade:
            direction = open_trade["direction"]
            entry, sl = open_trade["entry_price"], open_trade["sl"]
            tp1, tp2, tp3 = open_trade["tp1"], open_trade["tp2"], open_trade["tp3"]
            exit_reason = None
            exit_price = price
            if price <= sl: exit_reason, exit_price = "SL_HIT", sl
            elif price >= tp3: exit_reason, exit_price = "TP3_HIT", tp3
            elif price >= tp2: exit_reason, exit_price = "TP2_HIT", tp2
            elif price >= tp1: exit_reason, exit_price = "TP1_HIT", tp1
            if exit_reason:
                pips = exit_price - entry
                trades.append({
                    "entry_idx": open_trade["entry_idx"], "exit_idx": i,
                    "pips": round(pips, 5),
                    "result": "WIN" if exit_reason.startswith("TP") else "LOSS",
                    "exit_reason": exit_reason,
                })
                open_trade = None

        if open_trade is None:
            ta = compute_ta_score(price, hist)
            fa = compute_fa_score(pair, PAIR_API, cb_rates)
            ta_sign = 1 if ta["ta_score"] > 50 else (-1 if ta["ta_score"] < 50 else 0)
            fa_sign = 1 if fa["direction"] == "buy" else (-1 if fa["direction"] == "sell" else 0)
            agree = ta_sign == fa_sign and ta_sign != 0

            base_long = agree and ta["ta_score"] >= ta_long and fa["score"] >= fa_long and fa_sign > 0

            if base_long:
                ok = True
                if gate_mode.get("macd") and not macd_golden_cross_recent(hist):
                    ok = False
                if ok and gate_mode.get("rsi") and not rsi_oversold_reversal(hist):
                    ok = False
                if ok and gate_mode.get("ichimoku"):
                    pos = ichimoku_cloud_position(hist)
                    if pos is not True:
                        ok = False
                if ok:
                    atr = ta.get("atr") or (price * 0.005)
                    sl = price - atr * sl_mult
                    tp1, tp2, tp3 = price + atr * tp1_mult, price + atr * tp2_mult, price + atr * tp3_mult
                    open_trade = {"entry_idx": i, "entry_price": price, "direction": "LONG",
                                  "sl": sl, "tp1": tp1, "tp2": tp2, "tp3": tp3}

    return {"pair": pair, "trades": trades}


def summarize(all_trades, label):
    if not all_trades:
        print(f"  {label}: トレードなし")
        return
    total = len(all_trades)
    wins = sum(1 for t in all_trades if t["result"] == "WIN")
    gross_p = sum(t["pips"] for t in all_trades if t["pips"] > 0)
    gross_l = abs(sum(t["pips"] for t in all_trades if t["pips"] < 0))
    pf = round(gross_p / gross_l, 2) if gross_l > 0 else 999.0
    total_pips = round(sum(t["pips"] for t in all_trades), 3)
    print(f"  {label}: 総{total}件 勝率{round(wins/total*100,1)}% PF{pf} 合計{total_pips}pips")


def main():
    lookback = int(os.environ.get("BACKTEST_DAYS", "180"))
    cb_rates = fetch_live_central_bank_rates()
    tradable = [p for p in PAIR_API if p not in PAIR_EXCLUDE]
    print(f"[INFO] {len(tradable)}ペアの履歴取得中...")
    histories = {}
    for pair in tradable:
        closes = fetch_history(pair, 280)
        if closes and len(closes) >= 100:
            histories[pair] = closes
    print(f"[INFO] {len(histories)}ペア取得完了\n")

    variants = {
        "V0_現状(追加条件なし)": {},
        "V1_+MACDゴールデンクロス": {"macd": True},
        "V2_+RSI反発": {"rsi": True},
        "V3_+一目均衡表雲回帰": {"ichimoku": True},
        "V4_+全部(当初提案)": {"macd": True, "rsi": True, "ichimoku": True},
    }

    for name, gate in variants.items():
        all_trades = []
        for pair, closes in histories.items():
            r = run_pair(pair, closes, cb_rates, lookback, gate)
            all_trades.extend(r["trades"])
        print(f"=== {name} ===")
        summarize(all_trades, "結果")
        print()


if __name__ == "__main__":
    main()
