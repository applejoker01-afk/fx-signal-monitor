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


def calc_ema_series(prices, period):
    """EMAの時系列を返す"""
    if len(prices) < period:
        return None
    k = 2 / (period + 1)
    ema = prices[0]
    series = [ema]
    for p in prices[1:]:
        ema = p * k + ema * (1 - k)
        series.append(ema)
    return series


def calc_macd(closes, fast=12, slow=26, signal=9):
    """MACD line, signal line を返す（最新値のみ）"""
    if len(closes) < slow + signal:
        return None, None
    ema_fast = calc_ema_series(closes, fast)
    ema_slow = calc_ema_series(closes, slow)
    if not ema_fast or not ema_slow:
        return None, None
    # MACDライン = fast - slow（末尾を揃える）
    macd_line = [ema_fast[i] - ema_slow[i] for i in range(len(closes))]
    # シグナル = MACDのEMA
    signal_series = calc_ema_series(macd_line, signal)
    if not signal_series:
        return None, None
    return macd_line[-1], signal_series[-1]


def calc_rsi(closes, period=14):
    """RSI（最新値）"""
    if len(closes) < period + 1:
        return None
    gains, losses = [], []
    for i in range(1, len(closes)):
        diff = closes[i] - closes[i - 1]
        gains.append(max(diff, 0))
        losses.append(max(-diff, 0))
    avg_gain = statistics.mean(gains[-period:])
    avg_loss = statistics.mean(losses[-period:])
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


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


def detect_entries(bars, method, pair, rr=1.0, reverse=False):
    """
    指定方式で反発エントリーを検出。
    rr: リスクリワード比（TP = SL幅 × rr）。1.0ならTP=SL。
    reverse: Trueならエントリー方向を反転（逆張りの逆＝順張り的）。
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
            # reverse: 反発の逆方向にエントリー
            actual_long = is_support if not reverse else (not is_support)
            actual_dir = "LONG" if actual_long else "SHORT"
            if actual_long:
                # ロング: SLはエントリー下、TPは上
                sl = entry - atr * 1.5
                risk = entry - sl
                tp = entry + risk * rr
            else:
                sl = entry + atr * 1.5
                risk = sl - entry
                tp = entry - risk * rr
            entries.append({
                "index": i, "direction": actual_dir,
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


def detect_resonance_entries(bars, pair, rr=2.0):
    """
    三次元共鳴（順張り）エントリーを検出。
    次元1: EMA20/60/120パーフェクトオーダー（トレンド）
    次元2: MACD > signal + RSI > 50（モメンタム）
    次元3: 押し目からの転換（プライスアクション）
    SLは直近スイング、TPはRR倍。
    """
    entries = []
    closes = [b["close"] for b in bars]
    highs = [b["high"] for b in bars]
    lows = [b["low"] for b in bars]

    i = 130  # EMA120 + MACD が計算できる地点から
    while i < len(bars) - 30:
        window = closes[:i + 1]
        atr = calc_atr(highs[:i + 1], lows[:i + 1], closes[:i + 1])
        if not atr:
            i += 1
            continue

        # 次元1: トレンド（EMA整列）
        ema20 = calc_ema(window[-120:], 20)
        ema60 = calc_ema(window[-120:], 60)
        ema120 = calc_ema(window[-120:], 120)
        if not (ema20 and ema60 and ema120):
            i += 1
            continue
        long_trend = ema20 > ema60 > ema120
        short_trend = ema20 < ema60 < ema120
        if not (long_trend or short_trend):
            i += 1
            continue

        # 次元2: モメンタム（MACD + RSI）
        macd_line, signal_line = calc_macd(window)
        rsi = calc_rsi(window)
        if macd_line is None or rsi is None:
            i += 1
            continue
        long_mom = macd_line > signal_line and rsi > 50
        short_mom = macd_line < signal_line and rsi < 50

        # 次元3: プライスアクション（押し目/戻りからの転換）
        bar = bars[i]
        prev = bars[i - 1]
        is_bull = bar["close"] > bar["open"]
        is_bear = bar["close"] < bar["open"]
        # ロング: 直近で押した後の陽線転換（前足より安値切り上げ＋陽線）
        long_pa = is_bull and bar["low"] >= prev["low"] - atr * 0.2
        short_pa = is_bear and bar["high"] <= prev["high"] + atr * 0.2

        direction = None
        if long_trend and long_mom and long_pa:
            direction = "LONG"
        elif short_trend and short_mom and short_pa:
            direction = "SHORT"

        if direction:
            entry = bar["close"]
            # SLは直近5本のスイング安値/高値
            recent_lows = [b["low"] for b in bars[max(0, i - 5):i + 1]]
            recent_highs = [b["high"] for b in bars[max(0, i - 5):i + 1]]
            if direction == "LONG":
                sl = min(recent_lows) - atr * 0.3
                risk = entry - sl
                tp = entry + risk * rr
            else:
                sl = max(recent_highs) + atr * 0.3
                risk = sl - entry
                tp = entry - risk * rr
            if risk > 0:
                entries.append({
                    "index": i, "direction": direction,
                    "entry": entry, "sl": sl, "tp": tp,
                    "role": "三次元共鳴", "atr": atr,
                })
                i += 8
                continue
        i += 1

    return entries


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


def run_rr_comparison(price_data, pair, base_method="improveB",
                      rr_list=(1.0, 1.5, 2.0, 2.5, 3.0)):
    """
    最良方式（improveB）を固定し、RR（TP/SL比）を変えて比較。
    TPを遠くするほど決着に時間がかかるのでmax_holdを長めに。
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

    summaries = {}
    for rr in rr_list:
        entries = detect_entries(bars, base_method, pair, rr=rr)
        # TPが遠いほど時間がかかるのでmax_holdをRRに比例させる
        max_hold = int(20 * max(1.0, rr))
        results = evaluate_entries(bars, entries, max_hold=max_hold)
        label = f"RR1:{rr}"
        summaries[label] = summarize(results, label)
    return summaries


def run_strategy_comparison(price_data, pair, rr=2.0):
    """
    反発（逆張り）vs 三次元共鳴（順張り）を同じRRで比較。
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

    max_hold = int(20 * max(1.0, rr))
    summaries = {}

    # 反発（トレンドフィルター付き improveB）
    rebound = detect_entries(bars, "improveB", pair, rr=rr)
    rebound_results = evaluate_entries(bars, rebound, max_hold=max_hold)
    summaries["反発(improveB)"] = summarize(rebound_results, "反発(improveB)")

    # 反発の逆（同じエントリー地点で方向反転）
    reverse = detect_entries(bars, "improveB", pair, rr=rr, reverse=True)
    reverse_results = evaluate_entries(bars, reverse, max_hold=max_hold)
    summaries["反発の逆(reverse)"] = summarize(reverse_results, "反発の逆(reverse)")

    # 三次元共鳴（順張り）
    resonance = detect_resonance_entries(bars, pair, rr=rr)
    resonance_results = evaluate_entries(bars, resonance, max_hold=max_hold)
    summaries["三次元共鳴(順張り)"] = summarize(resonance_results, "三次元共鳴(順張り)")

    return summaries
