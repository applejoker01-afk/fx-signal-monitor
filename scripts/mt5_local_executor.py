# -*- coding: utf-8 -*-
"""
mt5_local_executor.py
「シグナル→承認→自動発注」の最後の一歩を担うローカル専用スクリプト。

★★★ これはGitHub Actions（クラウド）では絶対に実行しない ★★★
MT5端末がこのPC上で起動・ログイン済みであることが前提（IPC接続のため）。
投資専用PCで手動実行する（例: 1日3回の指値スキャン後にこのスクリプトを開き、
Discordの通知内容と照合しながら承認するかどうかを判断する）。

やること:
  1. data/pending_orders.json（クラウド側の指値待機シグナル）を読む
  2. まだ発注していないもの・期限切れでないものについて、実際の推奨ロットを
     計算し、承認プロンプトなしでそのままMT5へ指値注文を送信する
  3. 発注結果（成功/失敗・チケット番号）をメールで通知する
     （2026-07-23変更: 承認待ちをやめ「自動発注→事後通知→気に入らなければ
     MT5端末から手動キャンセル」という運用に変更した。ユーザー判断のこと）
  4. 送信結果を data/mt5_sent_orders.json に記録し、次回実行時に
     同じ注文を二重送信しないようにする

data/mt5_sent_orders.json はこのPCだけのローカル実行状態なので.gitignore対象。
クラウド側のpending_orders.jsonのライフサイクル（約定/失効判定）には一切関与しない
——ブローカー側で指値が約定したかどうかは、このスクリプトではなくMT5自体が管理する。
そのため「有効期限を過ぎたら自動失効」というクラウド側の設計は、実際にMT5へ
発注した注文には適用されない（GTC=無期限で送信しているため）。取り消したい
場合はMT5端末またはブローカーのマイページから手動キャンセルすること。
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from modules import mt5_bridge
from modules.pending_orders import load_pending_orders, PENDING_FILE
from modules.position_sizing import (
    calc_position_size, load_virtual_account, load_open_trades_raw,
)
from signal_scanner import PAIR_API, fmt_price

SENT_FILE = "data/mt5_sent_orders.json"


def load_sent() -> dict:
    if not os.path.exists(SENT_FILE):
        return {}
    try:
        with open(SENT_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_sent(sent: dict):
    os.makedirs("data", exist_ok=True)
    with open(SENT_FILE, "w", encoding="utf-8") as f:
        json.dump(sent, f, ensure_ascii=False, indent=2, default=str)


def order_key(pair: str, order: dict) -> str:
    """同一ペアで新しい指値が再登録された場合に区別するためのキー。"""
    return f"{pair}:{order.get('created_time', '')}"


def is_expired(order: dict, now: datetime) -> bool:
    valid_until = order.get("valid_until")
    if not valid_until:
        return False
    try:
        return now > datetime.fromisoformat(valid_until)
    except Exception:
        return False


def display_order(pair: str, order: dict):
    print(f"\n{'=' * 60}")
    print(f"  {pair}  {order.get('direction')}  ★{order.get('initial_stars', '?')}")
    print(f"{'=' * 60}")
    print(f"  指値: {fmt_price(pair, order.get('limit_price'))}"
          f"（スキャン時点値 {fmt_price(pair, order.get('scan_price'))}）")
    print(f"  SL:   {fmt_price(pair, order.get('sl'))}")
    print(f"  TP:   {fmt_price(pair, order.get('tp'))}")
    print(f"  TA: {order.get('ta_score')}  FA: {order.get('fa_score')}"
          f"  金利差: {order.get('fa_rate_diff')}")
    print(f"  有効期限: {order.get('valid_until')}")


def main() -> int:
    print("MT5接続中...")
    ok, msg = mt5_bridge.connect()
    print(f"  {msg}")
    if not ok:
        print("接続できないため終了します。MT5端末が起動・ログイン済みか確認してください。")
        return 1

    acc = mt5_bridge.get_account_summary()
    if acc:
        print(f"口座: {acc['login']}@{acc['server']}  "
              f"残高{acc['balance']:,.0f}{acc['currency']}  "
              f"有効証拠金{acc['equity']:,.0f}{acc['currency']}  "
              f"レバレッジ{acc['leverage']}倍")
    else:
        print("[WARN] 口座情報を取得できませんでした")

    pending = load_pending_orders()
    if not pending:
        print(f"\n{PENDING_FILE} に保留中の指値待機シグナルはありません。")
        mt5_bridge.disconnect()
        return 0

    sent = load_sent()
    now = datetime.now(timezone.utc)

    # 通貨換算・証拠金判定用に、MT5から取得できる現在値一覧を作る
    print("\nMT5から現在値を取得中（ポジションサイジング用）...")
    latest_pairs = mt5_bridge.build_latest_pairs(PAIR_API)
    print(f"  {len(latest_pairs)}/{len(PAIR_API)}ペアの現在値を取得")

    account = load_virtual_account()
    if acc:
        account = dict(account)
        account["current_balance"] = acc["balance"]  # シミュレーション残高でなく実残高を使う
    open_trades = load_open_trades_raw()

    targets = {
        k: v for k, v in pending.items()
        if order_key(k, v) not in sent and not is_expired(v, now)
    }

    if not targets:
        print("\n未処理の指値待機シグナルはありません（全て発注済みか期限切れ）。")
        mt5_bridge.disconnect()
        return 0

    print(f"\n未処理の指値待機シグナル: {len(targets)}件")

    for pair, order in targets.items():
        display_order(pair, order)

        sizing = calc_position_size(
            pair, order.get("limit_price"), order.get("sl"),
            PAIR_API, latest_pairs, account=account, open_trades=open_trades,
        )
        if not sizing.get("tradable"):
            print(f"  ⚠ サイジング不可のため発注しません: {sizing.get('note')}")
            continue

        units = sizing["units"]
        lots, note = mt5_bridge.units_to_lots(pair, units)
        if lots <= 0:
            print(f"  ⚠ ロット変換失敗のため発注しません: {note}")
            continue

        print(f"  推奨: {units}{PAIR_API[pair][0]} → {lots}ロットで自動発注します...")
        result = mt5_bridge.place_limit_order(
            pair, order.get("direction"), order.get("limit_price"),
            order.get("sl"), order.get("tp"), lots,
        )

        if result["success"]:
            print(f"  ✓ 発注成功 (チケット#{result['ticket']})")
        else:
            print(f"  ✗ 発注失敗 (retcode={result['retcode']}): {result['comment']}")

        mt5_bridge.notify_order_placed(pair, order.get("direction", ""), order, lots, result)

        sent[order_key(pair, order)] = {
            "pair": pair,
            "sent_at": now.isoformat(),
            "lots": lots,
            "units": units,
            "success": result["success"],
            "ticket": result.get("ticket"),
            "retcode": result.get("retcode"),
            "comment": result.get("comment"),
        }
        save_sent(sent)

    mt5_bridge.disconnect()
    print("\n完了。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
