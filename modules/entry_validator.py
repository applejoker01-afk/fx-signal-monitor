"""
entry_validator.py
エントリー有効性チェックモジュール（2026-06-25 追加）

問題:
  シグナル確認 → 発注までの時間差で価格が動き、
  実際のASK/BID エントリー価格では RR が大幅に劣化する。

解決策:
  「最大許容エントリー価格」= RR が MIN_RR_FLOOR を下回る直前の ASK/BID 価格
  この価格を超えていたら指値を入れるか SKIP する。

使い方:
    from modules.entry_validator import validate_entry, format_entry_block

    ev = validate_entry(r["price"], staged["sl"], r["atr"], r["pair"],
                        r["direction"], staged["spread_pips"], staged["tp"])
    print(format_entry_block(ev, r["pair"]))
"""

# エントリーを許容する最低 RR（これ以下になる価格では入らない）
MIN_RR_FLOOR = 0.70

# これ以下に RR が落ちたら "LIMIT" 推奨に変える（警告域）
WARN_RR = 0.90


def pip_size(pair: str) -> float:
    """1 pip の価格単位。JPYクロス=0.01、それ以外=0.0001。"""
    return 0.01 if (pair or "").upper().endswith("JPY") else 0.0001


def _decimals(pair: str) -> int:
    return 3 if (pair or "").upper().endswith("JPY") else 5


def _fmt(price: float, pair: str) -> str:
    """価格を適切な桁数でフォーマット。"""
    if price is None:
        return "?"
    return f"{price:.{_decimals(pair)}f}"


def validate_entry(
    signal_price: float,
    sl: float,
    atr: float,
    pair: str,
    direction: str = "LONG",
    spread_pips: float = None,
    tp: float = None,
) -> dict:
    """
    シグナル価格から「最大許容エントリー価格（ASK/BID 基準）」を計算する。

    計算ロジック:
      LONG の場合:
        実エントリー = ASK = mid + spread/2
        RR = (TP - ASK) / (ASK - SL)
        RR >= MIN_RR_FLOOR を満たす最大 ASK:
          max_ask = (TP + MIN_RR * SL) / (1 + MIN_RR)

      SHORT の場合:
        実エントリー = BID = mid - spread/2
        RR = (BID - TP) / (SL - BID)
        min_bid = (TP + MIN_RR * SL) / (1 + MIN_RR)  ← これ以下は入らない

    Args:
        signal_price: シグナル生成時の中値価格
        sl:          SL 価格
        atr:         ATR
        pair:        通貨ペア
        direction:   "LONG" or "SHORT"
        spread_pips: 動的スプレッド (pips)。None = スプレッドを考慮しない
        tp:          TP 価格。None の場合は SL 距離の 30% をスリッページ上限とする

    Returns dict:
        status:              "ENTER" / "LIMIT" / "SKIP"
        max_entry_mid:       最大エントリー許容価格（中値ベース）
        max_entry_exec:      最大エントリー許容価格（実執行価格: LONG=ASK, SHORT=BID）
        limit_order_price:   推奨指値価格（中値ベース・シグナル価格付近）
        sl_original:         元の SL
        sl_dist_pips:        SL距離（シグナル時 ASK/BID から）
        rr_at_signal:        シグナル時の RR（ASK/BID 基準）
        rr_at_max_entry:     max_entry_exec 時の RR
        spread_pips:         使用したスプレッド値
        slip_budget_pips:    スリッページ許容 pips
        note:                状況説明
        action_line:         1行アクション指示（通知表示用）
    """
    is_long = "LONG" in (direction or "").upper()
    ps = pip_size(pair)
    dec = _decimals(pair)
    sp_pips = spread_pips or 0.0
    spread_half = (sp_pips * ps) / 2.0

    # ── 実エントリー価格（シグナル時）──
    if is_long:
        signal_exec = signal_price + spread_half   # ASK
        sl_dist = signal_exec - sl                 # LONGのSL距離: exec > sl
    else:
        signal_exec = signal_price - spread_half   # BID
        sl_dist = sl - signal_exec                 # SHORTのSL距離: sl > exec

    if sl_dist <= 0:
        # SL がエントリーより有利な側にある（データ異常）
        return {
            "status": "SKIP",
            "max_entry_mid": signal_price,
            "max_entry_exec": signal_exec,
            "limit_order_price": signal_price,
            "sl_original": sl,
            "sl_dist_pips": 0,
            "rr_at_signal": None,
            "rr_at_max_entry": None,
            "spread_pips": sp_pips,
            "slip_budget_pips": 0,
            "note": "SL距離が 0 以下（データ異常）。エントリー不可。",
            "action_line": "SKIP — SL設定エラー",
        }

    sl_dist_pips = round(sl_dist / ps, 1)

    # ── シグナル時 RR ──
    if tp is not None:
        if is_long:
            tp_dist_signal = tp - signal_exec
        else:
            tp_dist_signal = signal_exec - tp
        rr_at_signal = round(tp_dist_signal / sl_dist, 2) if sl_dist > 0 else None
    else:
        rr_at_signal = None

    # ── 最大エントリー許容価格 ──
    if tp is not None:
        # RR = (TP - max_exec) / (max_exec - SL) >= MIN_RR  [LONG]
        # → max_exec <= (TP + MIN_RR × SL) / (1 + MIN_RR)
        # RR = (min_exec - TP) / (SL - min_exec) >= MIN_RR  [SHORT]
        # → min_exec >= (TP + MIN_RR × SL) / (1 + MIN_RR)
        max_exec = (tp + MIN_RR_FLOOR * sl) / (1 + MIN_RR_FLOOR)
        if is_long:
            max_entry_exec = max_exec
            max_entry_mid = max_entry_exec - spread_half   # mid換算
        else:
            max_entry_exec = max_exec
            max_entry_mid = max_entry_exec + spread_half   # mid換算
    else:
        # TP なし: SL距離の 30% をスリッページ上限とする
        slip_tol = sl_dist * 0.30
        if is_long:
            max_entry_exec = signal_exec + slip_tol
            max_entry_mid = max_entry_exec - spread_half
        else:
            max_entry_exec = signal_exec - slip_tol
            max_entry_mid = max_entry_exec + spread_half

    # スリッページ予算（mid 基準でシグナル価格からどれだけ動いてよいか）
    if is_long:
        slip_budget = max_entry_mid - signal_price
    else:
        slip_budget = signal_price - max_entry_mid
    slip_budget_pips = round(slip_budget / ps, 1) if ps else 0

    # max_entry 時の RR
    if tp is not None:
        sl_dist_at_max = abs(max_entry_exec - sl)
        if is_long:
            tp_dist_at_max = tp - max_entry_exec
        else:
            tp_dist_at_max = max_entry_exec - tp
        rr_at_max = round(tp_dist_at_max / sl_dist_at_max, 2) if sl_dist_at_max > 0 else None
    else:
        rr_at_max = None

    # ── 推奨指値価格 ──
    # 元のシグナル mid 価格で指値。価格が戻るまで待つ。
    limit_order_price = round(signal_price, dec)

    # ── 判定 ──
    # スプレッド（片道コスト=full spread）が mid ベース SL距離の 40% 以上 → 構造的に無理
    # ※ エントリー時点でスプレッド分だけ即時含み損になるため、
    #    全スプレッドが SL 距離の大部分を食うと実質的なリスクが著しく高い
    sl_dist_mid = abs(signal_price - sl)   # mid 基準の SL 距離
    spread_full = sp_pips * ps             # full spread (price 単位)
    spread_sl_ratio = spread_full / sl_dist_mid if sl_dist_mid > 0 else 999
    if spread_sl_ratio >= 0.40:
        status = "SKIP"
        note = (
            f"スプレッド {sp_pips:.1f}pips が mid 基準 SL距離 "
            f"{round(sl_dist_mid/ps,1):.1f}pips の"
            f"{spread_sl_ratio*100:.0f}% を占める。エントリー構造的に不利。"
        )
        action = f"SKIP — spread/SL_mid={spread_sl_ratio*100:.0f}% (上限40%)"

    elif slip_budget_pips <= 0:
        # TP が近すぎる・シグナル価格でも RR がギリギリ
        status = "LIMIT"
        note = (
            f"RR {rr_at_signal} が既に警戒域。シグナル価格 {_fmt(signal_price, pair)} "
            f"ちょうど or それより有利な価格でのみ入ること。"
        )
        action = f"LIMIT指値 {_fmt(limit_order_price, pair)} のみ"

    elif slip_budget_pips < sp_pips * 2:
        # 予算がスプレッドの 2 倍未満 → 成行注文だと fill 誤差でオーバーするリスク大
        # 指値必須
        status = "LIMIT"
        note = (
            f"スリッページ許容 {slip_budget_pips:.1f}pips がスプレッド {sp_pips:.1f}pips の"
            f"2倍未満のため成行注文は危険。指値でシグナル価格 {_fmt(signal_price, pair)} に入ること。"
        )
        action = f"LIMIT指値 {_fmt(limit_order_price, pair)} のみ（成行不可）"

    else:
        # 通常ケース: 成行でもスリッページ余裕あり
        status = "ENTER"
        note = (
            f"シグナル価格 {_fmt(signal_price, pair)} から最大 {slip_budget_pips:.1f}pips "
            f"以内の{'ASK' if is_long else 'BID'}価格まで有効。"
            f"RR: シグナル時{rr_at_signal} → max entry時{rr_at_max}。"
        )
        action = (
            f"MAX {'ASK' if is_long else 'BID'}: {_fmt(max_entry_exec, pair)} | "
            f"推奨指値(mid): {_fmt(limit_order_price, pair)}"
        )

    return {
        "status": status,
        "max_entry_mid": round(max_entry_mid, dec),
        "max_entry_exec": round(max_entry_exec, dec),
        "limit_order_price": limit_order_price,
        "sl_original": sl,
        "sl_dist_pips": sl_dist_pips,
        "rr_at_signal": rr_at_signal,
        "rr_at_max_entry": rr_at_max,
        "spread_pips": sp_pips,
        "slip_budget_pips": slip_budget_pips,
        "note": note,
        "action_line": action,
    }


def validate_entry_for_result(r: dict) -> dict:
    """
    シグナルresult dict から直接 validate_entry() を呼ぶヘルパー。
    staged_tp が含まれていない場合は {} を返す。

    Args:
        r: signal_scanner の result dict

    Returns:
        validate_entry() の戻り値、または {} (staged_tp 未生成時)
    """
    staged = r.get("staged_tp")
    if not staged:
        return {}
    return validate_entry(
        signal_price=r.get("price", 0),
        sl=staged.get("sl"),
        atr=r.get("atr", 0),
        pair=r.get("pair", ""),
        direction=r.get("direction", "LONG"),
        spread_pips=staged.get("spread_pips_dynamic") or staged.get("spread_pips"),
        tp=staged.get("tp") or staged.get("tp1"),
    )


def format_entry_block(ev: dict, pair: str = "") -> str:
    """
    validate_entry() の結果をメール/通知用テキストブロックに変換する。

    Args:
        ev:   validate_entry() の戻り値
        pair: 通貨ペア（書式化用）

    Returns:
        テキストブロック（複数行）
    """
    if not ev:
        return ""

    status = ev.get("status", "?")
    status_icon = {"ENTER": "✅", "LIMIT": "⚠️", "SKIP": "🚫"}.get(status, "❓")

    lines = [
        "  ┌─ エントリーチェック ─────────────────",
        f"  │ {status_icon} 判定: {status}",
    ]

    if status != "SKIP":
        exec_label = "最大ASK" if ev.get("sl_original") else "最大エントリー"
        lines.append(
            f"  │ 🎯 {exec_label}: {_fmt(ev.get('max_entry_exec'), pair)}"
            f"  (指値推奨: {_fmt(ev.get('limit_order_price'), pair)})"
        )
        budget = ev.get("slip_budget_pips", 0)
        lines.append(
            f"  │ 📏 スリッページ許容: {budget:.1f}pips"
            f" | スプレッド: {ev.get('spread_pips', 0):.1f}pips"
        )
        rr_sig = ev.get("rr_at_signal")
        rr_max = ev.get("rr_at_max_entry")
        if rr_sig is not None:
            rr_str = f"RR シグナル時 1:{rr_sig}"
            if rr_max is not None:
                rr_str += f" → MAX entry時 1:{rr_max}"
            lines.append(f"  │ 📊 {rr_str}")
    else:
        lines.append(f"  │ ❌ {ev.get('note', '')}")

    lines.append(f"  └─ {ev.get('action_line', '')}")
    return "\n".join(lines)


def format_entry_block_short(ev: dict, pair: str = "") -> str:
    """
    1〜2行に圧縮したコンパクト版（Discord embed field 向け）。
    """
    if not ev:
        return ""
    status = ev.get("status", "?")
    icon = {"ENTER": "✅", "LIMIT": "⚠️指値", "SKIP": "🚫"}.get(status, "❓")
    if status == "SKIP":
        return f"{icon} SKIP: {ev.get('note','')[:80]}"
    return (
        f"{icon} MAX {'ASK' if 'ASK' in ev.get('action_line','') else 'entry'}: "
        f"**{_fmt(ev.get('max_entry_exec'), pair)}** | "
        f"指値: {_fmt(ev.get('limit_order_price'), pair)} | "
        f"RR: 1:{ev.get('rr_at_signal','?')}→1:{ev.get('rr_at_max_entry','?')}"
    )
