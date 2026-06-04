"""
デイトレ反発ロジックの過去データ検証

15分足の過去データで、サポート/レジスタンス反発エントリーの
4方式を比較し、勝率・期待値・最大ドローダウンを測定する。

方式:
  current : 現状（タッチ→終値POI上＋陽線で確定）
  improveA: 反発の強さ確認（タッチ後N本以内に戻りを確認）
  improveB: トレンドフィルター（EMA200整合のみ）
  improveC: A + B 両方

エントリー後はTP1/SL（ATRベース）のどちらに先に当たるかで勝敗判定。
「勝率より期待値」を重視して評価する。
"""

import statistics


def calc_ema(prices, period):
    if len(prices) < period:
        return None
    k = 2 / (period + 1)
    ema = prices[0]
    for p in prices[1:]:
        ema = p * k + ema * (1 - k)
    return ema


def calc_atr(highs, lows, closes, period=14):
    """簡易ATR（TRの平均）"""
    if len(closes) < period + 1:
        return None
    trs = []
    for i in range(1, len(closes)):
        tr = max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i - 1]),
            abs(lows[i] - closes[i - 1]),
        )
        trs.append(tr)
    if len(trs) < period:
        return None
    return statistics.mean(trs[-period:])


def calc_pivot(prev_high, prev_low, prev_close):
    pp = (prev_high + prev_low + prev_close) / 3
    r1 = 2 * pp - prev_low
    s1 = 2 * pp - prev_high
    return {"PP": pp, "R1": r1, "S1": s1}


def find_poi_levels(bars, i, lookback_day=96):
    """
    i本目の時点でのPOI（サポート/レジスタンス候補）を返す。
    bars: [{high, low, close}, ...]（15分足、96本≒1日）
    """
    closes = [b["close"] for b in bars[:i]]
    if len(closes) < 50:
        return []
    levels = []
    # 50EMA・200EMA
    ema50 = calc_ema(closes[-200:], 50) if len(closes) >= 50 else None
    ema200 = calc_ema(closes[-200:], 200) if len(closes) >= 200 else None
    if ema50:
        levels.append({"role": "50EMA", "price": ema50})
    if ema200:
        levels.append({"role": "200EMA", "price": ema200})
    # 前日のピボット（直近96本を前日とみなす）
    if i >= lookback_day:
        day_bars = bars[i - lookback_day:i]
        ph = max(b["high"] for b in day_bars)
        pl = min(b["low"] for b in day_bars)
        pc = day_bars[-1]["close"]
        piv = calc_pivot(ph, pl, pc)
        levels.append({"role": "PP", "price": piv["PP"]})
        levels.append({"role": "S1", "price": piv["S1"]})
        levels.append({"role": "R1", "price": piv["R1"]})
        levels.append({"role": "前日安値", "price": pl})
        levels.append({"role": "前日高値", "price": ph})
    return levels


def detect_entries(bars, method, pair):
    """
    指定方式で反発エントリーを検出。
    戻り値: [{index, direction, entry, sl, tp, role}, ...]
    """
    entries = []
    closes = [b["close"] for b in bars]
    highs = [b["high"] for b in bars]
    lows = [b["low"] for b in bars]
    pip = 0.01 if pair.endswith("JPY") else 0.0001

    i = 210  # EMA200が計算できる地点から
    while i < len(bars) - 20:  # 後ろは結果判定用に余裕
        bar = bars[i]
        atr = calc_atr(highs[:i + 1], lows[:i + 1], closes[:i + 1])
        if not atr:
            i += 1
            continue

        levels = find_poi_levels(bars, i)
        touched = None
        for lv in levels:
            # タッチ判定: 安値がPOI±ATR×0.5以内（サポート） or 高値が近い（レジスタンス）
            dist = abs(bar["low"] - lv["price"])
            dist_r = abs(bar["high"] - lv["price"])
            if dist <= atr * 0.5 and bar["close"] > lv["price"]:
                touched = {"lv": lv, "side": "support"}
                break
            if dist_r <= atr * 0.5 and bar["close"] < lv["price"]:
                touched = {"lv": lv, "side": "resistance"}
                break

        if not touched:
            i += 1
            continue

        lv = touched["lv"]
        is_support = touched["side"] == "support"
        direction = "LONG" if is_support else "SHORT"

        # --- 各方式の確定条件 ---
        confirmed = False

        # current: 終値がPOIを回復＋陽線（支持）/陰線（抵抗）
        is_bull = bar["close"] > bar["open"] if "open" in bar else bar["close"] > closes[i - 1]
        if method == "current":
            confirmed = (is_support and is_bull) or (not is_support and not is_bull)

        # improveA: 反発の強さ（タッチ後3本以内に戻りを確認）
        elif method == "improveA":
            confirmed = _confirm_strength(bars, i, is_support, atr, pip)

        # improveB: トレンドフィルター（EMA200整合）
        elif method == "improveB":
            ema200 = calc_ema(closes[:i + 1][-200:], 200)
            if ema200:
                trend_up = bar["close"] > ema200
                # 上昇トレンドではサポート反発ロングのみ、下降ではレジ反落ショートのみ
                confirmed = (is_support and trend_up) or (not is_support and not trend_up)

        # improveC: A + B
        elif method == "improveC":
            ema200 = calc_ema(closes[:i + 1][-200:], 200)
            trend_ok = False
            if ema200:
                trend_up = bar["close"] > ema200
                trend_ok = (is_support and trend_up) or (not is_support and not trend_up)
            confirmed = trend_ok and _confirm_strength(bars, i, is_support, atr, pip)

        if confirmed:
            entry = bar["close"]
            if is_support:
                sl = lv["price"] - atr * 1.5
                tp = entry + atr * 1.5  # TP1（RR1:1相当）
            else:
                sl = lv["price"] + atr * 1.5
                tp = entry - atr * 1.5
            entries.append({
                "index": i, "direction": direction,
                "entry": entry, "sl": sl, "tp": tp, "role": lv["role"], "atr": atr,
            })
            i += 8  # エントリー後は少し飛ばす（連続エントリー防止）
        else:
            i += 1

    return entries


def _confirm_strength(bars, i, is_support, atr, pip):
    """タッチ後、当該足を含め最近の動きで反発の勢いがあるか"""
    # 直近2本の値動きで、反発方向に atr×0.3 以上動いているか
    if i < 2:
        return False
    recent = bars[i - 1:i + 1]
    move = bars[i]["close"] - bars[i - 2]["close"]
    if is_support:
        return move > atr * 0.3  # 上向きの勢い
    else:
        return move < -atr * 0.3  # 下向きの勢い


def evaluate_entries(bars, entries, max_hold=20):
    """
    各エントリーのその後を追い、TP/SLどちらに先に当たったか判定。
    max_hold本以内に決着しなければ最終足で清算。
    """
    results = []
    for e in entries:
        idx = e["index"]
        outcome = None
        exit_price = None
        for j in range(idx + 1, min(idx + 1 + max_hold, len(bars))):
            hi = bars[j]["high"]
            lo = bars[j]["low"]
            if e["direction"] == "LONG":
                if lo <= e["sl"]:
                    outcome, exit_price = "LOSS", e["sl"]
                    break
                if hi >= e["tp"]:
                    outcome, exit_price = "WIN", e["tp"]
                    break
            else:
                if hi >= e["sl"]:
                    outcome, exit_price = "LOSS", e["sl"]
                    break
                if lo <= e["tp"]:
                    outcome, exit_price = "WIN", e["tp"]
                    break
        if outcome is None:
            # 時間切れ清算
            exit_price = bars[min(idx + max_hold, len(bars) - 1)]["close"]
            if e["direction"] == "LONG":
                outcome = "WIN" if exit_price > e["entry"] else "LOSS"
            else:
                outcome = "WIN" if exit_price < e["entry"] else "LOSS"

        pf = 100 if False else (0.01 if e["entry"] > 50 else 0.0001)  # pip size近似
        pip = 0.01 if e["entry"] > 10 else 0.0001
        if e["direction"] == "LONG":
            pips = (exit_price - e["entry"]) / pip
        else:
            pips = (e["entry"] - exit_price) / pip
        results.append({**e, "outcome": outcome, "exit": exit_price, "pips": pips})
    return results


def summarize(results, method):
    """方式ごとの成績を集計"""
    if not results:
        return {"method": method, "trades": 0}
    wins = [r for r in results if r["outcome"] == "WIN"]
    losses = [r for r in results if r["outcome"] == "LOSS"]
    n = len(results)
    win_rate = len(wins) / n * 100
    gross_win = sum(r["pips"] for r in wins)
    gross_loss = abs(sum(r["pips"] for r in losses))
    pf = gross_win / gross_loss if gross_loss > 0 else float("inf")
    total_pips = sum(r["pips"] for r in results)
    avg_win = statistics.mean([r["pips"] for r in wins]) if wins else 0
    avg_loss = statistics.mean([r["pips"] for r in losses]) if losses else 0
    # 期待値 = 勝率×平均利益 − 負率×平均損失
    p_win = len(wins) / n
    expectancy = p_win * avg_win + (1 - p_win) * avg_loss  # avg_lossは負値

    # 最大ドローダウン（pips累積ベース）
    cum = 0
    peak = 0
    max_dd = 0
    for r in results:
        cum += r["pips"]
        peak = max(peak, cum)
        max_dd = min(max_dd, cum - peak)

    # 役割別の勝率
    by_role = {}
    for r in results:
        role = r["role"]
        by_role.setdefault(role, {"w": 0, "n": 0})
        by_role[role]["n"] += 1
        if r["outcome"] == "WIN":
            by_role[role]["w"] += 1

    return {
        "method": method, "trades": n, "win_rate": round(win_rate, 1),
        "wins": len(wins), "losses": len(losses),
        "total_pips": round(total_pips, 1), "pf": round(pf, 2) if pf != float("inf") else "∞",
        "avg_win": round(avg_win, 1), "avg_loss": round(avg_loss, 1),
        "expectancy": round(expectancy, 2), "max_dd": round(max_dd, 1),
        "by_role": {k: f"{v['w']}/{v['n']}" for k, v in by_role.items()},
    }


def run_comparison(price_data, pair):
    """
    1ペアについて4方式を比較。
    price_data: {"highs":[], "lows":[], "closes":[], "opens":[]}
    """
    highs = price_data["highs"]
    lows = price_data["lows"]
    closes = price_data["closes"]
    opens = price_data.get("opens", closes)
    bars = [
        {"high": highs[i], "low": lows[i], "close": closes[i],
         "open": opens[i] if i < len(opens) else closes[i]}
        for i in range(len(closes))
    ]
    if len(bars) < 250:
        return None

    methods = ["current", "improveA", "improveB", "improveC"]
    summaries = {}
    for m in methods:
        entries = detect_entries(bars, m, pair)
        results = evaluate_entries(bars, entries)
        summaries[m] = summarize(results, m)
    return summaries
