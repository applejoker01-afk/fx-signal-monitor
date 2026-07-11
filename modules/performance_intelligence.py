"""
performance_intelligence.py
パフォーマンス知能モジュール

⑩ 自己学習型シグナル重み付け（closed_tradesの実績で信頼度調整）
⑪ ドローダウン監視・連敗クールダウン
⑬ 相場局面判定（トレンド/レンジ）
"""

from datetime import datetime, timedelta, timezone


# ============================================================
# バックテスト実証済み ペア静的ベースライン（2026-06-09 180日実績）
# ============================================================

# 完全除外ペア: 流動性・政治リスク or バックテスト実証済み慢性不振で構造的に取引不可
# 2026-06-18: EURUSD(40%)/USDCHF(37.5%) を不振ペアのためハードブロックに昇格
#              ソフトブロック(-1調整)では★4シグナルが届いてしまい実際に取引されていた
# 2026-06-19: NZDJPY(33%・3件)/CADJPY(0%・2件) を追加
#              autoresearch実証: BOJ引き締め + RBNZ/BOC政策でダブル逆風。
#              サンプルは少ないが、マクロ構造（RBNZ ease + JPY strong）で根拠十分。
#              NZDJPY・AUDJPY は高相関のため、NZDJPY除外で AUDJPY を集中管理する。
PAIR_EXCLUDE = frozenset([
    "INRJPY", "TRYJPY",           # 流動性・政治リスク（当初から）
    "EURUSD", "USDCHF",           # 慢性不振（実証40%/37.5%）2026-06-18追加
    "NZDJPY", "CADJPY",           # BOJ局面でダブル逆風（実証33%/0%）2026-06-19追加
])

# 静的★調整: バックテスト勝率が明確に良/悪で、closed_tradesが少ない段階でも反映
# 値は adjustment (整数 or 0.5刻み)。build_pair_performance_mapの実績値とマージ
PAIR_STATIC_BASELINE = {
    # 🏆 主力ペア昇格（76.9%勝率）
    "SGDJPY": {"adjustment": +1,  "note": "主力ペア(実証76.9%)"},
    "EURAUD": {"adjustment": +1,  "note": "主力ペア(実証76.9%)"},
    # ✅ 好成績維持（69-72%）
    # AUDJPY: +1はSHORT方向のみ有効。LONG方向は apply_boj_cycle_directional_filter が
    # AUD(ease/stable) + JPY(pause) の組み合わせで NO_TRADE にハードブロック済み（2026-07-02確認）
    "AUDJPY": {"adjustment": +1,  "note": "好成績(実証72.2%) ※LONGはBOJサイクルフィルタでブロック済み"},
    "GBPJPY": {"adjustment": +1,  "note": "好成績(実証69.2%)"},
    # ❌ 慢性不振ペア（40%以下）→ PAIR_EXCLUDEに移動（ハードブロック）
    # "EURUSD": {"adjustment": -1,  "note": "不振ペア(実証40.0%)"},  # 除外済み
    # "USDCHF": {"adjustment": -1,  "note": "不振ペア(実証37.5%)"},  # 除外済み
    # "NZDJPY": {"adjustment": -1,  "note": "不振ペア(実証33%)"},     # 除外済み
    # "CADJPY": {"adjustment": -1,  "note": "不振ペア(実証0%)"},      # 除外済み
}


def apply_static_baseline(perf_map: dict) -> dict:
    """
    closed_tradesの実績マップに静的ベースラインをマージする。
    実績データがある場合は adjustment を合算（ただし -2〜+2 でクランプ）。
    実績データがないペアには静的値をそのまま追加。
    """
    for pair, static in PAIR_STATIC_BASELINE.items():
        if pair in perf_map:
            # 実績あり: adjustmentを加算（累積しすぎないようクランプ）
            combined = perf_map[pair]["adjustment"] + static["adjustment"]
            perf_map[pair]["adjustment"] = max(-2, min(2, combined))
            perf_map[pair]["note"] = perf_map[pair]["note"] + " / " + static["note"]
        else:
            # 実績なし: 静的値をそのまま登録
            perf_map[pair] = {
                "win_rate": None,
                "total": 0,
                "adjustment": static["adjustment"],
                "note": static["note"] + " (静的ベースライン)",
            }
    return perf_map


# ============================================================
# BOJ引き締めサイクル 方向性レジームフィルタ（2026-06-19追加）
# autoresearch: BOJ tightening + 対通貨ease = JPYクロスLONGは構造的逆風
# ============================================================

# JPYがピーク/引き締め継続中と判断するスタンスセット
_JPY_STRONG_STANCES = frozenset(["tighten", "pause"])  # pause=一時停止だがまだ高金利

# 対通貨が"ease"かつ積極的に利下げ中 → LONGは強くブロック
_CCY_EASE_HARD = frozenset(["ease"])                  # stance=="ease"
_CCY_EASE_HARD_MOMENTUM = frozenset(["stable", "accelerating"])  # ease方向が定着
# ease+deceleratingは利下げ減速中（底打ち近し）→ 軽めのペナルティ
_CCY_EASE_SOFT_MOMENTUM = frozenset(["decelerating"])
# trough（底打ち）は次が利上げ → ブロック不要
_CCY_EASE_NO_BLOCK_MOMENTUM = frozenset(["trough"])


def apply_boj_cycle_directional_filter(result: dict, cb_rates: dict) -> dict:
    """
    BOJ引き締めサイクル局面フィルタ: JPYクロスペアの方向性を中銀スタンスで制限する。

    対象: JPYを含む全クロスペア（XXX/JPY 形式）
    条件: JPY スタンスが強い（tighten/pause） AND 対通貨が ease中
    効果:
      - ease + stable/accelerating → LONGを★2にハードブロック（期待値マイナス）
      - ease + decelerating        → LONG★を1段降格（利下げ減速中で完全ブロックは過剰）
    SHORTシグナルには影響しない（BOJ局面ではSHORT有利のため）。

    実取引根拠（2026/5/26〜6/18, 66件）:
      ロング 37%勝率 vs ショート 57%勝率
      AUDJPY ロング -106.6pips（最大損失ペア）
      NZDJPY ロング -90.7pips（最大単発損失）

    Returns:
        result dict with optional added fields:
          regime_filter_applied: bool
          regime_filter_reason: str
    """
    pair = result.get("pair", "")
    direction = result.get("direction", "")

    # JPYクロスかどうか判定（XXJPY or JPY含む）
    if "JPY" not in pair:
        return result

    # LONG系シグナルのみが対象（SHORT/NO_TRADE/WAIT系はスルー）
    if "LONG" not in direction:
        return result

    # cb_rates の形式を正規化（"rates" キーがある場合とフラットな場合の両方に対応）
    rates_data = cb_rates.get("rates", cb_rates) if isinstance(cb_rates, dict) else {}

    # JPYのスタンスを取得
    jpy_info = rates_data.get("JPY", {})
    jpy_stance = jpy_info.get("stance", "neutral")
    if jpy_stance not in _JPY_STRONG_STANCES:
        # JPYが中立 or easeなら円高圧力がないためフィルタ不要
        return result

    # 対通貨（非JPY側）を特定
    # PAIR_API形式: AUDJPY → ("AUD", "JPY"), SGDJPY → ("SGD", "JPY")
    # JPYが後ろにあるパターン（XXX/JPY）の場合、FROM通貨 = pair[:3]
    # JPYが前にあるパターン（JPY/XXX）はPAIR_APIに存在しないため考慮不要
    non_jpy_ccy = pair.replace("JPY", "")
    if len(non_jpy_ccy) != 3:
        return result

    other_info = rates_data.get(non_jpy_ccy, {})
    other_stance = other_info.get("stance", "neutral")
    other_momentum = other_info.get("rate_momentum", "stable")

    if other_stance not in _CCY_EASE_HARD:
        # 対通貨が ease でない → フィルタ不要（neutral/tighten は問題なし）
        return result

    if other_momentum in _CCY_EASE_NO_BLOCK_MOMENTUM:
        # trough（底打ち）= もうすぐ利上げ転換 → ブロック不要
        return result

    cb_other = other_info.get("cb_name", non_jpy_ccy)
    cb_jpy = jpy_info.get("cb_name", "日銀")

    if other_momentum in _CCY_EASE_HARD_MOMENTUM:
        # ease + stable/accelerating = 積極利下げ中 → ハードブロック（★2固定）
        original_stars = result.get("stars", 1)
        result["stars"] = min(2, original_stars)  # ★2以下に抑制（元が★1ならそのまま）
        reason = (
            f"⚠️ BOJサイクルフィルタ: {non_jpy_ccy}({cb_other} ease/{other_momentum}) + "
            f"JPY({cb_jpy} {jpy_stance}) = LONGはダブル逆風"
        )
        result["verdict"] = f"🔻 {reason.lstrip('⚠️ ')}"
        result["direction"] = "NO_TRADE"  # 取引しない
        result["regime_filter_applied"] = True
        result["regime_filter_reason"] = reason
    elif other_momentum in _CCY_EASE_SOFT_MOMENTUM:
        # ease + decelerating = 利下げ減速中 → ★1段降格（見送り推奨だが禁止ではない）
        original_stars = result.get("stars", 1)
        new_stars = max(1, original_stars - 1)
        reason = (
            f"⚠️ BOJサイクル軽警告: {non_jpy_ccy}({cb_other} ease/{other_momentum}) + "
            f"JPY({cb_jpy} {jpy_stance}) = LONG方向注意"
        )
        if new_stars != original_stars:
            result["stars"] = new_stars
            result["regime_filter_applied"] = True
            result["regime_filter_reason"] = reason

    return result


# ============================================================
# VIXレジームフィルタ（2026-06-22追加）
# autoresearch: wiki/finance/vix-fx-signal-filter.md
# VIX×JPY安全資産 — キャリー崩壊リスクをセンチメントモードで判定
# 参考: 2024年8月5日 VIX日中~65・USD/JPY -14%・日経 -12.4%
# ============================================================

def apply_vix_regime_filter(result: dict, sentiment: dict) -> dict:
    """
    VIXレジームフィルタ: 高VIX局面でJPYクロスのLONGをブロック/降格する。

    sentiment["risk_mode"] の値 (sentiment_monitor.py で定義):
      panic     → VIX > 30 : JPYクロスLONG 完全ブロック（NO_TRADE, ★≤2）
      risk_off  → VIX > 25 : JPYクロスLONG ★1段降格
      caution   → VIX > 20 : 警告のみ（★変更なし）
      normal / complacent : フィルタなし

    SHORTシグナルは対象外（キャリー崩壊時のJPY急騰はSHORTに追い風）。
    """
    if not sentiment:
        return result

    risk_mode = sentiment.get("risk_mode", "normal")
    vix_value = sentiment.get("vix")
    pair = result.get("pair", "")
    direction = result.get("direction", "")

    # LONG系シグナルのみ対象
    if "LONG" not in direction:
        return result

    # JPYペア以外はスルー（将来の拡張余地として残す）
    if "JPY" not in pair:
        return result

    vix_str = f"VIX={vix_value:.1f}" if vix_value else "VIX高水準"

    if risk_mode == "panic":
        # VIX > 30 = 2024年8月型キャリー崩壊リスク → ハードブロック
        result["stars"] = min(2, result.get("stars", 1))
        result["direction"] = "NO_TRADE"
        result["vix_filter_applied"] = True
        result["vix_filter_reason"] = (
            f"VIXパニックフィルタ: {vix_str} — "
            f"キャリー崩壊リスク: JPYクロスLONG禁止"
        )

    elif risk_mode == "risk_off":
        # VIX > 25 = キャリー不安定化 → ★1段降格
        original_stars = result.get("stars", 1)
        new_stars = max(1, original_stars - 1)
        if new_stars != original_stars:
            result["stars"] = new_stars
            result["vix_filter_applied"] = True
            result["vix_filter_reason"] = (
                f"VIXリスクオフフィルタ: {vix_str} — "
                f"JPYクロスLONG -{original_stars - new_stars}★降格"
            )

    elif risk_mode == "caution":
        # VIX > 20 = 警告のみ（★変更なし、情報付与のみ）
        result["vix_caution"] = True
        result["vix_caution_reason"] = (
            f"VIX警戒域: {vix_str} — JPYクロスLONGは要注意"
        )

    return result


# ============================================================
# 💸 スプレッド/ATR比フィルタ（2026-06-23 追加）
# ============================================================

def apply_spread_filter(result: dict) -> dict:
    """
    スプレッド/ATR 比に応じてエキゾチック系シグナルを降格する。

    根拠: Frankfurter API は中値(mid)のみ提供のため、bid/ask スプレッドが
    広い通貨では、実際の ASK エントリーから見ると SL に既に近い状態となる。
    特に ZARJPY/TRYJPY/INRJPY/MXNJPY などエキゾチック系は致命的。

    閾値:
      spread/ATR > 30%  → ★≤2 強制（実質取引禁止）
      spread/ATR > 10%  → ★≤3 上限（エントリー注意）
      spread/ATR ≤ 10%  → 影響軽微・降格なし

    staged_tp['spread_atr_ratio'] を見て判定する。
    """
    staged = result.get("staged_tp") or {}
    ratio = staged.get("spread_atr_ratio")
    spread_pips = staged.get("spread_pips", 0)
    if ratio is None or ratio == 0:
        return result

    pair = result.get("pair", "")
    rr_eff = staged.get("rr_effective")
    rr_mid = staged.get("rr_tp")

    if ratio > 0.30:
        # 致命的スプレッド: ★≤2 強制
        original_stars = result.get("stars", 1)
        new_stars = min(2, original_stars)
        if new_stars != original_stars:
            result["stars"] = new_stars
            result["spread_filter_applied"] = True
            result["spread_filter_reason"] = (
                f"💸 スプレッド致命的: {pair} spread={spread_pips:.1f}pips "
                f"({ratio*100:.0f}% of ATR) — 実効RR 1:{rr_mid}→1:{rr_eff} に劣化。"
                f"取引非推奨で★{original_stars}→★{new_stars}降格。"
            )
    elif ratio > 0.10:
        # スプレッド広い: ★≤3 上限
        original_stars = result.get("stars", 1)
        new_stars = min(3, original_stars)
        if new_stars != original_stars:
            result["stars"] = new_stars
            result["spread_filter_applied"] = True
            result["spread_filter_reason"] = (
                f"💸 スプレッド広め: {pair} spread={spread_pips:.1f}pips "
                f"({ratio*100:.0f}% of ATR) — 実効RR 1:{rr_mid}→1:{rr_eff}。"
                f"★{original_stars}→★{new_stars}降格。"
            )
        else:
            # 元から★3以下でも警告だけ残す
            result["spread_caution"] = True
            result["spread_caution_reason"] = (
                f"💸 スプレッド広め: {pair} spread={spread_pips:.1f}pips "
                f"({ratio*100:.0f}% of ATR)。実効RR 1:{rr_eff}"
            )

    return result


# ============================================================
# ⑩ 自己学習型シグナル重み付け
# ============================================================

def build_pair_performance_map(closed_trades: list, min_trades: int = 5) -> dict:
    """
    決済済みトレードから、ペアごとの実績勝率を集計して
    信頼度調整マップを作る。

    Returns:
        {
          "AUDJPY": {"win_rate": 67.0, "total": 18, "adjustment": +0.5, "note": "実績良好"},
          "TRYJPY": {"win_rate": 38.0, "total": 15, "adjustment": -1, "note": "実績不振"},
        }
    """
    pair_stats = {}
    for t in closed_trades:
        pair = t.get("pair")
        if not pair:
            continue
        if pair not in pair_stats:
            pair_stats[pair] = {"wins": 0, "total": 0, "pips": 0.0}
        pair_stats[pair]["total"] += 1
        if t.get("result") == "WIN":
            pair_stats[pair]["wins"] += 1
        pair_stats[pair]["pips"] += t.get("pips", 0) or 0

    perf_map = {}
    for pair, s in pair_stats.items():
        if s["total"] < min_trades:
            # サンプル数が少なすぎる場合は調整なし
            perf_map[pair] = {
                "win_rate": round(s["wins"] / s["total"] * 100, 1) if s["total"] else 0,
                "total": s["total"],
                "adjustment": 0,
                "note": f"サンプル不足({s['total']}件)",
            }
            continue

        win_rate = s["wins"] / s["total"] * 100

        # 勝率に応じた★調整
        if win_rate >= 65:
            adjustment = 1
            note = "実績優秀"
        elif win_rate >= 55:
            adjustment = 0.5
            note = "実績良好"
        elif win_rate >= 45:
            adjustment = 0
            note = "実績平均"
        elif win_rate >= 35:
            adjustment = -1
            note = "実績不振"
        else:
            adjustment = -2
            note = "実績低調・要注意"

        perf_map[pair] = {
            "win_rate": round(win_rate, 1),
            "total": s["total"],
            "total_pips": round(s["pips"], 4),
            "adjustment": adjustment,
            "note": note,
        }

    return perf_map


def apply_performance_weighting(result: dict, perf_map: dict) -> dict:
    """
    シグナルに実績ベースの信頼度調整を適用。
    ★を増減させ、resultにperformance情報を付与。
    """
    pair = result.get("pair")
    if pair not in perf_map:
        return result

    perf = perf_map[pair]
    adjustment = perf.get("adjustment", 0)

    # ★4以上のシグナルにのみ調整を適用（誤シグナルの増幅を防ぐ）
    if result.get("stars", 0) >= 4 and adjustment != 0:
        original = result["stars"]
        # adjustmentは0.5刻みだが★は整数なので四捨五入的に適用
        new_stars = original + adjustment
        new_stars = max(1, min(5, round(new_stars)))
        if new_stars != original:
            result["stars"] = int(new_stars)
            result["performance_adjusted"] = True

    result["performance"] = {
        "win_rate": perf.get("win_rate"),
        "total_trades": perf.get("total"),
        "adjustment": adjustment,
        "note": perf.get("note"),
    }
    return result


# ============================================================
# ⑪ ドローダウン監視・連敗クールダウン
# ============================================================

def check_drawdown_alert(closed_trades: list, recent_n: int = 5) -> dict:
    """
    直近の決済トレードから連敗・ドローダウンを検知。

    Returns:
        {
          "alert": True/False,
          "level": "warning" | "critical" | "none",
          "recent_losses": 4,
          "recent_total": 5,
          "consecutive_losses": 3,
          "recent_pips": -8.5,
          "message": "...",
          "recommendation": "...",
        }
    """
    if not closed_trades:
        return {"alert": False, "level": "none"}

    # 決済時刻でソート（新しい順）
    sorted_trades = sorted(
        closed_trades,
        key=lambda t: t.get("exit_time", ""),
        reverse=True
    )

    recent = sorted_trades[:recent_n]
    if len(recent) < 3:
        return {"alert": False, "level": "none", "recent_total": len(recent)}

    recent_losses = sum(1 for t in recent if t.get("result") == "LOSS")
    recent_pips = sum(t.get("pips", 0) or 0 for t in recent)

    # 連続損失をカウント（最新から）
    consecutive_losses = 0
    for t in sorted_trades:
        if t.get("result") == "LOSS":
            consecutive_losses += 1
        else:
            break

    # 判定
    alert = False
    level = "none"
    message = ""
    recommendation = ""

    if consecutive_losses >= 4:
        alert = True
        level = "critical"
        message = f"🚨 {consecutive_losses}連敗中"
        recommendation = "相場環境が戦略と不一致の可能性。48時間の新規エントリー停止を強く推奨"
    elif consecutive_losses >= 3:
        alert = True
        level = "warning"
        message = f"⚠ {consecutive_losses}連敗中"
        recommendation = "24時間の新規エントリー見送りを推奨。相場局面を再確認"
    elif recent_losses >= 4 and len(recent) >= 5:
        alert = True
        level = "warning"
        message = f"⚠ 直近{len(recent)}件中{recent_losses}件が損失"
        recommendation = "勝率が低下中。ロットを半減するか一時休止を検討"

    return {
        "alert": alert,
        "level": level,
        "recent_losses": recent_losses,
        "recent_total": len(recent),
        "consecutive_losses": consecutive_losses,
        "recent_pips": round(recent_pips, 4),
        "message": message,
        "recommendation": recommendation,
    }


# ============================================================
# ⑬ 相場局面判定（トレンド/レンジ）
# ============================================================

def detect_market_regime(prices: list, period: int = 14) -> dict:
    """
    ADX的な指標で「トレンド相場かレンジ相場か」を判定。

    簡易ADX計算:
      +DM, -DM から方向性指数を求め、トレンドの強さを0〜100で表す。

    Returns:
        {
          "adx": 32.5,
          "regime": "trending" | "ranging" | "weak_trend",
          "regime_label": "トレンド相場",
          "trend_direction": "up" | "down" | "neutral",
          "note": "順張りシグナル有効",
        }
    """
    if not prices or len(prices) < period * 2:
        return {"adx": None, "regime": "unknown", "regime_label": "判定不可"}

    # True Range, +DM, -DM の計算
    plus_dm = []
    minus_dm = []
    tr = []

    for i in range(1, len(prices)):
        high = max(prices[i], prices[i - 1])
        low = min(prices[i], prices[i - 1])
        up_move = prices[i] - prices[i - 1]
        down_move = prices[i - 1] - prices[i]

        plus_dm.append(up_move if (up_move > down_move and up_move > 0) else 0)
        minus_dm.append(down_move if (down_move > up_move and down_move > 0) else 0)
        tr.append(high - low if high != low else abs(prices[i] - prices[i - 1]) or 0.0001)

    def smooth(arr, p):
        if len(arr) < p:
            return sum(arr) / len(arr) if arr else 0
        return sum(arr[-p:]) / p

    atr = smooth(tr, period)
    if atr == 0:
        return {"adx": None, "regime": "unknown", "regime_label": "判定不可"}

    plus_di = (smooth(plus_dm, period) / atr) * 100
    minus_di = (smooth(minus_dm, period) / atr) * 100

    di_sum = plus_di + minus_di
    if di_sum == 0:
        adx = 0
    else:
        dx = abs(plus_di - minus_di) / di_sum * 100
        adx = dx  # 簡易版（本来はDXの移動平均）

    # トレンド方向
    if plus_di > minus_di * 1.1:
        trend_direction = "up"
    elif minus_di > plus_di * 1.1:
        trend_direction = "down"
    else:
        trend_direction = "neutral"

    # 局面判定
    if adx >= 30:
        regime = "trending"
        regime_label = "トレンド相場"
        note = "順張りシグナル有効"
    elif adx >= 20:
        regime = "weak_trend"
        regime_label = "弱トレンド相場"
        note = "順張りやや有効"
    else:
        regime = "ranging"
        regime_label = "レンジ相場"
        note = "順張りは機能しにくい・逆張り向き"

    return {
        "adx": round(adx, 1),
        "plus_di": round(plus_di, 1),
        "minus_di": round(minus_di, 1),
        "regime": regime,
        "regime_label": regime_label,
        "trend_direction": trend_direction,
        "note": note,
    }
