# -*- coding: utf-8 -*-
"""
mt5_bridge.py
MT5（MetaTrader5）ブローカーへの発注ブリッジ。

前提・スコープ:
  - これはローカルの投資専用PCで動く scripts/mt5_local_executor.py からのみ
    呼び出される。GitHub Actions（クラウド側）からは絶対に呼び出さないこと
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
"""

from __future__ import annotations

import os

import MetaTrader5 as mt5

_connected = False


def connect() -> tuple[bool, str]:
    """MT5端末に接続しログインする。起動中のMT5端末が必要（IPC接続のため）。

    Returns:
        (成功したか, メッセージ)
    """
    global _connected

    login = os.environ.get("MT5_LOGIN")
    password = os.environ.get("MT5_PASSWORD")
    server = os.environ.get("MT5_SERVER")
    terminal_path = os.environ.get("MT5_TERMINAL_PATH")

    if not (login and password and server):
        return False, "MT5_LOGIN/MT5_PASSWORD/MT5_SERVER が未設定です"

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
    return True, "MT5接続成功"


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
    Market Watchに存在する（＝取引可能）ことを確認する。

    見つからない/取引不可の場合はNoneを返す（呼び出し側は
    「このブローカーでは扱っていないペア」として扱うこと）。
    """
    suffix = os.environ.get("MT5_SYMBOL_SUFFIX", "")
    candidates = [f"{pair}{suffix}"] if suffix else [pair]
    if pair not in candidates:
        candidates.append(pair)  # 接尾辞なし版も一応試す

    for symbol in candidates:
        info = mt5.symbol_info(symbol)
        if info is None:
            continue
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
    （from_currency_jpy_rate側がNone扱いで安全にフォールバックする）。"""
    latest = {}
    for pair in pair_api:
        price = get_tick_price(pair)
        if price is not None:
            latest[pair] = price
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
        "tp": tp or 0.0,
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
