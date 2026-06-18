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
PAIR_EXCLUDE = frozenset(["INRJPY", "TRYJPY", "EURUSD", "USDCHF"])

# 静的★調整: バックテスト勝率が明確に良/悪で、closed_tradesが少ない段階でも反映
# 値は adjustment (整数 or 0.5刻み)。build_pair_performance_mapの実績値とマージ
PAIR_STATIC_BASELINE = {
    # 🏆 主力ペア昇格（76.9%勝率）
    "SGDJPY": {"adjustment": +1,  "note": "主力ペア(実証76.9%)"},
    "EURAUD": {"adjustment": +1,  "note": "主力ペア(実証76.9%)"},
    # ✅ 好成績維持（69-72%）
    "AUDJPY": {"adjustment": +1,  "note": "好成績(実証72.2%)"},
    "GBPJPY": {"adjustment": +1,  "note": "好成績(実証69.2%)"},
    # ❌ 慢性不振ペア（40%以下）→ PAIR_EXCLUDEに移動（ハードブロック）
    # "EURUSD": {"adjustment": -1,  "note": "不振ペア(実証40.0%)"},  # 除外済み
    # "USDCHF": {"adjustment": -1,  "note": "不振ペア(実証37.5%)"},  # 除外済み
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
