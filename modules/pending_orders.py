"""
pending_orders.py
指値待機シグナル（2026-07-20追加）

背景: 毎時のリアルタイム通知だと、仕事中・就寝中にシグナルへ気づけないことがある。
1日3回（07:00/13:00/21:00 JST）の定期スキャンで、新規★4以上シグナルを
「今すぐ成行」ではなく「押し目を待つ指値注文」として提示し、ユーザーがその場で
証券会社へ指値注文を出しておけば、価格がその水準に届いたときに自動約定する運用を想定。

フロー:
  1. 3回/日のスキャン(ENTRY_MODE=limit)で、新規★4以上シグナルを
     data/pending_orders.json に登録する（現在値からATR×0.3の押し目を指値価格とする）。
  2. 毎時スキャン(通常のENTRY_MODE=market)で pending_orders.json をチェックし、
     現在値が指値に到達していれば「約定」とみなし open_trades.json へ正式追加する。
  3. 次回の指値スキャン時刻までに約定しなければ失効・削除する（陳腐化した指値の放置防止）。

SL/TPは指値注文作成時点（現在値ベース）で計算した絶対価格をそのまま保持し、
約定時のentry_priceだけを実際の指値価格に差し替える。押し目分だけ実質的な
リスクリワードが改善する（エントリーが有利になった分、SL距離は狭く・TP距離は広くなる）。
"""

import json
import os
from datetime import datetime, timedelta, timezone

PENDING_FILE = "data/pending_orders.json"

# 指値スキャンのスケジュール（JST時刻）。GitHub Actions側のcronと一致させること。
SCAN_HOURS_JST = [7, 13, 21]

# 押し目の深さ（ATR倍率）。LONGは現在値より安く、SHORTは現在値より高く指値を置く。
DEFAULT_PULLBACK_ATR_MULT = 0.3


def load_pending_orders() -> dict:
    if not os.path.exists(PENDING_FILE):
        return {}
    try:
        with open(PENDING_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_pending_orders(pending: dict):
    os.makedirs("data", exist_ok=True)
    with open(PENDING_FILE, "w", encoding="utf-8") as f:
        json.dump(pending, f, ensure_ascii=False, indent=2, default=str)


def next_scan_time_utc(now: datetime) -> datetime:
    """
    now(UTC)から見て次に指値スキャンが実行される時刻(UTC)を返す。
    SCAN_HOURS_JSTの各時刻をUTCに変換(-9h)して探索する。
    """
    jst = now + timedelta(hours=9)
    candidates = []
    for base_day_offset in (0, 1):
        day = jst.date() + timedelta(days=base_day_offset)
        for h in SCAN_HOURS_JST:
            cand_jst = datetime(day.year, day.month, day.day, h, 0, 0, tzinfo=timezone.utc)
            candidates.append(cand_jst)
    # JST時刻からUTCへ変換(-9h)して、nowより後の最も近いものを選ぶ
    candidates_utc = sorted(c - timedelta(hours=9) for c in candidates)
    for c in candidates_utc:
        if c > now:
            return c
    # 万一見つからなければ24時間後を返す（フォールバック、通常到達しない）
    return now + timedelta(hours=24)


def calc_pullback_price(direction: str, current_price: float, atr: float,
                         pullback_mult: float = DEFAULT_PULLBACK_ATR_MULT) -> float:
    """押し目の指値価格を計算する。LONGは安く、SHORTは高く。"""
    offset = atr * pullback_mult
    if "LONG" in direction:
        return current_price - offset
    return current_price + offset


def create_pending_order(r: dict, now: datetime,
                          pullback_mult: float = DEFAULT_PULLBACK_ATR_MULT) -> dict:
    """
    evaluate_full()の結果rから指値待機注文を作成する。
    r には pair, direction, price, stars, staged_tp, volatility_regime,
    ta_score, fa_score, fa_rate_diff が入っている前提。
    """
    pair = r.get("pair")
    direction = r.get("direction", "")
    current_price = r.get("price")
    staged = r.get("staged_tp", {}) or {}
    regime = r.get("volatility_regime", {}) or {}

    sl_width = staged.get("sl_width")
    sl_mult = regime.get("sl_multiplier")
    atr = (sl_width / sl_mult) if (sl_width and sl_mult) else None

    if current_price is None or atr is None:
        limit_price = current_price
    else:
        limit_price = calc_pullback_price(direction, current_price, atr, pullback_mult)

    _d = 3 if pair and pair.upper().endswith("JPY") else 6
    limit_price = round(limit_price, _d) if limit_price is not None else None

    return {
        "pair": pair,
        "direction": direction,
        "limit_price": limit_price,
        "scan_price": current_price,   # 参考: スキャン時点の現在値
        "pullback_atr_mult": pullback_mult,
        "sl": staged.get("sl"),
        "tp": staged.get("tp") or staged.get("tp1"),
        "tp1": staged.get("tp1") or staged.get("tp"),
        "tp2": staged.get("tp2"),
        "tp3": staged.get("tp3"),
        "trail_distance": staged.get("trail_distance", 0),
        "trail_atr_mult": staged.get("trail_atr_mult", 3.0),
        "be_target_after_tp": staged.get("be_target_after_tp"),
        "max_target": staged.get("max_target") or staged.get("tp3"),
        "tp_mode": staged.get("tp_mode", "single_with_trail"),
        "initial_stars": r.get("stars"),
        "ta_score": r.get("ta_score"),
        "fa_score": r.get("fa_score"),
        "fa_rate_diff": r.get("fa_rate_diff"),
        "regime": regime.get("regime"),
        "created_time": now.isoformat(),
        "valid_until": next_scan_time_utc(now).isoformat(),
    }


def _is_filled(order: dict, current_price: float) -> bool:
    direction = order.get("direction", "")
    limit_price = order.get("limit_price")
    if limit_price is None or current_price is None:
        return False
    if "LONG" in direction:
        # 買い指値: 現在値が指値以下まで下がってくれば約定
        return current_price <= limit_price
    else:
        # 売り指値: 現在値が指値以上まで上がってくれば約定
        return current_price >= limit_price


def check_pending_fills(pending: dict, latest_pairs: dict, now: datetime):
    """
    保留中の指値注文を現在値と照合し、約定・失効・継続待機の3グループに仕分ける。

    Returns:
        (filled: dict[pair, order], remaining: dict[pair, order], expired: dict[pair, order])
    """
    filled, remaining, expired = {}, {}, {}
    for pair, order in pending.items():
        current_price = latest_pairs.get(pair)
        valid_until = order.get("valid_until")
        is_expired = False
        if valid_until:
            try:
                if now > datetime.fromisoformat(valid_until):
                    is_expired = True
            except Exception:
                pass

        if current_price is not None and _is_filled(order, current_price):
            order = dict(order)
            order["fill_price"] = current_price
            order["fill_time"] = now.isoformat()
            filled[pair] = order
        elif is_expired:
            expired[pair] = order
        else:
            remaining[pair] = order

    return filled, remaining, expired


def pending_order_to_trade(order: dict, now: datetime) -> dict:
    """
    約定した指値注文を、trade_tracker.update_trades()と同じスキーマの
    tradeレコードに変換する。entry_priceは指値価格そのもの
    （SL/TPはスキャン時点の絶対価格をそのまま維持し、押し目分だけ実効RRが改善する）。
    """
    entry_price = order.get("limit_price")
    return {
        "pair": order.get("pair"),
        "entry_time": now.isoformat(),
        "entry_date": now.strftime("%Y-%m-%d"),
        "entry_price": entry_price,
        "direction": order.get("direction"),
        "initial_stars": order.get("initial_stars"),
        "sl": order.get("sl"),
        "initial_sl": order.get("sl"),
        "tp": order.get("tp"),
        "trail_distance": order.get("trail_distance", 0),
        "trail_atr_mult": order.get("trail_atr_mult", 3.0),
        "be_target_after_tp": order.get("be_target_after_tp", entry_price),
        "max_target": order.get("max_target"),
        "tp_mode": order.get("tp_mode", "single_with_trail"),
        "tp_hit": False,
        "trail_active": False,
        "extreme_price": entry_price,
        "tp1": order.get("tp1"),
        "tp2": order.get("tp2"),
        "tp3": order.get("tp3"),
        "ta_score": order.get("ta_score"),
        "fa_score": order.get("fa_score"),
        "fa_rate_diff": order.get("fa_rate_diff"),
        "regime": order.get("regime"),
        "entry_source": "pending_limit_order",   # 通常の成行エントリーと区別するためのフラグ
        "pending_created_time": order.get("created_time"),
        "pending_scan_price": order.get("scan_price"),
    }
