#!/usr/bin/env python3
"""
run_macd_deadcross_rsi_short_test.py
「MACDデッドクロス + RSI反発」をSHORTエントリーの追加ANDゲートとして
既存のTA/FAベースSHORT判定に重ねた場合の有効性を検証する。

2026-08-26、対JPY全ペアでの「急落→半値戻し」バックテスト（本スクリプトとは
別の一回限りの分析）で、新興国通貨（TRY/ZAR/INR/PLN/SEK）は半値戻し後に
上昇継続せず下落再開する傾向が見えた。これを「戻り売り」のタイミング
シグナルとして使えないかというユーザーの発案の検証。

run_macd_rsi_ichimoku_gate_test.py（2026-08-11、LONG向け: MACDゴールデン
クロス+RSI反発でエントリー精度が上がるか検証、結論は機会損失だった）と
対になる検証。RSI反発の定義（直近10日以内にRSI<=40まで下げ、かつ直近RSIが
3日前より上昇=底からの反発局面）はそのまま流用し、MACDのみゴールデン→
デッドクロスに反転させ、base条件もLONG→SHORTに反転させている。

このシステムは元々SHORTが構造的にほぼゼロ（run_macd_rsi_ichimoku_gate_test.py
のコメント参照）なので、まずbase SHORT条件だけでどれだけ機会があるかも
V0で確認する。
"""

import os
import statistics
from datetime import datetime, timedelta, timezone

from signal_scanner import (
    compute_ta_score, PAIR_API,
    ema_series, rsi as rsi_fn, macd_now,
)
from modules.rate_fetcher import fetch_live_central_bank_rates
from modules.performance_intelligence import PAIR_EXCLUDE

ATR_MULT = (3.0, 3.0, 4.5, 6.0)
TA_THRESHOLD = 60  # ta_score <= (100-60)=40 でSHORT対象（LONG閾値60をミラー）


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


def macd_dead_cross_recent(hist, lookback=3):
    """直近lookback営業日以内にMACD<signalへ切り替わったか（イベント検知）"""
    if len(hist) < 40:
        return False
    states = []
    for back in range(lookback, -1, -1):
        sub = hist[:len(hist) - back] if back > 0 else hist
        m, s = macd_now(sub)
        if m is None or s is None:
            return False
        states.append(m > s)
    # 過去は True（signal超え）で、直近が False（signal以下）に切り替わった瞬間があるか
    return states[0] and (not states[-1]) and any(not st for st in states)


def rsi_oversold_reversal(hist, dip_threshold=40, lookback=10):
    """直近lookback日以内にRSIがdip_threshold以下まで下がり、
    かつ直近RSIが3日前より上昇しているか（=底からの反発局面、V2と同一定義）"""
    if len(hist) < 30:
        return False
    r_now = rsi_fn(hist, 14)
    r_prev3 = rsi_fn(hist[:-3], 14) if len(hist) > 3 else None
    if r_now is None or r_prev3 is None:
        return False
    if r_now <= r_prev3:
        return False
    min_r = 100
    for back in range(0, lookback):
        sub = hist[:len(hist) - back] if back > 0 else hist
        r = rsi_fn(sub, 14)
        if r is not None:
            min_r = min(min_r, r)
    return min_r <= dip_threshold


def run_pair(pair, closes, cb_rates, lookback_days, gate_mode):
    n = len(closes)
    if n < 100:
        return {"pair": pair, "trades": []}
    start_idx = max(90, n - lookback_days)
    sl_mult, tp1_mult, tp2_mult, tp3_mult = ATR_MULT
    ta_thresh = TA_THRESHOLD

    trades = []
    open_trade = None

    for i in range(start_idx, n):
        hist = closes[:i + 1]
        price = closes[i]

        if open_trade:
            entry, sl = open_trade["entry_price"], open_trade["sl"]
            tp1, tp2, tp3 = open_trade["tp1"], open_trade["tp2"], open_trade["tp3"]
            exit_reason = None
            exit_price = price
            # SHORT: SLは上、TPは下
            if price >= sl: exit_reason, exit_price = "SL_HIT", sl
            elif price <= tp3: exit_reason, exit_price = "TP3_HIT", tp3
            elif price <= tp2: exit_reason, exit_price = "TP2_HIT", tp2
            elif price <= tp1: exit_reason, exit_price = "TP1_HIT", tp1
            if exit_reason:
                pips = entry - exit_price  # SHORTなので下がった分がプラス
                trades.append({
                    "entry_idx": open_trade["entry_idx"], "exit_idx": i,
                    "pips": round(pips, 5),
                    "result": "WIN" if exit_reason.startswith("TP") else "LOSS",
                    "exit_reason": exit_reason,
                })
                open_trade = None

        if open_trade is None:
            ta = compute_ta_score(price, hist)
            ta_sign = 1 if ta["ta_score"] > 50 else (-1 if ta["ta_score"] < 50 else 0)
            # 2026-08-26: FA一致条件は撤廃。対JPYは金利差構造上ほぼ全ペアで
            # FA方向が"buy"に偏っており(実測27/27でsell=0件)、FA一致を要求すると
            # base短が恒久的にゼロ件になる（run_macd_rsi_ichimoku_gate_test.py
            # のコメント通り）。本検証はキャリーに逆張りする短期テクニカルの
            # フェード狙いなので、TA単独で判定する。
            base_short = ta_sign < 0 and ta["ta_score"] <= (100 - ta_thresh)

            if base_short:
                ok = True
                if gate_mode.get("macd") and not macd_dead_cross_recent(hist):
                    ok = False
                if ok and gate_mode.get("rsi") and not rsi_oversold_reversal(hist):
                    ok = False
                if ok:
                    atr = ta.get("atr") or (price * 0.005)
                    sl = price + atr * sl_mult
                    tp1, tp2, tp3 = price - atr * tp1_mult, price - atr * tp2_mult, price - atr * tp3_mult
                    open_trade = {"entry_idx": i, "entry_price": price, "direction": "SHORT",
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
        "V0_現状(SHORT基本条件のみ)": {},
        "V1_+MACDデッドクロス": {"macd": True},
        "V2_+RSI反発": {"rsi": True},
        "V3_+両方(デッドクロス&RSI反発)": {"macd": True, "rsi": True},
    }

    for name, gate in variants.items():
        all_trades = []
        by_pair = {}
        for pair, closes in histories.items():
            r = run_pair(pair, closes, cb_rates, lookback, gate)
            all_trades.extend(r["trades"])
            if r["trades"]:
                by_pair[pair] = r["trades"]
        print(f"=== {name} ===")
        summarize(all_trades, "全体")
        for pair, trades in sorted(by_pair.items()):
            wins = sum(1 for t in trades if t["result"] == "WIN")
            print(f"    {pair}: {len(trades)}件 勝率{round(wins/len(trades)*100,1)}%")
        print()


if __name__ == "__main__":
    main()
