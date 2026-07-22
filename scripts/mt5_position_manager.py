# -*- coding: utf-8 -*-
"""
mt5_position_manager.py
「保有中ポジションの動的管理」をMT5の実ポジションに反映するローカル専用スクリプト。

★★★ これはGitHub Actions（クラウド）では絶対に実行しない ★★★
mt5_local_executor.pyと同じ理由（MT5端末のローカルIPC接続が必要）。

背景（2026-07-23、重大な設計漏れとして修正）:
  mt5_local_executor.pyは「発注」だけを自動化していたが、その後の
  ポジション管理（TP到達後にSLをBE+0.5Rへ移動してトレーリング開始する等）は
  何もしていなかった。既存のクラウド側シミュレーション
  (modules/trade_tracker.py の check_exit_condition()) が持つロジックを、
  実際のMT5ポジションにもそのまま適用する。

やること（1回の実行で）:
  1. mt5_local_executor.pyが送信した指値のうち、まだ「約定したかどうか」を
     確認していないものをチェックし、約定していれば data/mt5_open_trades.json
     に追跡対象として登録する（trade_trackerと同じスキーマのレコードを作る）。
  2. 追跡中の各ポジションについて、現在値・現在のシグナル(TA/FA再評価)を使い
     check_exit_condition() を呼ぶ（クラウド側の毎時ジョブと全く同じ関数）。
       - 状態更新のみ(SL移動)ならMT5のポジションSLを実際に変更する
       - 決済条件成立なら成行でポジションを閉じ、メール通知する
  3. ブローカー側で既に閉じられている（初期SL到達等）ポジションは
     追跡対象から静かに外す。

このスクリプトは発注(mt5_local_executor.py)より高頻度に走らせる必要がある
（トレーリングは価格変動に追随する必要があるため）。
setup_mt5_task_scheduler.pyでは毎時実行のタスクとして別登録すること。

data/mt5_open_trades.json はこのPCだけのローカル実行状態（.gitignore対象）。
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from modules import mt5_bridge
from modules.trade_tracker import check_exit_condition
from modules.pending_orders import pending_order_to_trade
from modules.rate_fetcher import fetch_live_central_bank_rates
from modules.sentiment_monitor import evaluate_market_sentiment
from signal_scanner import evaluate_full, fetch_history, PAIR_API, fmt_price

SENT_FILE = "data/mt5_sent_orders.json"
MT5_OPEN_TRADES_FILE = "data/mt5_open_trades.json"


def load_json(path: str) -> dict:
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_json(path: str, obj: dict):
    os.makedirs("data", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2, default=str)


def detect_fills(sent: dict, mt5_trades: dict, now: datetime) -> tuple[dict, dict]:
    """未解決の送信済み指値について約定/失効を確認し、約定分をmt5_tradesへ追加する。"""
    changed = False
    for key, rec in sent.items():
        if rec.get("resolved"):
            continue
        ticket = rec.get("ticket")
        if not ticket:
            rec["resolved"] = True  # 送信自体が失敗していたので追跡不要
            changed = True
            continue

        state = mt5_bridge.get_order_final_state(ticket)
        if state is None:
            continue  # まだ発注中（未約定）。次回また確認する

        if state.get("filled"):
            pos_ticket = state["position_ticket"]
            order_snapshot = rec.get("order_snapshot", {})
            trade = pending_order_to_trade(order_snapshot, now)
            trade["pair"] = rec["pair"]
            trade["mt5_ticket"] = pos_ticket
            trade["entry_source"] = "mt5_live"
            mt5_trades[str(pos_ticket)] = trade
            print(f"  [約定検知] {rec['pair']} 指値チケット#{ticket} "
                  f"→ ポジション#{pos_ticket}として追跡開始")
        else:
            print(f"  [約定なし] {rec['pair']} 指値チケット#{ticket}: {state.get('state')}")

        rec["resolved"] = True
        changed = True

    return sent, mt5_trades


def manage_positions(mt5_trades: dict, now: datetime) -> dict:
    """追跡中の各ポジションを再評価し、SL更新 or 決済を行う。"""
    if not mt5_trades:
        return mt5_trades

    live_tickets = {p["ticket"] for p in mt5_bridge.get_open_positions()}

    cb_rates = fetch_live_central_bank_rates()
    sentiment = evaluate_market_sentiment()

    to_remove = []
    for ticket_str, trade in mt5_trades.items():
        ticket = int(ticket_str)
        pair = trade["pair"]

        if ticket not in live_tickets:
            print(f"  [追跡終了] {pair} #{ticket}: ブローカー側で既に決済済み"
                  f"（初期SL到達など）。追跡から除外")
            to_remove.append(ticket_str)
            continue

        current_price = mt5_bridge.get_tick_price(pair)
        if current_price is None:
            print(f"  [WARN] {pair} #{ticket}: 現在値取得失敗、今回はスキップ")
            continue

        prices = fetch_history(pair, 280)
        if prices and len(prices) >= 60:
            sig = evaluate_full(pair, prices[-1], prices, cb_rates, sentiment, now)
            current_stars = sig["stars"]
            current_direction = sig["direction"]
        else:
            current_stars = trade.get("initial_stars", 0)
            current_direction = trade.get("direction", "")

        result = check_exit_condition(trade, current_price, current_stars, current_direction)
        if result is None:
            continue

        if "_state_update" in result:
            upd = result["_state_update"]
            trade.update(upd)
            if "sl" in upd:
                mod = mt5_bridge.modify_position_sl(ticket, upd["sl"])
                if mod.get("success"):
                    print(f"  [SL更新] {pair} #{ticket}: SL -> {fmt_price(pair, upd['sl'])}")
                else:
                    print(f"  [SL更新失敗] {pair} #{ticket}: {mod.get('comment')}")
        else:
            close_res = mt5_bridge.close_position(ticket)
            print(f"  [決済] {pair} #{ticket} 理由={result['exit_reason']} "
                  f"結果={result['result']} pips={result['pips']}: {close_res}")
            mt5_bridge.notify_position_closed(pair, trade, result, close_res)
            if close_res.get("success"):
                to_remove.append(ticket_str)
            # 決済失敗時は次回また試行するため追跡を続ける

    for t in to_remove:
        mt5_trades.pop(t, None)

    return mt5_trades


def main() -> int:
    print("MT5接続中...")
    ok, msg = mt5_bridge.connect()
    print(f"  {msg}")
    if not ok:
        return 1

    now = datetime.now(timezone.utc)

    sent = load_json(SENT_FILE)
    mt5_trades = load_json(MT5_OPEN_TRADES_FILE)

    print("\n[1/2] 約定検知...")
    sent, mt5_trades = detect_fills(sent, mt5_trades, now)
    save_json(SENT_FILE, sent)
    save_json(MT5_OPEN_TRADES_FILE, mt5_trades)

    print(f"\n[2/2] 保有中ポジションの再評価（{len(mt5_trades)}件）...")
    mt5_trades = manage_positions(mt5_trades, now)
    save_json(MT5_OPEN_TRADES_FILE, mt5_trades)

    mt5_bridge.disconnect()
    print("\n完了。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
