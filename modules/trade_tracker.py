"""
trade_tracker.py
トレードのライフサイクル管理エンジン

1シグナル = 1トレードとして正確に追跡する。

状態遷移:
  ★3以下 → ★4に上昇 = エントリー（open_trades.jsonに記録）
  ★4以上を維持 = 保有中（重複カウントしない）
  決済条件 = TP/SL到達 or ★4を割る or 方向反転 → closed_trades.jsonlに記録

決済理由（2026-06-10刷新：単一TP + トレーリング戦略）:
  TP_HIT      : TP到達（旧TP1相当・ほぼ確実に到達する利確水準）
                到達後はトレーリング有効化、SLをBE+0.5Rへ移動
  TRAIL_HIT   : トレーリングストップ到達（トレンド継続後の利益確定）
  BE_HIT      : TP到達後の戻りでBE+0.5Rに到達（小利確保）
  SL_HIT      : 初期SLに到達（負け）
  SIGNAL_LOST : ★2を割った（シグナルが明確に弱体化・中長期保持後の消滅）
  REVERSED    : 方向が反転した（LONG→SHORT等・明確なトレンド転換）

  ── 後方互換 ──
  TP1_HIT, TP2_HIT, TP3_HIT は既存のクローズドトレード履歴に残る可能性あり
  新規トレードは TP_HIT / TRAIL_HIT / BE_HIT を使用
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
    保有中トレードが決済条件を満たすか判定（2026-06-10: 単一TP+トレーリング対応）。

    新戦略:
    - Phase 1（TP未到達）: 初期SL or 単一TP の判定
    - Phase 2（TP到達後）: トレーリングストップ or BE+0.5R の判定

    後方互換:
    - 既存open_tradesに tp2/tp3 がある場合は従来通り（移行期）

    Returns:
        - None: 保有継続
        - dict (with "_state_update"): SL移動・トレーリング更新（保有継続だが状態変化）
        - dict (with "exit_reason"): 決済確定
    """
    direction = trade["direction"]
    entry_price = trade["entry_price"]
    sl = trade.get("sl")
    tp = trade.get("tp") or trade.get("tp1")  # 新旧両対応

    # トレーリング関連の状態
    tp_hit_flag = trade.get("tp_hit", False)
    trail_active = trade.get("trail_active", False)
    trail_distance = trade.get("trail_distance", 0)
    extreme_price = trade.get("extreme_price")  # トレーリング用の最高値/最低値

    exit_reason = None
    exit_price = current_price
    state_update = None  # 保有継続だが状態が更新される場合

    is_long = direction.endswith("LONG")

    # ── Phase 1: TP未到達のフェーズ ──
    if not tp_hit_flag:
        if is_long:
            # ロング
            if sl is not None and current_price <= sl:
                exit_reason = "SL_HIT"; exit_price = sl
            elif tp is not None and current_price >= tp:
                # TP到達 → Phase 2へ移行（決済しない）
                tp_hit_flag = True
                trail_active = True
                extreme_price = current_price
                # SLを BE+0.5R へ移動
                new_sl = trade.get("be_target_after_tp", entry_price)
                state_update = {
                    "tp_hit": True,
                    "tp_hit_time": None,  # 後で設定
                    "trail_active": True,
                    "trail_distance": trade.get("trail_distance", 0),
                    "extreme_price": extreme_price,
                    "sl": new_sl,  # SL移動
                    "initial_sl": trade.get("initial_sl", sl),  # 元のSLを保持
                }
        else:
            # ショート
            if sl is not None and current_price >= sl:
                exit_reason = "SL_HIT"; exit_price = sl
            elif tp is not None and current_price <= tp:
                tp_hit_flag = True
                trail_active = True
                extreme_price = current_price
                new_sl = trade.get("be_target_after_tp", entry_price)
                state_update = {
                    "tp_hit": True,
                    "trail_active": True,
                    "trail_distance": trade.get("trail_distance", 0),
                    "extreme_price": extreme_price,
                    "sl": new_sl,
                    "initial_sl": trade.get("initial_sl", sl),
                }

    # ── Phase 2: TP到達後（トレーリング中）──
    elif trail_active and trail_distance > 0:
        # 最高値/最低値の更新
        if is_long:
            if extreme_price is None or current_price > extreme_price:
                extreme_price = current_price
                state_update = {"extreme_price": extreme_price}
            # トレーリングストップ価格 = 最高値 - trail_distance
            trail_stop = extreme_price - trail_distance
            # BE+0.5R よりトレーリングストップが上なら、SLをトレールへ
            be_target = trade.get("be_target_after_tp", entry_price)
            effective_sl = max(trail_stop, be_target)
            if current_price <= effective_sl:
                # トレーリングストップ or BE到達
                if trail_stop > be_target:
                    exit_reason = "TRAIL_HIT"
                else:
                    exit_reason = "BE_HIT"
                exit_price = effective_sl
            else:
                if state_update is None:
                    state_update = {}
                state_update["sl"] = effective_sl
        else:
            # ショート
            if extreme_price is None or current_price < extreme_price:
                extreme_price = current_price
                state_update = {"extreme_price": extreme_price}
            trail_stop = extreme_price + trail_distance
            be_target = trade.get("be_target_after_tp", entry_price)
            effective_sl = min(trail_stop, be_target)
            if current_price >= effective_sl:
                if trail_stop < be_target:
                    exit_reason = "TRAIL_HIT"
                else:
                    exit_reason = "BE_HIT"
                exit_price = effective_sl
            else:
                if state_update is None:
                    state_update = {}
                state_update["sl"] = effective_sl

    # ── シグナルベースの決済判定（Phase問わず） ──
    if exit_reason is None:
        if current_direction.endswith(("LONG", "SHORT")) and current_stars >= 2:
            cur_is_long = current_direction.endswith("LONG")
            if cur_is_long != is_long:
                exit_reason = "REVERSED"
                exit_price = current_price
        if exit_reason is None and current_stars < 2:
            exit_reason = "SIGNAL_LOST"
            exit_price = current_price

    if exit_reason is None:
        # 状態更新のみ（継続保有）
        if state_update:
            return {"_state_update": state_update}
        return None

    # ── 損益計算 ──
    if is_long:
        pips = exit_price - entry_price
    else:
        pips = entry_price - exit_price

    # 勝敗判定
    if exit_reason in ("TP_HIT", "TRAIL_HIT", "TP1_HIT", "TP2_HIT", "TP3_HIT"):
        result = "WIN"
    elif exit_reason == "BE_HIT":
        # BE+0.5R到達は小利確保（微益）
        result = "WIN" if pips > 0 else "EVEN"
    elif exit_reason == "SL_HIT":
        result = "LOSS"
    else:
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
    state_changes = []  # トレーリング状態変化（保有継続だが状態が更新された）

    # 現在のシグナルをpair→result辞書に
    current_by_pair = {r["pair"]: r for r in results}

    # ── 1. 既存トレードの決済判定 ──
    pairs_to_close = []
    for pair, trade in open_trades.items():
        cur = current_by_pair.get(pair)
        if not cur:
            # この通貨ペアが評価対象から消えた（データ欠損）→ そのまま保有継続
            continue

        current_price = cur.get("price", trade["entry_price"])
        exit_info = check_exit_condition(
            trade,
            current_price,
            cur.get("stars", 0),
            cur.get("direction", ""),
        )

        # 現在価格を保存（保有ポジション表示用）
        trade["current_price"] = current_price
        trade["current_stars"] = cur.get("stars", 0)

        if exit_info is None:
            continue

        # ── 状態更新のみ（決済しない）──
        if "_state_update" in exit_info:
            state_update = exit_info["_state_update"]
            # tp_hit_time を必要なら設定
            if state_update.get("tp_hit") and "tp_hit_time" in state_update and state_update["tp_hit_time"] is None:
                state_update["tp_hit_time"] = now.isoformat()
            trade.update(state_update)
            state_changes.append({
                "pair": pair,
                "trade": trade,
                "update": state_update,
            })
            continue

        # ── 決済 ──
        if "exit_reason" in exit_info:
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
            entry_price = r.get("price")
            initial_sl = staged.get("sl")
            trade = {
                "pair": pair,
                "entry_time": now.isoformat(),
                "entry_date": now.strftime("%Y-%m-%d"),
                "entry_price": entry_price,
                "direction": direction,
                "initial_stars": stars,
                "sl": initial_sl,
                "initial_sl": initial_sl,  # トレーリング後も初期SLを保持
                # ── 新方式: 単一TP + トレーリング ──
                "tp": staged.get("tp") or staged.get("tp1"),  # 新旧両対応
                "trail_distance": staged.get("trail_distance", 0),
                "trail_atr_mult": staged.get("trail_atr_mult", 3.0),
                "be_target_after_tp": staged.get("be_target_after_tp", entry_price),
                "max_target": staged.get("max_target") or staged.get("tp3"),  # 参考
                "tp_mode": staged.get("tp_mode", "single_with_trail"),
                # ── 状態フラグ（初期値）──
                "tp_hit": False,
                "trail_active": False,
                "extreme_price": entry_price,  # トレーリング基準
                # ── 後方互換: 既存JSONとの整合性 ──
                "tp1": staged.get("tp1") or staged.get("tp"),
                "tp2": staged.get("tp2"),
                "tp3": staged.get("tp3"),
                # ── メタデータ ──
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
        "state_changes": state_changes,
        "open_trades": open_trades,  # 保有ポジション一覧（Discord通知用）
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
