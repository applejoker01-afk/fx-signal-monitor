"""
exit_strategy_backtest.py
決済方式の比較バックテスト

同じエントリーシグナルに対して3つの決済方式を当てはめ、
どれが最も期待値・プロフィットファクターが高いかを検証する。

方式A: OCO固定      … TP1到達で全利確 / SL到達で損切り（両端固定）
方式B: トレーリング … 価格が伸びたらSLを追従（ATR×N）、反転で決済
方式C: ハイブリッド … TP1で半分利確 → 残りをトレール（SLはBEに移動）

評価指標（理論文書準拠）:
  勝率, プロフィットファクター(PF), 期待値, 最大ドローダウン, 平均RR
"""


def _simulate_oco(entry, direction, sl, tp1, future_prices):
    """
    方式A: OCO固定。TP1かSLのどちらかに先に到達した方で決済。
    future_prices: エントリー後の価格系列
    """
    is_long = direction == "LONG"
    for px in future_prices:
        if is_long:
            if px <= sl:
                return {"exit": sl, "reason": "SL", "pips": sl - entry}
            if px >= tp1:
                return {"exit": tp1, "reason": "TP", "pips": tp1 - entry}
        else:
            if px >= sl:
                return {"exit": sl, "reason": "SL", "pips": entry - sl}
            if px <= tp1:
                return {"exit": tp1, "reason": "TP", "pips": entry - tp1}
    # 期間内に未決済 → 最終価格で決済
    last = future_prices[-1] if future_prices else entry
    pips = (last - entry) if is_long else (entry - last)
    return {"exit": last, "reason": "EXPIRE", "pips": pips}


def _simulate_trailing(entry, direction, atr, future_prices, trail_mult=2.0):
    """
    方式B: トレーリングストップ。
    価格が有利に動いたらSLをtrail_mult×ATR下（ロング）に追従。
    反転してトレールSLに当たったら決済。
    """
    is_long = direction == "LONG"
    trail_dist = atr * trail_mult

    if is_long:
        peak = entry
        stop = entry - trail_dist
        for px in future_prices:
            if px > peak:
                peak = px
                stop = max(stop, peak - trail_dist)  # SLを引き上げ
            if px <= stop:
                return {"exit": stop, "reason": "TRAIL", "pips": stop - entry}
        last = future_prices[-1] if future_prices else entry
        return {"exit": last, "reason": "EXPIRE", "pips": last - entry}
    else:
        trough = entry
        stop = entry + trail_dist
        for px in future_prices:
            if px < trough:
                trough = px
                stop = min(stop, trough + trail_dist)
            if px >= stop:
                return {"exit": stop, "reason": "TRAIL", "pips": entry - stop}
        last = future_prices[-1] if future_prices else entry
        return {"exit": last, "reason": "EXPIRE", "pips": entry - last}


def _simulate_hybrid(entry, direction, sl, tp1, atr, future_prices, trail_mult=2.0):
    """
    方式C: ハイブリッド。
    TP1到達で半分利確 + SLをBE(建値)へ移動 → 残り半分をトレール。
    TP1前にSL到達なら全損。
    """
    is_long = direction == "LONG"
    trail_dist = atr * trail_mult
    half_locked = False
    realized = 0.0  # 確定済み損益（半分利確分）

    if is_long:
        stop = sl
        peak = entry
        for px in future_prices:
            if not half_locked:
                # 前半: TP1かSLか
                if px <= stop:
                    return {"exit": stop, "reason": "SL", "pips": stop - entry}
                if px >= tp1:
                    # 半分利確 + SLをBEへ
                    realized = (tp1 - entry) * 0.5
                    half_locked = True
                    stop = entry  # BE
                    peak = px
            else:
                # 後半: トレール
                if px > peak:
                    peak = px
                    stop = max(stop, peak - trail_dist)
                if px <= stop:
                    rest = (stop - entry) * 0.5
                    return {"exit": stop, "reason": "TRAIL", "pips": realized + rest}
        last = future_prices[-1] if future_prices else entry
        if half_locked:
            return {"exit": last, "reason": "EXPIRE", "pips": realized + (last - entry) * 0.5}
        return {"exit": last, "reason": "EXPIRE", "pips": last - entry}
    else:
        stop = sl
        trough = entry
        for px in future_prices:
            if not half_locked:
                if px >= stop:
                    return {"exit": stop, "reason": "SL", "pips": entry - stop}
                if px <= tp1:
                    realized = (entry - tp1) * 0.5
                    half_locked = True
                    stop = entry
                    trough = px
            else:
                if px < trough:
                    trough = px
                    stop = min(stop, trough + trail_dist)
                if px >= stop:
                    rest = (entry - stop) * 0.5
                    return {"exit": stop, "reason": "TRAIL", "pips": realized + rest}
        last = future_prices[-1] if future_prices else entry
        if half_locked:
            return {"exit": last, "reason": "EXPIRE", "pips": realized + (entry - last) * 0.5}
        return {"exit": last, "reason": "EXPIRE", "pips": entry - last}


def _generate_entries(pair, prices, compute_ta_fn, compute_fa_fn,
                       cb_rates, pair_api, lookback_days):
    """
    過去の各時点で★4相当のエントリーが出た箇所を抽出。
    （backtest.pyと同じ判定ロジック）
    """
    n = len(prices)
    if n < 60:
        return []
    start_idx = max(50, n - lookback_days)
    entries = []
    for i in range(start_idx, n - 1):  # 最後の1本は未来がないので除外
        hist = prices[:i + 1]
        cur = prices[i]
        ta = compute_ta_fn(cur, hist)
        fa = compute_fa_fn(pair, pair_api, cb_rates)
        ta_sign = 1 if ta["ta_score"] > 50 else (-1 if ta["ta_score"] < 50 else 0)
        fa_sign = 1 if fa["direction"] == "buy" else (-1 if fa["direction"] == "sell" else 0)
        agree = ta_sign == fa_sign and ta_sign != 0
        direction = None
        if agree and ta["ta_score"] >= 60 and fa["score"] >= 55:
            direction = "LONG" if fa_sign > 0 else "SHORT"
        elif agree and ta["ta_score"] <= 40 and fa["score"] <= 45:
            direction = "SHORT"
        if direction:
            atr = ta.get("atr") or cur * 0.005
            entries.append({"idx": i, "price": cur, "direction": direction, "atr": atr})
    return entries


def compare_exit_strategies(all_histories, compute_ta_fn, compute_fa_fn,
                            cb_rates, pair_api, lookback_days=200,
                            future_window=30):
    """
    全ペアでエントリーを抽出し、3方式の決済をシミュレートして比較。

    future_window: エントリー後、何本先まで決済機会を見るか
    """
    methods = {"OCO": [], "TRAIL": [], "HYBRID": []}

    for pair, prices in all_histories.items():
        if not prices or len(prices) < 60:
            continue
        entries = _generate_entries(pair, prices, compute_ta_fn, compute_fa_fn,
                                    cb_rates, pair_api, lookback_days)
        for e in entries:
            idx = e["idx"]
            entry = e["price"]
            direction = e["direction"]
            atr = e["atr"]
            future = prices[idx + 1: idx + 1 + future_window]
            if not future:
                continue

            # TP/SL設定（ATR×2.5基準）
            if direction == "LONG":
                sl = entry - atr * 2.5
                tp1 = entry + atr * 2.5
            else:
                sl = entry + atr * 2.5
                tp1 = entry - atr * 2.5

            methods["OCO"].append(_simulate_oco(entry, direction, sl, tp1, future))
            methods["TRAIL"].append(_simulate_trailing(entry, direction, atr, future))
            methods["HYBRID"].append(_simulate_hybrid(entry, direction, sl, tp1, atr, future))

    # 各方式の集計
    results = {}
    for name, trades in methods.items():
        results[name] = _summarize(trades)
    return results


def _summarize(trades):
    if not trades:
        return {"total": 0}
    total = len(trades)
    wins = [t for t in trades if t["pips"] > 0]
    losses = [t for t in trades if t["pips"] < 0]
    win_rate = round(len(wins) / total * 100, 1)

    gross_profit = sum(t["pips"] for t in wins)
    gross_loss = abs(sum(t["pips"] for t in losses))
    pf = round(gross_profit / gross_loss, 2) if gross_loss > 0 else 999.0

    avg_win = (gross_profit / len(wins)) if wins else 0
    avg_loss = (gross_loss / len(losses)) if losses else 0

    # 期待値 E = P_W×W - P_L×L
    p_w = len(wins) / total
    p_l = len(losses) / total
    expectancy = round(p_w * avg_win - p_l * avg_loss, 5)

    # 最大ドローダウン（累積pips曲線から）
    cum = 0
    peak = 0
    max_dd = 0
    for t in trades:
        cum += t["pips"]
        peak = max(peak, cum)
        max_dd = min(max_dd, cum - peak)

    total_pips = round(sum(t["pips"] for t in trades), 5)

    return {
        "total": total,
        "win_rate": win_rate,
        "wins": len(wins),
        "losses": len(losses),
        "profit_factor": pf,
        "avg_win": round(avg_win, 5),
        "avg_loss": round(avg_loss, 5),
        "expectancy": expectancy,
        "max_drawdown": round(max_dd, 5),
        "total_pips": total_pips,
    }


def pick_best_method(results):
    """期待値とPFから最良の決済方式を判定"""
    valid = {k: v for k, v in results.items() if v.get("total", 0) > 0}
    if not valid:
        return None
    # 期待値を主、PFを従に評価
    best = max(valid.items(), key=lambda x: (x[1]["expectancy"], x[1]["profit_factor"]))
    return {"method": best[0], "stats": best[1]}
