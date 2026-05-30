"""
ambush_alert.py
待ち伏せ型アラート（代替案1）

リアルタイム監視の代わりに「重要価格に接近したら通知」する。
看護師の勤務形態（張り付けない）に最適化。

理論「上位足POI（関心領域）で待つ」を、人間が画面に張り付かず実現する。

重要価格（POI: Point of Interest）:
  ・50 EMA / 200 EMA（押し目買い・戻り売りの動的サポレジ）
  ・サポート / レジスタンス（advanced_analyticsで検出済みを流用）
  ・前日高値 / 前日安値
  ・ピボットポイント（PP / R1 / S1）

接近判定:
  現在価格が重要価格の「接近閾値（ATRベース）」以内に入ったらアラート。
"""


def calc_ema(prices, period):
    """指数移動平均（最新値）"""
    if len(prices) < period:
        return None
    k = 2 / (period + 1)
    ema = sum(prices[:period]) / period
    for p in prices[period:]:
        ema = p * k + ema * (1 - k)
    return ema


def calc_pivot_points(prev_high, prev_low, prev_close):
    """
    クラシック・ピボットポイント計算。
    前日の高値・安値・終値から当日の重要価格帯を算出。
    """
    pp = (prev_high + prev_low + prev_close) / 3
    r1 = 2 * pp - prev_low
    s1 = 2 * pp - prev_high
    r2 = pp + (prev_high - prev_low)
    s2 = pp - (prev_high - prev_low)
    return {
        "pp": pp, "r1": r1, "s1": s1, "r2": r2, "s2": s2
    }


def build_poi_levels(pair, price, prices, support_resistance=None):
    """
    1通貨ペアの重要価格（POI）一覧を構築。

    Returns:
        [
          {"type": "200EMA", "price": 158.4, "role": "動的サポート", "kind": "support"},
          {"type": "前日安値", "price": 157.8, "role": "サポート", "kind": "support"},
          ...
        ]
    """
    levels = []

    if not prices or len(prices) < 50:
        return levels

    # ── 移動平均（動的サポレジ）──
    ema50 = calc_ema(prices, 50)
    ema200 = calc_ema(prices, 200) if len(prices) >= 200 else None

    if ema50:
        kind = "support" if price > ema50 else "resistance"
        role = "押し目候補(50EMA)" if kind == "support" else "戻り候補(50EMA)"
        levels.append({"type": "50EMA", "price": round(ema50, 5),
                       "role": role, "kind": kind})
    if ema200:
        kind = "support" if price > ema200 else "resistance"
        role = "強サポート(200EMA)" if kind == "support" else "強レジスタンス(200EMA)"
        levels.append({"type": "200EMA", "price": round(ema200, 5),
                       "role": role, "kind": kind})

    # ── 前日高安（直近の足から推定）──
    # prices は日足なので、直近2本が「前日」「当日」に近い
    if len(prices) >= 2:
        prev_high = max(prices[-2], prices[-1])
        prev_low = min(prices[-2], prices[-1])
        # より正確には直近5本のレンジを使う
        recent5 = prices[-6:-1] if len(prices) >= 6 else prices[:-1]
        if recent5:
            prev_high = max(recent5)
            prev_low = min(recent5)
            prev_close = prices[-1]

            # ピボット
            piv = calc_pivot_points(prev_high, prev_low, prev_close)
            for key, label in [("r1", "R1"), ("s1", "S1"), ("pp", "PP")]:
                pv = piv[key]
                kind = "support" if pv < price else "resistance"
                levels.append({"type": f"ピボット{label}", "price": round(pv, 5),
                               "role": f"ピボット{label}", "kind": kind})

    # ── サポレジ（advanced_analyticsの検出結果を流用）──
    if support_resistance:
        for r in (support_resistance.get("resistance") or [])[:2]:
            levels.append({"type": "レジスタンス", "price": r["price"],
                           "role": f"{r.get('label','レジスタンス')}", "kind": "resistance"})
        for s in (support_resistance.get("support") or [])[:2]:
            levels.append({"type": "サポート", "price": s["price"],
                           "role": f"{s.get('label','サポート')}", "kind": "support"})

    return levels


def check_ambush_proximity(pair, price, prices, atr,
                            support_resistance=None,
                            threshold_atr=0.5):
    """
    現在価格が重要価格（POI）に接近しているか判定。

    Args:
        threshold_atr: 接近とみなすATRの倍率（0.5 = ATRの半分以内なら接近）

    Returns:
        {
          "has_alert": True/False,
          "nearest": {...},          # 最も近いPOI
          "approaching": [...],      # 接近中の全POI
          "message": "...",
        }
    """
    if not atr or atr == 0 or not prices:
        return {"has_alert": False, "approaching": []}

    levels = build_poi_levels(pair, price, prices, support_resistance)
    if not levels:
        return {"has_alert": False, "approaching": []}

    threshold = atr * threshold_atr
    approaching = []

    for lv in levels:
        distance = abs(price - lv["price"])
        if distance <= threshold:
            dist_pct = distance / price * 100
            approaching.append({
                **lv,
                "distance": round(distance, 5),
                "distance_pct": round(dist_pct, 3),
            })

    if not approaching:
        return {"has_alert": False, "approaching": []}

    # 距離が近い順にソート
    approaching.sort(key=lambda x: x["distance"])
    nearest = approaching[0]

    # メッセージ生成
    direction_hint = ""
    if nearest["kind"] == "support":
        direction_hint = "→ 反発でのロング候補"
    else:
        direction_hint = "→ 反落でのショート候補"

    message = (
        f"{nearest['role']}（{nearest['price']}）に接近 "
        f"あと{nearest['distance_pct']:.2f}% {direction_hint}"
    )

    return {
        "has_alert": True,
        "nearest": nearest,
        "approaching": approaching,
        "message": message,
    }


def evaluate_ambush(result, prices, atr_threshold=0.5):
    """
    シグナル結果に待ち伏せアラート情報を付与。
    signal_scannerの評価ループから呼ぶ。
    """
    pair = result.get("pair")
    price = result.get("price")
    atr = result.get("atr")
    sr = result.get("support_resistance")

    ambush = check_ambush_proximity(
        pair, price, prices, atr,
        support_resistance=sr,
        threshold_atr=atr_threshold,
    )
    result["ambush"] = ambush

    # ★4シグナル + 重要価格接近 = 高確度ゾーン判定
    if ambush.get("has_alert") and result.get("stars", 0) >= 4:
        nearest = ambush["nearest"]
        # シグナル方向とPOIの種類が整合するか
        direction = result.get("direction", "")
        is_long = direction.endswith("LONG")
        # ロングなら support 接近、ショートなら resistance 接近が理想
        if (is_long and nearest["kind"] == "support") or \
           (not is_long and nearest["kind"] == "resistance"):
            result["high_confidence_zone"] = True
            result["ambush_quality"] = "整合（シグナル方向とPOIが一致）"
        else:
            result["high_confidence_zone"] = False
            result["ambush_quality"] = "注意（シグナルとPOIが逆方向）"

    return result


def collect_ambush_alerts(all_results):
    """
    全ペアから待ち伏せアラートを集約。
    通知用にまとめる。

    Returns:
        {
          "high_confidence": [...],  # ★4+POI整合の最重要
          "approaching": [...],      # POI接近中（シグナル不問）
        }
    """
    high_confidence = []
    approaching = []

    for r in all_results:
        ambush = r.get("ambush", {})
        if not ambush.get("has_alert"):
            continue

        if r.get("high_confidence_zone"):
            high_confidence.append({
                "pair": r["pair"],
                "label": r.get("label", r["pair"]),
                "price": r["price"],
                "stars": r.get("stars", 0),
                "direction": r.get("direction", ""),
                "nearest": ambush["nearest"],
                "quality": r.get("ambush_quality", ""),
            })
        else:
            # ★4未満でもPOI接近は「待ち伏せ候補」として拾う
            approaching.append({
                "pair": r["pair"],
                "label": r.get("label", r["pair"]),
                "price": r["price"],
                "stars": r.get("stars", 0),
                "nearest": ambush["nearest"],
            })

    # 重要度順
    high_confidence.sort(key=lambda x: -x["stars"])
    approaching.sort(key=lambda x: x["nearest"]["distance_pct"])

    return {
        "high_confidence": high_confidence,
        "approaching": approaching[:8],  # 上位8件まで
    }
