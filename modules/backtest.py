"""
backtest.py
バックテスト機能（機能⑨）

過去の価格データを使って「もしこの戦略を使っていたら」を検証する。
closed_trades.jsonl が貯まるのを待たずに、即座に過去成績が分かる。

シグナル判定ロジックは signal_scanner.py と同一のものを使い、
過去の各日付時点で★4以上だったらエントリーしたとみなして
TP/SL到達をシミュレーションする。
"""

import json
from datetime import datetime, timedelta, timezone


def run_backtest(
    pair: str,
    full_prices: list,
    compute_ta_score_fn,
    compute_fa_score_fn,
    cb_rates: dict,
    pair_api: dict,
    atr_multipliers: tuple = (2.5, 2.5, 5.0, 8.5),
    lookback_days: int = 180,
) -> dict:
    """
    1通貨ペアのバックテストを実行。

    Args:
        pair: 通貨ペア
        full_prices: 全価格履歴（280日分など）
        compute_ta_score_fn: TAスコア計算関数（signal_scannerから渡す）
        compute_fa_score_fn: FAスコア計算関数
        cb_rates: 中央銀行金利
        pair_api: ペア定義
        atr_multipliers: (SL, TP1, TP2, TP3) のATR乗数
        lookback_days: 検証する日数

    Returns:
        {
          "pair": "ZARJPY",
          "trades": [...],
          "total": 25, "wins": 15, "losses": 10,
          "win_rate": 60.0,
          "tp1_hits": 10, "tp2_hits": 4, "tp3_hits": 1, "sl_hits": 10,
          "profit_factor": 1.85,
          "total_pips": 12.5,
        }
    """
    sl_mult, tp1_mult, tp2_mult, tp3_mult = atr_multipliers

    n = len(full_prices)
    if n < 60:
        return {"pair": pair, "trades": [], "total": 0, "error": "履歴不足"}

    # 検証開始位置（最低でも50日分の履歴が必要なので、それ以降から）
    start_idx = max(50, n - lookback_days)

    trades = []
    open_trade = None  # 同時に1ポジションのみ（バックテストの簡略化）

    for i in range(start_idx, n):
        # i日目時点の価格履歴（未来を見ない）
        hist = full_prices[:i + 1]
        current_price = full_prices[i]

        # ── 保有中トレードの決済判定 ──
        if open_trade:
            direction = open_trade["direction"]
            entry = open_trade["entry_price"]
            sl = open_trade["sl"]
            tp1 = open_trade["tp1"]
            tp2 = open_trade["tp2"]
            tp3 = open_trade["tp3"]
            is_long = direction == "LONG"

            exit_reason = None
            exit_price = current_price

            if is_long:
                if current_price <= sl:
                    exit_reason = "SL_HIT"; exit_price = sl
                elif current_price >= tp3:
                    exit_reason = "TP3_HIT"; exit_price = tp3
                elif current_price >= tp2:
                    exit_reason = "TP2_HIT"; exit_price = tp2
                elif current_price >= tp1:
                    exit_reason = "TP1_HIT"; exit_price = tp1
            else:
                if current_price >= sl:
                    exit_reason = "SL_HIT"; exit_price = sl
                elif current_price <= tp3:
                    exit_reason = "TP3_HIT"; exit_price = tp3
                elif current_price <= tp2:
                    exit_reason = "TP2_HIT"; exit_price = tp2
                elif current_price <= tp1:
                    exit_reason = "TP1_HIT"; exit_price = tp1

            if exit_reason:
                pips = (exit_price - entry) if is_long else (entry - exit_price)
                trades.append({
                    "entry_idx": open_trade["entry_idx"],
                    "exit_idx": i,
                    "direction": direction,
                    "entry_price": round(entry, 5),
                    "exit_price": round(exit_price, 5),
                    "exit_reason": exit_reason,
                    "pips": round(pips, 5),
                    "result": "WIN" if exit_reason.startswith("TP") else "LOSS",
                    "hold_days": i - open_trade["entry_idx"],
                })
                open_trade = None

        # ── 新規エントリー判定（保有していない時のみ）──
        if open_trade is None:
            ta = compute_ta_score_fn(current_price, hist)
            fa = compute_fa_score_fn(pair, pair_api, cb_rates)

            ta_sign = 1 if ta["ta_score"] > 50 else (-1 if ta["ta_score"] < 50 else 0)
            fa_sign = 1 if fa["direction"] == "buy" else (-1 if fa["direction"] == "sell" else 0)
            agree = ta_sign == fa_sign and ta_sign != 0

            # ★4以上の条件
            entry_dir = None
            if agree and ta["ta_score"] >= 60 and fa["score"] >= 55:
                entry_dir = "LONG" if fa_sign > 0 else "SHORT"
            elif agree and ta["ta_score"] <= 40 and fa["score"] <= 45:
                entry_dir = "SHORT"

            if entry_dir:
                atr = ta.get("atr") or (current_price * 0.005)
                if entry_dir == "LONG":
                    sl = current_price - atr * sl_mult
                    tp1 = current_price + atr * tp1_mult
                    tp2 = current_price + atr * tp2_mult
                    tp3 = current_price + atr * tp3_mult
                else:
                    sl = current_price + atr * sl_mult
                    tp1 = current_price - atr * tp1_mult
                    tp2 = current_price - atr * tp2_mult
                    tp3 = current_price - atr * tp3_mult

                open_trade = {
                    "entry_idx": i,
                    "entry_price": current_price,
                    "direction": entry_dir,
                    "sl": sl, "tp1": tp1, "tp2": tp2, "tp3": tp3,
                }

    # ── 集計 ──
    return _summarize_backtest(pair, trades)


def _summarize_backtest(pair: str, trades: list) -> dict:
    if not trades:
        return {"pair": pair, "trades": [], "total": 0, "win_rate": 0}

    total = len(trades)
    wins = sum(1 for t in trades if t["result"] == "WIN")
    losses = total - wins

    tp1_hits = sum(1 for t in trades if t["exit_reason"] == "TP1_HIT")
    tp2_hits = sum(1 for t in trades if t["exit_reason"] == "TP2_HIT")
    tp3_hits = sum(1 for t in trades if t["exit_reason"] == "TP3_HIT")
    sl_hits = sum(1 for t in trades if t["exit_reason"] == "SL_HIT")

    gross_profit = sum(t["pips"] for t in trades if t["pips"] > 0)
    gross_loss = abs(sum(t["pips"] for t in trades if t["pips"] < 0))
    profit_factor = round(gross_profit / gross_loss, 2) if gross_loss > 0 else 999.0

    total_pips = round(sum(t["pips"] for t in trades), 4)
    avg_hold = round(sum(t["hold_days"] for t in trades) / total, 1)

    return {
        "pair": pair,
        "trades": trades,
        "total": total,
        "wins": wins,
        "losses": losses,
        "win_rate": round(wins / total * 100, 1),
        "tp1_hits": tp1_hits,
        "tp2_hits": tp2_hits,
        "tp3_hits": tp3_hits,
        "sl_hits": sl_hits,
        "profit_factor": profit_factor,
        "total_pips": total_pips,
        "avg_hold_days": avg_hold,
    }


def run_full_backtest(
    all_histories: dict,
    compute_ta_score_fn,
    compute_fa_score_fn,
    cb_rates: dict,
    pair_api: dict,
    lookback_days: int = 180,
    atr_multipliers: tuple = (2.5, 2.5, 5.0, 8.5),
) -> dict:
    """
    全通貨ペアのバックテストを実行して総合結果を返す。

    Args:
        all_histories: {pair: [prices...]}

    Returns:
        {
          "by_pair": {pair: backtest_result},
          "overall": {...},
          "best_pairs": [...],
          "worst_pairs": [...],
        }
    """
    by_pair = {}
    for pair, prices in all_histories.items():
        if prices and len(prices) >= 60:
            result = run_backtest(
                pair, prices, compute_ta_score_fn, compute_fa_score_fn,
                cb_rates, pair_api, lookback_days=lookback_days,
                atr_multipliers=atr_multipliers
            )
            if result.get("total", 0) > 0:
                by_pair[pair] = result

    if not by_pair:
        return {"by_pair": {}, "overall": {}, "best_pairs": [], "worst_pairs": []}

    # 全体集計
    all_total = sum(r["total"] for r in by_pair.values())
    all_wins = sum(r["wins"] for r in by_pair.values())
    all_pips = sum(r["total_pips"] for r in by_pair.values())
    all_tp1 = sum(r["tp1_hits"] for r in by_pair.values())
    all_tp2 = sum(r["tp2_hits"] for r in by_pair.values())
    all_tp3 = sum(r["tp3_hits"] for r in by_pair.values())
    all_sl = sum(r["sl_hits"] for r in by_pair.values())

    gross_p = sum(t["pips"] for r in by_pair.values() for t in r["trades"] if t["pips"] > 0)
    gross_l = abs(sum(t["pips"] for r in by_pair.values() for t in r["trades"] if t["pips"] < 0))
    overall_pf = round(gross_p / gross_l, 2) if gross_l > 0 else 999.0

    overall = {
        "total": all_total,
        "wins": all_wins,
        "losses": all_total - all_wins,
        "win_rate": round(all_wins / all_total * 100, 1) if all_total else 0,
        "tp1_hits": all_tp1, "tp2_hits": all_tp2, "tp3_hits": all_tp3, "sl_hits": all_sl,
        "total_pips": round(all_pips, 4),
        "profit_factor": overall_pf,
        "lookback_days": lookback_days,
    }

    # ペア別ランキング（勝率順・最低5トレード）
    qualified = [r for r in by_pair.values() if r["total"] >= 5]
    ranked = sorted(qualified, key=lambda r: -r["win_rate"])
    best_pairs = [{"pair": r["pair"], "win_rate": r["win_rate"], "total": r["total"]}
                  for r in ranked[:5]]
    worst_pairs = [{"pair": r["pair"], "win_rate": r["win_rate"], "total": r["total"]}
                   for r in ranked[-5:]][::-1]

    return {
        "by_pair": by_pair,
        "overall": overall,
        "best_pairs": best_pairs,
        "worst_pairs": worst_pairs,
    }
