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
  SIGNAL_LOST : 72h以上保有かつ含み損かつ★3未満（2026-06-22: 条件厳格化）
  REVERSED    : 方向が反転した（LONG→SHORT等・明確なトレンド転換）

  ── 後方互換 ──
  TP1_HIT, TP2_HIT, TP3_HIT は既存のクローズドトレード履歴に残る可能性あり
  新規トレードは TP_HIT / TRAIL_HIT / BE_HIT を使用
"""

import json
import os
from datetime import datetime, timezone

from modules.position_sizing import calc_position_size, pnl_to_jpy, record_trade_pnl
from modules.advanced_analytics import calc_correlated_exposure_multiplier


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


# KRWJPY等、証券会社の建値慣行で100単位表示するペアの生スケール補正。
# signal_scanner.py の DISPLAY_SCALE と同じ内容を局所複製（モジュール循環回避）。
_DISPLAY_SCALE = {"KRWJPY": 100.0}


def _pair_decimals(pair: str) -> int:
    """JPYクロス=3、それ以外=6。signal_scanner.pair_decimals と同規約。

    _DISPLAY_SCALEを持つペアは丸め精度を上乗せする
    （2026-07-22発見: KRWJPY等の小さい生値がゼロに潰れ、実運用のP&L集計が
    不正確になるバグの修正）。
    """
    base = 3 if pair and pair.upper().endswith("JPY") else 6
    scale = _DISPLAY_SCALE.get(pair.upper(), 1.0) if pair else 1.0
    if scale and scale != 1.0:
        base += len(str(int(scale))) - 1
    return base


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
        # ── SIGNAL_LOST（2026-06-22 Phase1出口改善: 条件を厳格化）──
        # 変更前: current_stars < 2 で即時SIGNAL_LOST（85.5%の出口がここで発生）
        # 変更後: 72h以上保有 かつ 含み損 かつ ★3未満 の場合のみ
        # 根拠: 567,000バックテスト研究でMoving Average Exit（＝SIGNAL_LOST）は最下位
        #       autoresearch: wiki/finance/fx-exit-strategy-fundamentals.md
        if exit_reason is None:
            hold_h = _hours_between(trade.get("entry_time", ""), datetime.now(timezone.utc))
            current_pips = (
                (current_price - entry_price) if is_long
                else (entry_price - current_price)
            )
            if hold_h > 72 and current_pips < 0 and current_stars < 3:
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

    _d = _pair_decimals(trade.get("pair", ""))
    return {
        "exit_reason": exit_reason,
        "exit_price": round(exit_price, _d),
        "pips": round(pips, _d),
        "result": result,
    }


def update_trades(results: list, now: datetime,
                   pair_api: dict = None, latest_pairs: dict = None,
                   entry_mode: str = "market") -> dict:
    """
    毎時スキャン時に呼ぶメイン関数。

    1. 既存の保有トレードの決済判定
    2. 新規エントリーの記録
    3. ファイル保存

    entry_mode="limit" の場合、新規エントリーの即時オープンは行わない
    （modules/pending_orders.py 経由の指値待機フローに委ねる。2026-07-20追加）。

    pair_api / latest_pairs を渡すと、2026-07-20追加のシミュレーション口座
    （data/virtual_account.json）と連動したポジションサイジングを行う:
      - 新規エントリー時: 仮想残高・リスク許容度・SL値幅から推奨ロット数(units)を
        計算しtradeに記録する。
      - 決済時: units × 損益(円換算)をvirtual_accountのcurrent_balanceに反映する。
    どちらも省略した場合はポジションサイジングを行わない（後方互換）。

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

        # 現在価格を保存（保有ポジション表示用） — ペア別精度で丸める
        _d = _pair_decimals(pair)
        trade["current_price"] = round(current_price, _d) if current_price is not None else current_price
        trade["current_stars"] = cur.get("stars", 0)

        if exit_info is None:
            continue

        # ── 状態更新のみ（決済しない）──
        if "_state_update" in exit_info:
            state_update = exit_info["_state_update"]
            # tp_hit_time を必要なら設定
            if state_update.get("tp_hit") and "tp_hit_time" in state_update and state_update["tp_hit_time"] is None:
                state_update["tp_hit_time"] = now.isoformat()
            # 価格スケール値をペア別精度で丸める（Discord/JSON 表示の桁ズレ防止）
            for _k in ("sl", "extreme_price", "trail_distance"):
                if state_update.get(_k) is not None:
                    state_update[_k] = round(state_update[_k], _d)
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
            # シミュレーション口座残高へ反映（2026-07-20追加）
            # units（新規エントリー時にサイジング済み）が無いトレード（機能追加前の
            # 既存ポジション等）はスキップし、残高は変動させない。
            units = trade.get("units")
            if units and pair_api is not None and latest_pairs is not None:
                pnl_per_unit = pnl_to_jpy(pair, pair_api, exit_info.get("pips", 0), latest_pairs)
                if pnl_per_unit is not None:
                    pnl_jpy = round(units * pnl_per_unit, 0)
                    closed["pnl_jpy"] = pnl_jpy
                    record_trade_pnl(pnl_jpy)
            append_closed_trade(closed)
            newly_closed.append(closed)
            pairs_to_close.append(pair)

    # 決済したトレードをオープンリストから除去
    for pair in pairs_to_close:
        del open_trades[pair]

    # ── 2. 新規エントリーの記録 ──
    # 同一サイクルで決済したペアは再エントリーしない（決済と同時の即エントリー防止）
    #
    # entry_mode（2026-07-20追加）:
    #   "market"（デフォルト・毎時スキャン）: 従来通り、★4以上を検出したその場で
    #     現在値エントリーとして即座にopen_tradesへ記録する。
    #   "limit"（1日3回の指値スキャン専用）: ここでは即座にオープンせず、
    #     modules/pending_orders.py 側で押し目の指値注文として登録する
    #     （signal_scanner.py の呼び出し元が別途処理する）。
    closed_this_cycle = set(pairs_to_close)
    for r in results:
        pair = r["pair"]
        stars = r.get("stars", 0)
        direction = r.get("direction", "")

        if entry_mode == "limit":
            continue

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

            # ポジションサイジング（2026-07-20追加、シミュレーション口座連動）
            # open_trades（この時点でのメモリ上の状態）を明示的に渡すことで、
            # 同一スキャンサイクル内で複数ペアが同時に新規シグナルを出した場合でも、
            # 先に処理されたペアの証拠金を後続ペアの余力計算に正しく反映させる。
            if pair_api is not None and latest_pairs is not None and initial_sl is not None:
                # 相関エクスポージャー（2026-07-21追加）: 既存保有ポジションと
                # 同方向の通貨エクスポージャーが重複していればロットを圧縮する。
                exp_mult, exp_note = calc_correlated_exposure_multiplier(
                    pair, direction, open_trades, pair_api
                )
                if exp_note:
                    print(f"  [SIZING] {pair}: {exp_note}")
                sizing = calc_position_size(
                    pair, entry_price, initial_sl, pair_api, latest_pairs,
                    open_trades=open_trades, exposure_multiplier=exp_mult,
                )
                trade["position_sizing"] = sizing
                if sizing.get("tradable"):
                    trade["units"] = sizing["units"]

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
# 指値待機注文の約定処理（2026-07-20追加）
# ============================================================

def open_trade_from_pending_fill(trade: dict, pair_api: dict = None,
                                  latest_pairs: dict = None) -> dict:
    """
    modules.pending_orders.pending_order_to_trade() が生成したtradeレコードを
    open_trades.jsonへ正式追加する（ポジションサイジング込み）。
    毎時スキャン側で pending_orders.check_pending_fills() が約定を検知した際に呼ぶ。

    通常の新規エントリー（update_trades内）と同じサイジングロジックを使うが、
    こちらは1件ずつ即時に呼ばれる想定のため、呼び出し側で最新のopen_tradesを
    渡すこと（同一サイクル内で複数約定した場合の証拠金二重計上を防ぐため）。
    """
    open_trades = load_open_trades()
    pair = trade["pair"]

    if pair_api is not None and latest_pairs is not None and trade.get("sl") is not None:
        exp_mult, exp_note = calc_correlated_exposure_multiplier(
            pair, trade.get("direction", ""), open_trades, pair_api
        )
        if exp_note:
            print(f"  [SIZING] {pair}: {exp_note}")
        sizing = calc_position_size(
            pair, trade["entry_price"], trade["sl"], pair_api, latest_pairs,
            open_trades=open_trades, exposure_multiplier=exp_mult,
        )
        trade["position_sizing"] = sizing
        if sizing.get("tradable"):
            trade["units"] = sizing["units"]

    open_trades[pair] = trade
    save_open_trades(open_trades)
    return trade


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
