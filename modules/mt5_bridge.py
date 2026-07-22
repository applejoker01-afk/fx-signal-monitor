# -*- coding: utf-8 -*-
"""
mt5_bridge.py
MT5（MetaTrader5）ブローカーへの発注ブリッジ。

前提・スコープ:
  - これはローカルの投資専用PCで動く scripts/mt5_local_executor.py と
    scripts/mt5_position_manager.py からのみ呼び出される。
    GitHub Actions（クラウド側）からは絶対に呼び出さないこと
    （MT5端末はローカルPC上で起動・ログイン済みである必要があり、
    プロセス間通信でしか繋がらないため、クラウド環境では原理的に動作しない）。
  - シグナル判定（TA/FA/カルマンフローレイヤー等）は一切ここでは行わない。
    signal_scanner.py / pending_orders.py が既に計算した
    limit_price・SL・TPをそのまま証券会社へ流すだけの「実行層」。
  - 発注方式は指値(LIMIT)のみ。pending_orders.jsonの設計（現在値からの
    押し目を待つ）と整合させるため、成行(MARKET)は現時点でサポートしない。

環境変数:
  MT5_LOGIN            必須。MT5口座番号（整数）
  MT5_PASSWORD         必須。MT5口座パスワード
  MT5_SERVER           必須。サーバー名（例: "XMTrading-MT5"）
  MT5_TERMINAL_PATH    任意。terminal64.exeのフルパス（複数MT5併用時のみ必要）
  MT5_SYMBOL_SUFFIX    任意。ブローカーのシンボル命名規則の接尾辞
                        （例: マイクロ口座で"USDJPYm"のような場合は"m"）
  MT5_LOT_SIZE_UNITS   任意。1.0ロット＝何FROM通貨単位か（既定100000＝標準口座想定。
                        マイクロ口座で1ロット=1,000通貨のブローカーもあるため要確認）
  SMTP_HOST/SMTP_PORT/SMTP_USER/SMTP_PASS/MAIL_FROM/MAIL_TO
                        任意。設定時のみ発注後にメール通知を送る
                        （signal_scanner.pyと同じSMTP設定を流用可能）
"""

from __future__ import annotations

import os
import smtplib
from email.header import Header
from email.mime.text import MIMEText

import MetaTrader5 as mt5

_connected = False


def connect() -> tuple[bool, str]:
    """MT5端末に接続する。起動中のMT5端末が必要（IPC接続のため）。

    2026-07-23実機確認: MT5端末が既にログイン済みなら、引数なしの
    mt5.initialize()だけで現在のセッションにそのまま相乗りできる。
    まずこれを試し、失敗した場合のみ環境変数(MT5_LOGIN/PASSWORD/SERVER)
    による明示ログインにフォールバックする（端末が未起動、または
    別口座へ切り替えたい場合向け）。

    Returns:
        (成功したか, メッセージ)
    """
    global _connected

    if mt5.initialize():
        info = mt5.account_info()
        _connected = True
        if info:
            return True, f"MT5接続成功（起動中の端末に相乗り: {info.login}@{info.server}）"
        return True, "MT5接続成功（起動中の端末に相乗り、口座情報は未取得）"

    login = os.environ.get("MT5_LOGIN")
    password = os.environ.get("MT5_PASSWORD")
    server = os.environ.get("MT5_SERVER")
    terminal_path = os.environ.get("MT5_TERMINAL_PATH")

    if not (login and password and server):
        code, desc = mt5.last_error()
        return False, (
            f"起動中端末への接続失敗 (code={code}): {desc} / "
            "かつ MT5_LOGIN/MT5_PASSWORD/MT5_SERVER も未設定です。"
            "MT5端末を起動してログインするか、環境変数を設定してください。"
        )

    try:
        login_int = int(login)
    except ValueError:
        return False, f"MT5_LOGIN は数値である必要があります: {login!r}"

    kwargs = {"login": login_int, "password": password, "server": server}
    if terminal_path:
        init_ok = mt5.initialize(terminal_path, **kwargs)
    else:
        init_ok = mt5.initialize(**kwargs)

    if not init_ok:
        code, desc = mt5.last_error()
        return False, f"MT5接続失敗 (code={code}): {desc}"

    _connected = True
    return True, "MT5接続成功（環境変数によるログイン）"


def disconnect():
    global _connected
    if _connected:
        mt5.shutdown()
        _connected = False


def get_account_summary() -> dict | None:
    """接続中口座の残高・証拠金情報を返す（発注前の目視確認用）。"""
    info = mt5.account_info()
    if info is None:
        return None
    return {
        "login": info.login,
        "server": info.server,
        "balance": info.balance,
        "equity": info.equity,
        "margin": info.margin,
        "margin_free": info.margin_free,
        "currency": info.currency,
        "leverage": info.leverage,
    }


def resolve_symbol(pair: str) -> str | None:
    """自社ペア名(例:"USDJPY")をブローカーのシンボル名に変換し、
    実際に発注可能（trade_mode != DISABLED）なシンボルを返す。

    2026-07-23判明・重要: XM(Tradexfin)デモでは主要7ペア(USDJPY/EURUSD/
    GBPUSD/AUDUSD/NZDUSD/USDCAD/USDCHF)の接尾辞なしシンボルは実在し
    価格も付くが、trade_mode=DISABLED の「参照用」シンボルで発注できない
    （実機でretcode 10017 "Trade disabled"を確認）。実際に発注できるのは
    「#」付きの方（例: "USDJPY#"）。クロス通貨(EURJPY/GBPJPY/EURGBP/EURAUD/
    GBPAUD/GBPCHF/AUDCHF/EURCHF/AUDNZD/EURNZD等)は「#」付きしか存在しない。
    エキゾチック通貨(SEK/NOK/BRL/PLN/KRW/TRY/ZAR/INR/HKD/CNYの各JPYクロス、
    USDCNY)はどちらの形でも存在せず、ブローカー側で本当に取り扱っていない。

    そのため単に「存在するか」ではなく「trade_mode != DISABLED か」を
    判定基準にする。見つからない/取引不可の場合はNoneを返す（呼び出し側は
    「このブローカーでは扱っていないペア」として扱うこと）。
    """
    env_suffix = os.environ.get("MT5_SYMBOL_SUFFIX", "")
    # 環境変数指定があれば最優先、次に"#"付き（実機で判明した実際の取引可能形）、
    # 最後に接尾辞なしを試す（"#"が存在しないブローカー・口座タイプ向けの保険）
    candidates = []
    if env_suffix:
        candidates.append(f"{pair}{env_suffix}")
    if f"{pair}#" not in candidates:
        candidates.append(f"{pair}#")
    if pair not in candidates:
        candidates.append(pair)

    for symbol in candidates:
        info = mt5.symbol_info(symbol)
        if info is None:
            continue
        if info.trade_mode == mt5.SYMBOL_TRADE_MODE_DISABLED:
            continue  # 価格は付くが発注不可な参照用シンボル。次の候補へ
        if not info.visible:
            if not mt5.symbol_select(symbol, True):
                continue
        return symbol
    return None


def get_tick_price(pair: str) -> float | None:
    """指定ペアの現在Bid価格を返す（ポジションサイジングの通貨換算用）。
    シンボルが存在しない/ティック取得不可ならNone。"""
    symbol = resolve_symbol(pair)
    if symbol is None:
        return None
    tick = mt5.symbol_info_tick(symbol)
    if tick is None or tick.bid == 0:
        return None
    return float(tick.bid)


def build_latest_pairs(pair_api: dict) -> dict:
    """pair_api内の全ペアについて、このブローカーで取得できる現在値をまとめて返す。
    modules.position_sizing.calc_position_size() の latest_pairs 引数にそのまま渡せる。
    ブローカーが扱っていないペアはキーごと省略される
    （from_currency_jpy_rate側がNone扱いで安全にフォールバックする）。

    2026-07-23判明: symbol_select()でMarket Watchに追加した直後は、配信が
    温まるまでtickが0で返ることがある（XM実機で確認、1回目22ペア中9ペアが
    tick取得失敗→2回目は22/22件成功）。先に全シンボルをresolve/select してから
    少し待ってティックを取得する2段階方式でこれを吸収する。"""
    import time

    symbols = {}
    for pair in pair_api:
        sym = resolve_symbol(pair)
        if sym:
            symbols[pair] = sym

    if symbols:
        time.sleep(0.5)  # 配信ウォームアップ待ち

    latest = {}
    for pair, sym in symbols.items():
        tick = mt5.symbol_info_tick(sym)
        if tick is not None and tick.bid != 0:
            latest[pair] = float(tick.bid)
    return latest


def units_to_lots(pair: str, units: int) -> tuple[float, str]:
    """FROM通貨建てのunits数を、ブローカーのロット単位・刻み幅・上下限に
    合わせて丸めたロット数に変換する。

    Returns:
        (lots, note) — lots が 0.0 の場合は発注不可（理由はnoteに記載）
    """
    symbol = resolve_symbol(pair)
    if symbol is None:
        return 0.0, f"{pair}: このブローカーでは扱っていないシンボル"

    info = mt5.symbol_info(symbol)
    if info is None:
        return 0.0, f"{pair}: シンボル情報取得失敗"

    lot_size_units = float(os.environ.get("MT5_LOT_SIZE_UNITS", "100000"))
    raw_lots = units / lot_size_units

    step = info.volume_step or 0.01
    lots = (raw_lots // step) * step
    lots = round(lots, 2)

    if lots < info.volume_min:
        return 0.0, (
            f"{pair}: 計算ロット{lots}がブローカー最小ロット{info.volume_min}未満"
            f"（units={units}, lot_size_units={lot_size_units:.0f}想定。"
            f"実際の1ロット単位が違う場合はMT5_LOT_SIZE_UNITSを見直すこと）"
        )
    if lots > info.volume_max:
        lots = info.volume_max

    return lots, ""


def place_limit_order(
    pair: str, direction: str, limit_price: float,
    sl: float | None, tp: float | None, lots: float,
    comment: str = "fx-signal-monitor",
) -> dict:
    """指値(LIMIT)注文をブローカーへ送信する。

    重要(2026-07-23修正): tp引数はブローカーへは送らない。
    既存のシミュレーション戦略(modules/trade_tracker.py)の"tp"は
    「ここで決済」ではなく「ここでSLをBE+0.5Rへ移動しトレーリング開始」
    という"トリガー"であり、本物の利確価格ではない
    （tp_mode="single_with_trail"、[[2026-06-10-fx-monitor-customization]]）。
    ここでブローカー側のtpとして送ってしまうと、価格がtpに触れた瞬間に
    本当に決済されてしまい、トレンド継続時の伸ばし分を全て取り逃す。
    ブローカー側には初期SLのみを安全装置として送り、TP到達後の
    BE移動・トレーリングは scripts/mt5_position_manager.py が
    check_exit_condition()と同じロジックでSLを動的に更新することで実現する。
    tp引数は将来の拡張用に残しているが現状は未使用（呼び出し側の互換性維持）。

    Returns:
        {"success": bool, "ticket": int|None, "retcode": int|None, "comment": str}
    """
    symbol = resolve_symbol(pair)
    if symbol is None:
        return {"success": False, "ticket": None, "retcode": None,
                "comment": f"{pair}: シンボル未対応"}

    is_long = "LONG" in direction
    order_type = mt5.ORDER_TYPE_BUY_LIMIT if is_long else mt5.ORDER_TYPE_SELL_LIMIT

    request = {
        "action": mt5.TRADE_ACTION_PENDING,
        "symbol": symbol,
        "volume": lots,
        "type": order_type,
        "price": limit_price,
        "sl": sl or 0.0,
        # tpはブローカーへ送らない（上記docstring参照）。position_managerが動的管理する。
        "type_time": mt5.ORDER_TIME_GTC,
        "type_filling": mt5.ORDER_FILLING_RETURN,
        "comment": comment[:31],  # MT5のcomment長制限
    }

    result = mt5.order_send(request)
    if result is None:
        code, desc = mt5.last_error()
        return {"success": False, "ticket": None, "retcode": None,
                "comment": f"order_send()がNoneを返却 (code={code}): {desc}"}

    success = result.retcode == mt5.TRADE_RETCODE_DONE
    return {
        "success": success,
        "ticket": result.order if success else None,
        "retcode": result.retcode,
        "comment": result.comment,
    }


# ============================================================
# 保有中ポジションの動的管理（2026-07-23追加）
# ============================================================

def get_open_positions(comment_filter: str | None = "fx-signal") -> list:
    """自システムが発注したポジション一覧を返す（本物の口座上のポジション）。
    comment_filterを指定すると、そのcommentを含むものだけに絞る
    （手動で入れた他のポジションを誤って操作しないため）。"""
    positions = mt5.positions_get()
    if positions is None:
        return []
    result = []
    for p in positions:
        if comment_filter and comment_filter not in (p.comment or ""):
            continue
        result.append({
            "ticket": p.ticket,
            "symbol": p.symbol,
            "type": "LONG" if p.type == mt5.ORDER_TYPE_BUY else "SHORT",
            "volume": p.volume,
            "price_open": p.price_open,
            "sl": p.sl,
            "tp": p.tp,
            "price_current": p.price_current,
            "profit": p.profit,
            "comment": p.comment,
            "time": p.time,
        })
    return result


def get_pending_order_tickets(comment_filter: str | None = "fx-signal") -> set:
    """未約定の指値注文チケット番号の集合を返す（約定検知の判定材料）。"""
    orders = mt5.orders_get()
    if orders is None:
        return set()
    return {
        o.ticket for o in orders
        if not comment_filter or comment_filter in (o.comment or "")
    }


def modify_position_sl(ticket: int, new_sl: float) -> dict:
    """既存ポジションのSLのみを変更する（TPはposition_managerが管理するため送らない）。"""
    pos = mt5.positions_get(ticket=ticket)
    if not pos:
        return {"success": False, "comment": f"ポジション#{ticket}が見つかりません"}
    p = pos[0]

    request = {
        "action": mt5.TRADE_ACTION_SLTP,
        "position": ticket,
        "symbol": p.symbol,
        "sl": new_sl,
        "tp": p.tp,  # 既存のtp設定を保持（通常0のまま）
    }
    result = mt5.order_send(request)
    if result is None:
        code, desc = mt5.last_error()
        return {"success": False, "comment": f"SL変更失敗 (code={code}): {desc}"}
    success = result.retcode == mt5.TRADE_RETCODE_DONE
    return {"success": success, "retcode": result.retcode, "comment": result.comment}


def close_position(ticket: int, comment: str = "fx-signal-close") -> dict:
    """成行でポジションを全量決済する。"""
    pos = mt5.positions_get(ticket=ticket)
    if not pos:
        return {"success": False, "comment": f"ポジション#{ticket}が見つかりません（既に決済済みの可能性）"}
    p = pos[0]

    is_long = p.type == mt5.ORDER_TYPE_BUY
    close_type = mt5.ORDER_TYPE_SELL if is_long else mt5.ORDER_TYPE_BUY
    tick = mt5.symbol_info_tick(p.symbol)
    if tick is None:
        return {"success": False, "comment": f"{p.symbol}: ティック取得失敗"}
    price = tick.bid if is_long else tick.ask

    request = {
        "action": mt5.TRADE_ACTION_DEAL,
        "symbol": p.symbol,
        "volume": p.volume,
        "type": close_type,
        "position": ticket,
        "price": price,
        "type_filling": mt5.ORDER_FILLING_RETURN,
        "comment": comment[:31],
    }
    result = mt5.order_send(request)
    if result is None:
        code, desc = mt5.last_error()
        return {"success": False, "comment": f"決済失敗 (code={code}): {desc}"}
    success = result.retcode == mt5.TRADE_RETCODE_DONE
    return {
        "success": success,
        "close_price": price if success else None,
        "retcode": result.retcode,
        "comment": result.comment,
    }


def notify_order_placed(pair: str, direction: str, order: dict, lots: float,
                         send_result: dict) -> bool:
    """指値発注（成功/失敗）をメールで通知する。

    承認フローを持たない「自動発注→事後通知→気に入らなければ手動キャンセル」
    運用のための通知。件名だけでスマホの通知欄から内容が分かるようにする。

    Returns: 送信できたかどうか（SMTP未設定時はFalseを返すだけで例外にしない）
    """
    smtp_host = os.environ.get("SMTP_HOST", "smtp.gmail.com")
    smtp_port = os.environ.get("SMTP_PORT", "465")
    smtp_user = os.environ.get("SMTP_USER")
    smtp_pass = os.environ.get("SMTP_PASS")
    from_addr = os.environ.get("MAIL_FROM")
    to_addr = os.environ.get("MAIL_TO")

    if not all([smtp_user, smtp_pass, from_addr, to_addr]):
        print("[INFO] MT5発注通知: SMTP未設定のためメール送信をスキップ")
        return False

    dir_arrow = "↑LONG" if "LONG" in direction else "↓SHORT"

    if send_result.get("success"):
        subject = f"[MT5] 指値発注 {pair}{dir_arrow} #{send_result['ticket']}"
        body = (
            f"MT5に指値注文を自動発注しました。\n\n"
            f"ペア: {pair}  方向: {direction}\n"
            f"指値: {order.get('limit_price')}\n"
            f"SL: {order.get('sl')}  TP: {order.get('tp')}\n"
            f"ロット: {lots}\n"
            f"チケット: #{send_result['ticket']}\n\n"
            f"この取引をしたくない場合は、MT5端末（またはXMのマイページ）から\n"
            f"チケット#{send_result['ticket']}を手動でキャンセルしてください。\n"
            f"未約定の指値は valid_until を過ぎても自動失効しません"
            f"（クラウド側のpending_orders.jsonの失効判定とは独立しています）。\n"
        )
    else:
        subject = f"[MT5] 発注失敗 {pair}{dir_arrow}"
        body = (
            f"MT5への指値発注が失敗しました。対応不要ですが記録として送信します。\n\n"
            f"ペア: {pair}  方向: {direction}\n"
            f"指値: {order.get('limit_price')}\n"
            f"エラー: retcode={send_result.get('retcode')} "
            f"{send_result.get('comment')}\n"
        )

    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = Header(subject, "utf-8")
    msg["From"] = from_addr
    msg["To"] = to_addr

    try:
        with smtplib.SMTP_SSL(smtp_host, int(smtp_port or 465), timeout=20) as s:
            s.login(smtp_user, smtp_pass)
            s.send_message(msg)
        print(f"[OK] MT5発注通知メール送信: {to_addr}")
        return True
    except Exception as e:
        print(f"[ERROR] MT5発注通知メール送信失敗: {e}")
        return False


# ============================================================
# 約定検知（2026-07-23追加）
# ============================================================

def get_order_final_state(ticket: int) -> dict | None:
    """指値注文チケットの最終状態を調べる。

    Returns:
        None                                      … まだ約定待ち（発注中）
        {"filled": True,  "position_ticket": int} … 約定してポジション化した
        {"filled": False, "state": str}            … 取消/失効/拒否等で消滅した
    """
    # まだ「発注中(working)」ならhistory側には出てこず、orders_get側に残る
    still_pending = mt5.orders_get(ticket=ticket)
    if still_pending:
        return None

    hist = mt5.history_orders_get(ticket=ticket)
    if not hist:
        return None  # ブローカー側の反映待ち。次回また確認する

    o = hist[0]
    state_names = {
        0: "STARTED", 1: "PLACED", 2: "CANCELED", 3: "PARTIAL",
        4: "FILLED", 5: "REJECTED", 6: "EXPIRED",
        7: "REQUEST_ADD", 8: "REQUEST_MODIFY", 9: "REQUEST_CANCEL",
    }
    if o.state == mt5.ORDER_STATE_FILLED:
        return {"filled": True, "position_ticket": o.position_id}
    return {"filled": False, "state": state_names.get(o.state, str(o.state))}


def notify_position_closed(pair: str, trade: dict, exit_result: dict, close_result: dict) -> bool:
    """保有中ポジションの決済（TP/SL/トレール/シグナル反転等）をメールで通知する。"""
    smtp_host = os.environ.get("SMTP_HOST", "smtp.gmail.com")
    smtp_port = os.environ.get("SMTP_PORT", "465")
    smtp_user = os.environ.get("SMTP_USER")
    smtp_pass = os.environ.get("SMTP_PASS")
    from_addr = os.environ.get("MAIL_FROM")
    to_addr = os.environ.get("MAIL_TO")

    if not all([smtp_user, smtp_pass, from_addr, to_addr]):
        print("[INFO] MT5決済通知: SMTP未設定のためメール送信をスキップ")
        return False

    reason = exit_result.get("exit_reason", "?")
    result_label = exit_result.get("result", "?")
    pips = exit_result.get("pips")
    emoji = "✅" if result_label == "WIN" else ("➖" if result_label == "BE" else "❌")

    subject = f"[MT5] {emoji}決済 {pair} {reason} ({result_label})"
    body = (
        f"MT5ポジションが決済されました。\n\n"
        f"ペア: {pair}  方向: {trade.get('direction')}\n"
        f"エントリー: {trade.get('entry_price')}\n"
        f"決済理由: {reason}\n"
        f"決済価格: {close_result.get('close_price')}\n"
        f"pips: {pips}\n"
        f"結果: {result_label}\n"
    )

    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = Header(subject, "utf-8")
    msg["From"] = from_addr
    msg["To"] = to_addr

    try:
        with smtplib.SMTP_SSL(smtp_host, int(smtp_port or 465), timeout=20) as s:
            s.login(smtp_user, smtp_pass)
            s.send_message(msg)
        print(f"[OK] MT5決済通知メール送信: {to_addr}")
        return True
    except Exception as e:
        print(f"[ERROR] MT5決済通知メール送信失敗: {e}")
        return False
