"""
trade_tracker.py
トレードのライフサイクル管理エンジン

1シグナル = 1トレードとして正確に追跡する。

状態遷移:
  ★3以下 → ★4に上昇 = エントリー（open_trades.jsonに記録）
  ★4以上を維持 = 保有中（重複カウントしない）
  決済条件 = TP/SL到達 or ★4を割る or 方向反転 → closed_trades.jsonlに記録

決済理由:
  TP3_HIT  : TP3到達（中長期目標達成・大勝ち）
  TP2_HIT  : TP2到達（勝ち）
  TP1_HIT  : TP1到達（小勝ち）
  SL_HIT   : ストップロス到達（負け）
  SIGNAL_LOST : ★2を割った（シグナルが明確に弱体化・中長期保持後の消滅）
  REVERSED : 方向が反転した（LONG→SHORT等・明確なトレンド転換）
"""

import json
import os
from datetime import datetime, timezone


OPEN_TRADES_FILE = "data/open_trades.json"
CLOSED_TRADES_FILE = "data/closed_trades.jsonl"
MAX_CLOSED_DAYS = 120  # 120日分の決済履歴を保持


# ============================================================
# ファイル読み書き
# ============================================================

def load_open_trades() -> dict:
    """保有中トレードを読み込む。{pair: trade_dict}"""
    if not os.path.exists(OPEN_TRADES_FILE):
        return {}
    try:
        with open(OPEN_TRADES_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_open_trades(open_trades: dict):
    os.makedirs("data", exist_ok=True)
    with open(OPEN_TRADES_FILE, "w", encoding="utf-8") as f:
        json.dump(open_trades, f, ensure_ascii=False, indent=2, default=str)


def append_closed_trade(trade: dict):
    os.makedirs("data", exist_ok=True)
    with open(CLOSED_TRADES_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps(trade, ensure_ascii=False, default=str) + "\n")


def load_closed_trades(days_back: int = None) -> list:
    """決済済みトレードを読み込む"""
    if not os.path.exists(CLOSED_TRADES_FILE):
        return []
    trades = []
    cutoff = None
    if days_back:
        from datetime import timedelta
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days_back)).isoformat()

    with open(CLOSED_TRADES_FILE, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                t = json.loads(line)
                if cutoff is None or t.get("exit_time", "") >= cutoff:
                    trades.append(t)
            except Exception:
                continue
    return trades


def prune_closed_trades():
    """120日より古い決済履歴を削除"""
    if not os.path.exists(CLOSED_TRADES_FILE):
        return
    from datetime import timedelta
    cutoff = (datetime.now(timezone.utc) - timedelta(days=MAX_CLOSED_DAYS)).isoformat()
    kept = []
    with open(CLOSED_TRADES_FILE, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                t = json.loads(line)
                if t.get("exit_time", "") >= cutoff:
                    kept.append(line)
            except Exception:
                continue
    with open(CLOSED_TRADES_FILE, "w", encoding="utf-8") as f:
        f.write("\n".join(kept) + ("\n" if kept else ""))


# ============================================================
# トレード判定ロジック
# ============================================================

def _hours_between(iso_start: str, dt_end: datetime) -> float:
    try:
        start = datetime.fromisoformat(iso_start)
        if start.tzinfo is None:
            start = start.replace(tzinfo=timezone.utc)
        return round((dt_end - start).total_seconds() / 3600, 1)
    except Exception:
        return 0.0


def check_exit_condition(trade: dict, current_price: float,
                         current_stars: int, current_direction: str) -> dict:
    """
    保有中トレードが決済条件を満たすか判定。

    Returns:
        None（継続）または決済情報dict
    """
    direction = trade["direction"]
    entry_price = trade["entry_price"]
    sl = trade.get("sl")
    tp1 = trade.get("tp1")
    tp2 = trade.get("tp2")
    tp3 = trade.get("tp3")

    exit_reason = None
    exit_price = current_price

    is_long = direction.endswith("LONG")

    # ── 価格ベースの決済判定（TP/SL）──
    if is_long:
        # ロング: 上昇でTP、下落でSL
        if sl is not None and current_price <= sl:
            exit_reason = "SL_HIT"; exit_price = sl
        elif tp3 is not None and current_price >= tp3:
            exit_reason = "TP3_HIT"; exit_price = tp3
        elif tp2 is not None and current_price >= tp2:
            exit_reason = "TP2_HIT"; exit_price = tp2
        elif tp1 is not None and current_price >= tp1:
            exit_reason = "TP1_HIT"; exit_price = tp1
    else:
        # ショート: 下落でTP、上昇でSL
        if sl is not None and current_price >= sl:
            exit_reason = "SL_HIT"; exit_price = sl
        elif tp3 is not None and current_price <= tp3:
            exit_reason = "TP3_HIT"; exit_price = tp3
        elif tp2 is not None and current_price <= tp2:
            exit_reason = "TP2_HIT"; exit_price = tp2
        elif tp1 is not None and current_price <= tp1:
            exit_reason = "TP1_HIT"; exit_price = tp1

    # ── シグナルベースの決済判定 ──
    if exit_reason is None:
        # 方向が反転した（★2以上の確信を伴う明確な反転のみ。
        # 中長期なので、弱い逆シグナルでは決済しない）
        if current_direction.endswith(("LONG", "SHORT")) and current_stars >= 2:
            cur_is_long = current_direction.endswith("LONG")
            if cur_is_long != is_long:
                exit_reason = "REVERSED"
                exit_price = current_price
        # ★が2を割った（シグナルが明確に弱体化・中長期なので多少の低下では決済しない）
        if exit_reason is None and current_stars < 2:
            exit_reason = "SIGNAL_LOST"
            exit_price = current_price

    if exit_reason is None:
        return None  # 継続保有

    # ── 損益計算 ──
    if is_long:
        pips = exit_price - entry_price
    else:
        pips = entry_price - exit_price

    # 勝敗判定
    if exit_reason in ("TP1_HIT", "TP2_HIT", "TP3_HIT"):
        result = "WIN"
    elif exit_reason == "SL_HIT":
        result = "LOSS"
    else:
        # SIGNAL_LOST / REVERSED は損益で判定
        result = "WIN" if pips > 0 else ("LOSS" if pips < 0 else "EVEN")

    return {
        "exit_reason": exit_reason,
        "exit_price": round(exit_price, 5),
        "pips": round(pips, 5),
        "result": result,
    }


def update_trades(results: list, now: datetime) -> dict:
    """
    毎時スキャン時に呼ぶメイン関数。

    1. 既存の保有トレードの決済判定
    2. 新規エントリーの記録
    3. ファイル保存

    Returns:
        {
          "newly_opened": [...],   # 今回新規エントリーしたトレード
          "newly_closed": [...],   # 今回決済したトレード
          "still_open": int,       # 現在保有中の件数
        }
    """
    open_trades = load_open_trades()
    newly_opened = []
    newly_closed = []

    # 現在のシグナルをpair→result辞書に
    current_by_pair = {r["pair"]: r for r in results}

    # ── 1. 既存トレードの決済判定 ──
    pairs_to_close = []
    for pair, trade in open_trades.items():
        cur = current_by_pair.get(pair)
        if not cur:
            # この通貨ペアが評価対象から消えた（データ欠損）→ そのまま保有継続
            continue

        exit_info = check_exit_condition(
            trade,
            cur.get("price", trade["entry_price"]),
            cur.get("stars", 0),
            cur.get("direction", ""),
        )

        if exit_info:
            closed = {
                **trade,
                "exit_time": now.isoformat(),
                "exit_date": now.strftime("%Y-%m-%d"),
                "hold_hours": _hours_between(trade["entry_time"], now),
                **exit_info,
            }
            append_closed_trade(closed)
            newly_closed.append(closed)
            pairs_to_close.append(pair)

    # 決済したトレードをオープンリストから除去
    for pair in pairs_to_close:
        del open_trades[pair]

    # ── 2. 新規エントリーの記録 ──
    # 同一サイクルで決済したペアは再エントリーしない（決済と同時の即エントリー防止）
    closed_this_cycle = set(pairs_to_close)
    for r in results:
        pair = r["pair"]
        stars = r.get("stars", 0)
        direction = r.get("direction", "")

        # ★4以上 かつ 方向が明確 かつ まだ保有していない かつ 今サイクルで決済していない
        if (stars >= 4
                and direction.endswith(("LONG", "SHORT"))
                and pair not in open_trades
                and pair not in closed_this_cycle):

            staged = r.get("staged_tp", {})
            trade = {
                "pair": pair,
                "entry_time": now.isoformat(),
                "entry_date": now.strftime("%Y-%m-%d"),
                "entry_price": r.get("price"),
                "direction": direction,
                "initial_stars": stars,
                "sl": staged.get("sl"),
                "tp1": staged.get("tp1"),
                "tp2": staged.get("tp2"),
                "tp3": staged.get("tp3"),
                "ta_score": r.get("ta_score"),
                "fa_score": r.get("fa_score"),
                "fa_rate_diff": r.get("fa_rate_diff"),
                "regime": r.get("volatility_regime", {}).get("regime"),
            }
            open_trades[pair] = trade
            newly_opened.append(trade)

    # ── 3. 保存 ──
    save_open_trades(open_trades)
    prune_closed_trades()

    return {
        "newly_opened": newly_opened,
        "newly_closed": newly_closed,
        "still_open": len(open_trades),
    }


# ============================================================
# 集計
# ============================================================

def calc_trade_stats(days_back: int = 7) -> dict:
    """
    決済済みトレードから統計を計算。
    週次レポート用。
    """
    trades = load_closed_trades(days_back=days_back)
    if not trades:
        return {}

    total = len(trades)
    wins = sum(1 for t in trades if t.get("result") == "WIN")
    losses = sum(1 for t in trades if t.get("result") == "LOSS")
    evens = sum(1 for t in trades if t.get("result") == "EVEN")

    win_rate = round(wins / total * 100, 1) if total else 0

    # 決済理由別カウント
    reason_counts = {}
    for t in trades:
        reason = t.get("exit_reason", "?")
        reason_counts[reason] = reason_counts.get(reason, 0) + 1

    # ペア別成績
    pair_stats = {}
    for t in trades:
        p = t.get("pair", "?")
        if p not in pair_stats:
            pair_stats[p] = {"total": 0, "wins": 0, "total_pips": 0.0}
        pair_stats[p]["total"] += 1
        if t.get("result") == "WIN":
            pair_stats[p]["wins"] += 1
        pair_stats[p]["total_pips"] += t.get("pips", 0) or 0

    # 平均保有時間
    hold_hours = [t.get("hold_hours", 0) for t in trades if t.get("hold_hours")]
    avg_hold = round(sum(hold_hours) / len(hold_hours), 1) if hold_hours else 0

    # 最大の勝ち・負け
    best = max(trades, key=lambda t: t.get("pips", 0)) if trades else None
    worst = min(trades, key=lambda t: t.get("pips", 0)) if trades else None

    return {
        "total_trades": total,
        "wins": wins,
        "losses": losses,
        "evens": evens,
        "win_rate": win_rate,
        "reason_counts": reason_counts,
        "pair_stats": pair_stats,
        "avg_hold_hours": avg_hold,
        "best_trade": best,
        "worst_trade": worst,
    }
