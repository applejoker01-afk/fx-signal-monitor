"""
advanced_analytics.py
高度分析モジュール - 8機能のうち7機能を統合

① 通貨強弱メーター
② ボラティリティ・レジーム判定
③ 段階的TP計算
④ 複数ポジション相関リスク警告
⑤ キャリートレード魅力度スコア
⑥ サポート・レジスタンス自動検出
⑧ 日銀介入リスクインジケーター
"""

import math
from datetime import datetime, timezone

from modules.spread_monitor import get_dynamic_spread_pips, get_dynamic_spread_metadata


# ============================================================
# ① 通貨強弱メーター
# ============================================================

def calc_currency_strength(all_pair_prices: dict, pair_api: dict) -> dict:
    """
    各通貨の総合強弱スコアを算出（-100 〜 +100）。

    仕組み:
      すべての通貨ペアの方向性（base通貨が強いか弱いか）を集計し、
      各通貨が他通貨に対して平均的にどれだけ強いかを数値化。

    Args:
        all_pair_prices: {pair: {"current": float, "prices_30d": [float]}}
        pair_api: {"USDJPY": ("USD","JPY"), ...}

    Returns:
        {
          "USD": {"score": 65.2, "rank": 1, "label": "強い"},
          "JPY": {"score": -30.1, "rank": 8, "label": "弱い"},
          ...
        }
    """
    currency_wins = {}   # 各通貨の「勝ち」カウント（強い方向に動いた）
    currency_total = {}  # 各通貨が関わったペア数

    for pair, (base, quote) in pair_api.items():
        if pair not in all_pair_prices:
            continue
        prices = all_pair_prices[pair].get("prices_30d", [])
        if len(prices) < 5:
            continue

        # 30日前比の変化率
        change_pct = (prices[-1] - prices[0]) / prices[0] * 100

        for ccy in [base, quote]:
            currency_total[ccy] = currency_total.get(ccy, 0) + 1

        if change_pct > 0:
            # base通貨が強い
            currency_wins[base] = currency_wins.get(base, 0) + abs(change_pct)
            currency_wins[quote] = currency_wins.get(quote, 0) - abs(change_pct)
        elif change_pct < 0:
            # quote通貨が強い
            currency_wins[quote] = currency_wins.get(quote, 0) + abs(change_pct)
            currency_wins[base] = currency_wins.get(base, 0) - abs(change_pct)

    # 正規化（最大絶対値で割って-100〜+100にスケール）
    scores_raw = {}
    for ccy, total in currency_total.items():
        if total > 0:
            scores_raw[ccy] = currency_wins.get(ccy, 0) / total

    if not scores_raw:
        return {}

    max_abs = max(abs(v) for v in scores_raw.values()) or 1
    scores = {}
    for ccy, raw in scores_raw.items():
        score = round(raw / max_abs * 100, 1)
        if score >= 50:
            label = "強い"
        elif score >= 20:
            label = "やや強い"
        elif score >= -20:
            label = "中立"
        elif score >= -50:
            label = "やや弱い"
        else:
            label = "弱い"
        scores[ccy] = {"score": score, "label": label}

    # ランク付け（スコア降順）
    ranked = sorted(scores.keys(), key=lambda c: -scores[c]["score"])
    for rank, ccy in enumerate(ranked, 1):
        scores[ccy]["rank"] = rank

    return scores


def get_pair_strength_context(pair: str, pair_api: dict, strength: dict) -> dict:
    """
    特定ペアの通貨強弱コンテキストを返す。
    例: USDJPY → base=USD(+65), quote=JPY(-30) → 両方向一致でロング有利
    """
    if not strength or pair not in pair_api:
        return {}

    base, quote = pair_api[pair]
    base_info = strength.get(base, {})
    quote_info = strength.get(quote, {})

    base_score = base_info.get("score", 0)
    quote_score = quote_info.get("score", 0)
    spread = base_score - quote_score

    if spread >= 40:
        context = "強い通貨 vs 弱い通貨（ロング有利）"
        bias = "long"
    elif spread >= 15:
        context = "やや強い通貨 vs 弱め（ロング寄り）"
        bias = "slight_long"
    elif spread <= -40:
        context = "弱い通貨 vs 強い通貨（ショート有利）"
        bias = "short"
    elif spread <= -15:
        context = "やや弱い通貨 vs 強め（ショート寄り）"
        bias = "slight_short"
    else:
        context = "通貨強弱が拮抗（中立）"
        bias = "neutral"

    return {
        "base_currency": base,
        "quote_currency": quote,
        "base_score": base_score,
        "base_label": base_info.get("label", "?"),
        "base_rank": base_info.get("rank", 0),
        "quote_score": quote_score,
        "quote_label": quote_info.get("label", "?"),
        "quote_rank": quote_info.get("rank", 0),
        "spread": round(spread, 1),
        "context": context,
        "bias": bias,
    }


# ============================================================
# ② ボラティリティ・レジーム判定
# ============================================================

def detect_volatility_regime(prices: list, atr_current: float) -> dict:
    """
    現在のATRが過去90日ATRに対してどのくらい大きいかを判定し、
    最適なATR乗数を動的に返す。

    Returns:
        {
          "regime": "high" | "normal" | "low",
          "regime_label": "高ボラ相場",
          "atr_ratio": 1.45,         # 現在ATR / 90日平均ATR
          "sl_multiplier": 4.0,      # 推奨SL ATR乗数
          "tp1_multiplier": 4.0,     # TP1 ATR乗数
          "tp2_multiplier": 8.0,     # TP2 ATR乗数
          "tp3_multiplier": 14.0,    # TP3 ATR乗数（中長期目標）
          "note": "高ボラのためSL幅を自動拡大"
        }
    """
    if not prices or atr_current is None or atr_current == 0:
        return _default_regime()

    # 90日分の日次ATRを計算（期間が短ければある分だけ使う）
    n = min(len(prices) - 1, 90)
    if n < 14:
        return _default_regime()

    recent = prices[-n - 1:]
    daily_tr = [abs(recent[i] - recent[i - 1]) for i in range(1, len(recent))]
    if not daily_tr:
        return _default_regime()

    atr_90d = sum(daily_tr) / len(daily_tr)
    if atr_90d == 0:
        return _default_regime()

    ratio = atr_current / atr_90d

    # 2026-06-22 Phase1出口戦略改善（autoresearch: wiki/finance/fx-exit-strategy-fundamentals.md）
    # TP = 3.0×ATR → 1.5×ATR（到達率 6.5% → 30-50% 目標）
    # 根拠: 567,000バックテスト研究で TP=3×ATRはほぼ到達不能と判明（実取引62件で確認）
    # SLは据え置き（3.0×ATR → リスク管理は変更なし）
    # トレーリング幅は calc_staged_tp() 内で 2.0×ATR に別途設定（TP幅と分離）
    if ratio >= 1.5:
        regime = "high"
        regime_label = "高ボラ相場"
        sl_mult = 3.5
        tp1_mult = 2.0   # 変更: 3.5 → 2.0（高ボラ時でも早期到達を優先）
        tp2_mult = 5.0
        tp3_mult = 7.0
        note = f"ATRが90日平均の{ratio:.1f}倍 → SL拡大・TP縮小（Phase1出口改善）"
    elif ratio <= 0.7:
        regime = "low"
        regime_label = "低ボラ相場"
        sl_mult = 2.5
        tp1_mult = 1.25  # 変更: 2.5 → 1.25（低ボラでも早期到達を優先）
        tp2_mult = 4.0
        tp3_mult = 5.0
        note = f"ATRが90日平均の{ratio:.1f}倍 → SL縮小・TP縮小（Phase1出口改善）"
    else:
        regime = "normal"
        regime_label = "通常ボラ相場"
        sl_mult = 3.0
        tp1_mult = 1.5   # 変更: 3.0 → 1.5（主要変更: 到達率向上）
        tp2_mult = 4.5
        tp3_mult = 6.0
        note = f"ATR比率{ratio:.1f} → TP=1.5×ATR（Phase1出口改善・到達率向上）"

    return {
        "regime": regime,
        "regime_label": regime_label,
        "atr_ratio": round(ratio, 2),
        "atr_90d": round(atr_90d, 5),
        "sl_multiplier": sl_mult,
        "tp1_multiplier": tp1_mult,
        "tp2_multiplier": tp2_mult,
        "tp3_multiplier": tp3_mult,
        "note": note,
    }


def _default_regime() -> dict:
    # 2026-06-22 Phase1出口改善: tp1_multiplier 3.0 → 1.5
    return {
        "regime": "normal",
        "regime_label": "通常ボラ相場",
        "atr_ratio": 1.0,
        "atr_90d": None,
        "sl_multiplier": 3.0,
        "tp1_multiplier": 1.5,   # 変更: 3.0 → 1.5（Phase1出口改善）
        "tp2_multiplier": 4.5,
        "tp3_multiplier": 6.0,
        "note": "データ不足 → Phase1デフォルト設定（TP=1.5×ATR）",
    }


# ============================================================
# ③ 段階的TP計算
# ============================================================

def _pair_decimals(pair: str) -> int:
    """ペアに応じた小数桁数を返す。JPYクロス=3、それ以外=6。

    signal_scanner.py の pair_decimals と同じ規約を局所複製。
    モジュール循環を避けるため独立定義。
    """
    return 3 if pair and pair.upper().endswith("JPY") else 6


def _spread_for_pair(pair: str, dynamic_pips: float = None) -> float:
    """ペアの実効スプレッドを価格単位で返す。

    dynamic_pips が渡された場合はそれを優先（spread_monitor からの動的値）。
    None の場合は静的テーブル（旧来ロジック）にフォールバックする。

    Args:
        pair:         通貨ペア名
        dynamic_pips: セッション×VIX 補正済みスプレッド (pips)。None = 静的テーブル使用。
    """
    if not pair:
        return 0.0
    pip_size = 0.01 if pair.upper().endswith("JPY") else 0.0001
    if dynamic_pips is not None:
        # 動的スプレッドを価格単位に変換して返す
        return dynamic_pips * pip_size
    # 静的テーブル fallback（spread_monitor.SPREAD_PIPS_BASE と同値）
    table = {
        "USDJPY": 0.2, "EURJPY": 0.3, "GBPJPY": 2.0,
        "AUDJPY": 1.5, "NZDJPY": 2.0, "CADJPY": 1.5, "CHFJPY": 2.0,
        "SGDJPY": 2.5, "HKDJPY": 3.0,
        "CNYJPY": 5.0, "MXNJPY": 5.0, "ZARJPY": 10.0,
        "INRJPY": 8.0, "TRYJPY": 30.0,
        "EURUSD": 0.5, "GBPUSD": 1.5, "AUDUSD": 1.0, "NZDUSD": 2.0,
        "USDCAD": 1.5, "USDCHF": 2.0,
        "EURGBP": 1.5, "EURAUD": 3.0,
    }
    pips = table.get(pair.upper(), 0.0)
    return pips * pip_size


def calc_staged_tp(price: float, direction: str, atr: float, regime: dict,
                   prices: list = None, pair: str = "",
                   spread_pips: float = None) -> dict:
    """
    単一TP + トレーリングストップ戦略を計算（2026-06-10刷新）。

    実情ベースの改善：
    - TP1/TP2/TP3の3段階は判断難しく、TP1選ぶと機会損失、TP3選ぶと届かず損失
    - 解決策: TP は単一（旧TP1相当）でほぼ確実に到達 → トレーリングで伸ばす

    戦略:
    1. TP到達まで: SL固定でホールド
    2. TP到達時: 半量利確 OR 全保有 + SL を BE+0.5R へ移動 + トレーリング有効化
    3. トレーリング: 最高値（LONG）/最低値（SHORT）から trail_atr_mult × ATR の追従ストップ
    4. トレーリングストップ到達: 利益確定で終了

    旧tp2/tp3は「理論最大利益」として参考表示のみ（注文には使わない）。

    Args:
        price: 現在価格
        direction: "LONG" or "SHORT"
        atr: 現在のATR
        regime: detect_volatility_regime()の戻り値

    Returns:
        {
          "sl": 9.11,                # 初期SL
          "tp": 10.21,               # 単一TP（旧TP1相当、ATR×3.0）
          "trail_atr_mult": 3.0,     # トレーリング幅（ATR乗数）
          "trail_distance": 0.15,    # トレーリング距離（実値）
          "be_plus_offset": 0.05,    # BE+0.5R = 0.5 × SL幅
          "be_target_after_tp": 9.66, # TP到達後のSL移動先（BE+0.5R）
          "max_target": 11.31,       # 理論最大利益（参考・旧TP3）
          "rr_tp": 1.0,              # TP のリスクリワード
          "tp1": ..., "tp2": ..., "tp3": ..., # 後方互換のため残す
          "strategy": "TP(...)到達 → SLをBE+0.5R移動 + トレーリング(N×ATR)発動",
          "tp_mode": "single_with_trail"
        }
    """
    if not atr or atr == 0:
        return {}

    # 2026-06-22 Phase1出口改善: TP=1.5×ATR（到達率向上）+ トレーリング幅を TP から分離
    # 変更前: TP=3.0×ATR, trail=3.0×ATR（TP到達率6.5%、SIGNAL_LOST 85.5%）
    # 変更後: TP=1.5×ATR, trail=2.0×ATR（TP到達率30-50%目標）
    # SLは3.0×ATR維持（リスク管理は変更なし）
    sl_mult = regime.get("sl_multiplier", 3.0)
    tp_mult = regime.get("tp1_multiplier", 1.5)   # 変更: 3.0 → 1.5（Phase1出口改善）
    tp2_mult = regime.get("tp2_multiplier", 4.5)  # 後方互換（参考のみ）
    tp3_mult = regime.get("tp3_multiplier", 6.0)  # 後方互換（参考のみ）
    trail_mult = 2.0  # 変更: TP幅から分離 → 固定2.0×ATR（TP到達後の余裕幅）

    sl_width = atr * sl_mult
    tp_width = atr * tp_mult
    tp2_width = atr * tp2_mult
    tp3_width = atr * tp3_mult
    trail_distance = atr * trail_mult
    # BE+0.5R: SLをBEからリスクの0.5倍分プラス側に移動（小利確保）
    be_offset = sl_width * 0.5

    # ─── Chandelier Exit 動的SL（2026-06-11 研究C反映）───
    # 真の Chandelier Exit: 過去N日の高値/安値 ± ATR*mult
    # 条件: prices が22本以上ある場合に使用。エントリー価格より有利（保護的）な場合のみ採用。
    # 参照: wiki/concepts/chandelier-exit-trailing-stop.md
    CHANDELIER_PERIOD = 22
    chandelier_sl_active = False
    chandelier_note = ""

    if direction in ("LONG", "LIGHT_LONG"):
        sl_fixed = price - sl_width
        tp = price + tp_width
        tp2 = price + tp2_width
        tp3 = price + tp3_width
        be_target = price + be_offset

        # Chandelier Exit SL: 過去22日高値 - ATR*sl_mult
        if prices and len(prices) >= CHANDELIER_PERIOD:
            recent_high = max(prices[-CHANDELIER_PERIOD:])
            sl_ce = recent_high - sl_width
            # CE SL はエントリー価格より下（安全）かつ固定SLより上（タイト）な時のみ採用
            if sl_fixed < sl_ce < price:
                sl = sl_ce
                chandelier_sl_active = True
                chandelier_note = (
                    f"Chandelier Exit: 過去{CHANDELIER_PERIOD}日高値"
                    f"({round(recent_high, _pair_decimals(pair))})-{sl_mult}xATR"
                )
            else:
                sl = sl_fixed  # 固定SLの方が安全（広い）場合はそちらを使用
        else:
            sl = sl_fixed

    elif direction in ("SHORT", "LIGHT_SHORT"):
        sl_fixed = price + sl_width
        tp = price - tp_width
        tp2 = price - tp2_width
        tp3 = price - tp3_width
        be_target = price - be_offset

        # Chandelier Exit SL: 過去22日安値 + ATR*sl_mult
        if prices and len(prices) >= CHANDELIER_PERIOD:
            recent_low = min(prices[-CHANDELIER_PERIOD:])
            sl_ce = recent_low + sl_width
            # CE SL はエントリー価格より上（安全）かつ固定SLより下（タイト）な時のみ採用
            if price < sl_ce < sl_fixed:
                sl = sl_ce
                chandelier_sl_active = True
                chandelier_note = (
                    f"Chandelier Exit: 過去{CHANDELIER_PERIOD}日安値"
                    f"({round(recent_low, _pair_decimals(pair))})+{sl_mult}xATR"
                )
            else:
                sl = sl_fixed
        else:
            sl = sl_fixed

    else:
        return {}

    # ペア別の表示桁数（JPYクロス=3、それ以外=6）
    # 注: pair 未指定の旧呼び出し互換のため price>10 をフォールバックに残す
    decimals = _pair_decimals(pair) if pair else (3 if price > 10 else 6)

    rr_tp = round(tp_mult / sl_mult, 1)
    rr_tp2 = round(tp2_mult / sl_mult, 1)
    rr_tp3 = round(tp3_mult / sl_mult, 1)

    # ── スプレッド補正（2026-06-23 追加、2026-06-25 動的化）──
    # bid/ask スプレッドを考慮した実効 SL/TP 距離と RR を算出。
    # spread_pips が渡されている場合はセッション×VIX 補正済みの動的値を使用する。
    # 中値ベースの sl/tp はそのまま、effective_* で表示用の歪み補正値を提供。
    # LONG: ASK=mid+spread/2 で約定 → SL までの距離+spread/2、TP までの距離-spread/2
    # SHORT: BID=mid-spread/2 で約定 → SL までの距離+spread/2、TP までの距離-spread/2
    spread_price = _spread_for_pair(pair, dynamic_pips=spread_pips)
    spread_is_dynamic = spread_pips is not None
    spread_half = spread_price / 2.0
    # 中値ベースの距離
    sl_dist_mid = sl_width  # = atr * sl_mult
    tp_dist_mid = tp_width  # = atr * tp_mult
    # 実効距離（スプレッドにより SL は近く、TP は遠くなる）
    sl_dist_effective = sl_dist_mid + spread_half
    tp_dist_effective = max(tp_dist_mid - spread_half, 0.0)
    rr_effective = round(tp_dist_effective / sl_dist_effective, 2) if sl_dist_effective > 0 else 0
    # spread/ATR 比（降格フィルタ判定で使用）
    spread_atr_ratio = round(spread_price / atr, 3) if atr else 0

    return {
        # ── 新方式（単一TP + トレーリング）──
        "sl": round(sl, decimals),
        "tp": round(tp, decimals),
        "trail_atr_mult": trail_mult,
        "trail_distance": round(trail_distance, decimals),
        "be_plus_offset": round(be_offset, decimals),
        "be_target_after_tp": round(be_target, decimals),
        "max_target": round(tp3, decimals),  # 参考: 理論最大利益（旧TP3）
        "rr_tp": rr_tp,
        "tp_mode": "single_with_trail",
        # ── 後方互換（既存コード/JSON消費者向け）──
        "tp1": round(tp, decimals),      # 新TPは旧TP1相当
        "tp2": round(tp2, decimals),
        "tp3": round(tp3, decimals),
        "sl_width": round(sl_width, decimals),
        "tp1_width": round(tp_width, decimals),
        "rr_tp1": rr_tp,
        "rr_tp2": rr_tp2,
        "rr_tp3": rr_tp3,
        # ── スプレッド補正情報（表示用）──
        "spread_price": round(spread_price, decimals),
        "spread_pips": round(spread_price / (0.01 if pair.upper().endswith("JPY") else 0.0001), 1) if pair else 0,
        "spread_atr_ratio": spread_atr_ratio,
        "sl_dist_effective": round(sl_dist_effective, decimals),
        "tp_dist_effective": round(tp_dist_effective, decimals),
        "rr_effective": rr_effective,
        # 動的スプレッドフラグ（2026-06-25 追加）
        "spread_is_dynamic": spread_is_dynamic,
        "spread_pips_dynamic": round(spread_pips, 2) if spread_pips is not None else None,
        # ── 共通メタ ──
        "regime_label": regime.get("regime_label", ""),
        "strategy": (
            f"TP({round(tp, decimals)})到達 → SLを BE+0.5R({round(be_target, decimals)}) "
            f"へ移動 → トレーリング({trail_mult}×ATR={round(trail_distance, decimals)})発動 "
            f"→ 利を伸ばす（理論最大: {round(tp3, decimals)}）"
        ),
        # Chandelier Exit 情報（2026-06-11 研究C反映）
        "chandelier_sl_active": chandelier_sl_active,
        "chandelier_note": chandelier_note,
    }


# ============================================================
# ④ 複数ポジション相関リスク警告
# ============================================================

def calc_correlation_risk(all_results: list, pair_api: dict) -> dict:
    """
    ★4以上シグナルのポジション間の通貨集中リスクを分析。

    Returns:
        {
          "currency_exposure": {"USD": +3, "JPY": -2, "AUD": +1},
          "warnings": ["USD買いポジションが3件集中"],
          "risk_level": "high" | "medium" | "low",
          "recommendation": "USDペアを1〜2件に絞ることを推奨"
        }
    """
    # ★4以上のシグナルのみ対象
    active = [
        r for r in all_results
        if r.get("stars", 0) >= 4
        and r.get("direction", "").endswith(("LONG", "SHORT"))
        and r.get("pair") in pair_api
    ]

    if not active:
        return {"currency_exposure": {}, "warnings": [], "risk_level": "low"}

    # 各通貨のエクスポージャー計算
    # LONG: base通貨を買い(+1)、quote通貨を売り(-1)
    # SHORT: base通貨を売り(-1)、quote通貨を買い(+1)
    exposure = {}
    for r in active:
        pair = r["pair"]
        base, quote = pair_api[pair]
        direction = r["direction"]

        if direction.endswith("LONG"):
            exposure[base] = exposure.get(base, 0) + 1
            exposure[quote] = exposure.get(quote, 0) - 1
        else:
            exposure[base] = exposure.get(base, 0) - 1
            exposure[quote] = exposure.get(quote, 0) + 1

    # 警告生成
    warnings = []
    for ccy, exp in exposure.items():
        abs_exp = abs(exp)
        if abs_exp >= 3:
            direction_label = "買い" if exp > 0 else "売り"
            warnings.append(
                f"⚠ {ccy}{direction_label}ポジションが{abs_exp}件集中"
                f" → 同方向リスクに注意"
            )
        elif abs_exp == 2:
            direction_label = "買い" if exp > 0 else "売り"
            warnings.append(
                f"△ {ccy}{direction_label}ポジションが2件 → やや集中"
            )

    # リスクレベル判定
    max_exposure = max((abs(v) for v in exposure.values()), default=0)
    if max_exposure >= 3:
        risk_level = "high"
        recommendation = "同一通貨のポジション集中を解消するか、ロットを半減することを推奨"
    elif max_exposure == 2:
        risk_level = "medium"
        recommendation = "同一通貨が2件重複。新規エントリー時は注意"
    else:
        risk_level = "low"
        recommendation = "ポジション分散良好"

    return {
        "currency_exposure": exposure,
        "warnings": warnings,
        "risk_level": risk_level,
        "recommendation": recommendation,
        "active_signals_count": len(active),
    }


# ============================================================
# ⑤ キャリートレード魅力度スコア
# ============================================================

def calc_carry_score(pair: str, rate_diff: float, atr: float, price: float) -> dict:
    """
    金利差 / ボラリスク でキャリートレードの魅力度を数値化。

    Returns:
        {
          "carry_score": 1.8,
          "label": "普通",
          "annual_swap_pct": 3.6,    # 年率スワップ収益率
          "daily_volatility_pct": 2.0,  # 日次ボラ率
          "breakeven_days": 20,      # スワップでSL幅を回収するのに必要な日数
          "note": "AUDJPYは最もコスパの良いキャリー通貨"
        }
    """
    if rate_diff is None or atr is None or price is None or price == 0:
        return {}

    # 日次ボラティリティ（%）
    daily_vol_pct = (atr / price) * 100
    if daily_vol_pct == 0:
        return {}

    # 年率スワップ収益率（金利差の絶対値）
    annual_swap_pct = abs(rate_diff)

    # キャリースコア = 年率スワップ / 日次ボラ
    # スコアが高いほど、ボラに対してスワップが大きい
    carry_score = round(annual_swap_pct / daily_vol_pct, 2)

    # SL幅をスワップで回収するまでの日数
    sl_pct = daily_vol_pct * 2.5  # ATR×2.5のSL幅
    if annual_swap_pct > 0:
        daily_swap_pct = annual_swap_pct / 365
        breakeven_days = int(sl_pct / daily_swap_pct) if daily_swap_pct > 0 else 9999
    else:
        breakeven_days = 9999

    if carry_score >= 3.0:
        label = "非常に優れたキャリー"
    elif carry_score >= 1.5:
        label = "良好なキャリー"
    elif carry_score >= 0.8:
        label = "普通のキャリー"
    elif carry_score >= 0.3:
        label = "キャリー効率が低い"
    else:
        label = "キャリー不向き"

    return {
        "carry_score": carry_score,
        "label": label,
        "annual_swap_pct": round(annual_swap_pct, 2),
        "daily_volatility_pct": round(daily_vol_pct, 2),
        "breakeven_days": breakeven_days,
        "note": f"SL幅をスワップで回収: 約{breakeven_days}日",
    }


# ============================================================
# ⑥ サポート・レジスタンス自動検出
# ============================================================

def detect_support_resistance(prices: list, current_price: float, pair: str = "") -> dict:
    """
    過去の価格から主要なサポート・レジスタンス水準を検出。

    アルゴリズム:
      1. 局所的高値・安値を検出（前後N本のローソクより高い/低い点）
      2. 近い水準をクラスタリング（ATRの0.5倍以内はまとめる）
      3. 強度（そのレベルが何回タッチされたか）でソート

    Returns:
        {
          "resistance": [
            {"price": 160.00, "strength": 5, "label": "強いレジスタンス"},
            {"price": 159.50, "strength": 2, "label": "レジスタンス"},
          ],
          "support": [
            {"price": 158.80, "strength": 3, "label": "サポート"},
          ],
          "nearest_resistance": {"price": 159.50, "distance_pct": 0.23},
          "nearest_support": {"price": 158.80, "distance_pct": 0.21},
          "context": "レジスタンスまで0.23%（近い）",
        }
    """
    if not prices or len(prices) < 20:
        return {}

    # ATR計算（クラスタリング用）
    daily_tr = [abs(prices[i] - prices[i - 1]) for i in range(1, len(prices))]
    atr = sum(daily_tr[-14:]) / min(14, len(daily_tr)) if daily_tr else current_price * 0.005
    cluster_threshold = atr * 0.5

    # 局所的高値・安値の検出（前後5本より高い/低い点）
    window = 5
    peaks = []
    troughs = []
    for i in range(window, len(prices) - window):
        segment = prices[i - window:i + window + 1]
        if prices[i] == max(segment):
            peaks.append(prices[i])
        if prices[i] == min(segment):
            troughs.append(prices[i])

    def cluster_levels(levels):
        """近い水準をまとめてクラスタ化し、出現回数をカウント"""
        if not levels:
            return []
        sorted_levels = sorted(levels)
        clusters = []
        current_cluster = [sorted_levels[0]]

        for level in sorted_levels[1:]:
            if level - current_cluster[-1] <= cluster_threshold:
                current_cluster.append(level)
            else:
                avg = sum(current_cluster) / len(current_cluster)
                clusters.append({"price": round(avg, _pair_decimals(pair)), "count": len(current_cluster)})
                current_cluster = [level]
        if current_cluster:
            avg = sum(current_cluster) / len(current_cluster)
            clusters.append({"price": round(avg, 3), "count": len(current_cluster)})

        return sorted(clusters, key=lambda x: -x["count"])

    resistance_clusters = cluster_levels([p for p in peaks if p > current_price])
    support_clusters = cluster_levels([p for p in troughs if p < current_price])

    def format_levels(clusters, is_resistance):
        result = []
        for c in clusters[:4]:  # 上位4件のみ
            strength = c["count"]
            if strength >= 4:
                label = "強いレジスタンス" if is_resistance else "強いサポート"
            elif strength >= 2:
                label = "レジスタンス" if is_resistance else "サポート"
            else:
                label = "弱いレジスタンス" if is_resistance else "弱いサポート"

            dist_pct = abs(c["price"] - current_price) / current_price * 100
            result.append({
                "price": c["price"],
                "strength": strength,
                "label": label,
                "distance_pct": round(dist_pct, 2),
            })
        return result

    resistances = format_levels(resistance_clusters, True)
    supports = format_levels(support_clusters, False)

    # 最近傍レベルの特定
    nearest_r = min(resistances, key=lambda x: x["distance_pct"]) if resistances else None
    nearest_s = max(supports, key=lambda x: -x["distance_pct"]) if supports else None

    # コンテキストメッセージ
    context_parts = []
    if nearest_r:
        if nearest_r["distance_pct"] < 0.5:
            context_parts.append(f"レジスタンス({nearest_r['price']})まで{nearest_r['distance_pct']:.2f}%（非常に近い・注意）")
        else:
            context_parts.append(f"レジスタンス({nearest_r['price']})まで{nearest_r['distance_pct']:.2f}%")
    if nearest_s:
        if nearest_s["distance_pct"] < 0.5:
            context_parts.append(f"サポート({nearest_s['price']})まで{nearest_s['distance_pct']:.2f}%（非常に近い・注意）")
        else:
            context_parts.append(f"サポート({nearest_s['price']})まで{nearest_s['distance_pct']:.2f}%")

    return {
        "resistance": resistances,
        "support": supports,
        "nearest_resistance": nearest_r,
        "nearest_support": nearest_s,
        "context": " / ".join(context_parts) if context_parts else "レベル検出なし",
    }


# ============================================================
# ⑧ 日銀介入リスクインジケーター
# ============================================================

INTERVENTION_HISTORY = [
    # (price_level, 介入実績)
    (151.0, "2022年9-10月 介入実績"),
    (152.0, "2022年10月 連続介入"),
    (155.0, "介入警戒ゾーン開始"),
    (158.0, "2024年4月 介入実績付近"),
    (160.0, "2024年4月29日 介入実績"),
    (162.0, "2024年7月 介入実績"),
]


def calc_intervention_risk(
    pair: str,
    price: float,
    prices_30d: list,
    sentiment: dict,
) -> dict:
    """
    USDJPY専用の日銀介入リスクスコアを算出（0〜100）。

    スコア構成:
      価格水準   (0〜40点): 155円超で加点、160円超で最大
      上昇速度   (0〜25点): 30日で急騰していると加点
      ボラ急騰   (0〜15点): ATRが急拡大していると加点
      DXY急上昇  (0〜20点): ドル独歩高の場合に加点

    Returns:
        {
          "risk_score": 72,
          "risk_level": "HIGH",
          "components": {...},
          "nearest_historical_level": "2024年4月29日 介入実績",
          "recommendation": "ロングシグナルを2段階降格推奨",
        }
    """
    if pair != "USDJPY":
        return {}

    score = 0
    components = {}

    # ── 価格水準スコア（0〜40点）──
    if price >= 162.0:
        price_score = 40
        price_note = "162円超（過去介入実績レベル・最高警戒）"
    elif price >= 160.0:
        price_score = 35
        price_note = "160円超（過去介入実績レベル）"
    elif price >= 158.0:
        price_score = 27
        price_note = "158円超（高警戒ゾーン）"
    elif price >= 155.0:
        price_score = 20
        price_note = "155円超（警戒開始ゾーン）"
    elif price >= 152.0:
        price_score = 8
        price_note = "152円超（注意ゾーン）"
    else:
        price_score = 0
        price_note = f"{price:.2f}円（介入警戒水準以下）"

    score += price_score
    components["price"] = {"score": price_score, "note": price_note}

    # ── 上昇速度スコア（0〜25点）──
    speed_score = 0
    speed_note = "データ不足"
    if prices_30d and len(prices_30d) >= 10:
        price_30d_ago = prices_30d[0]
        change_30d = price - price_30d_ago
        if change_30d >= 8.0:
            speed_score = 25
            speed_note = f"30日で+{change_30d:.1f}円急騰（非常に速い）"
        elif change_30d >= 5.0:
            speed_score = 18
            speed_note = f"30日で+{change_30d:.1f}円上昇（速い）"
        elif change_30d >= 3.0:
            speed_score = 10
            speed_note = f"30日で+{change_30d:.1f}円上昇（やや速い）"
        elif change_30d >= 1.0:
            speed_score = 5
            speed_note = f"30日で+{change_30d:.1f}円上昇（緩やか）"
        else:
            speed_score = 0
            speed_note = f"30日変化: {change_30d:+.1f}円（横ばいまたは下落）"

    score += speed_score
    components["speed"] = {"score": speed_score, "note": speed_note}

    # ── ボラ急騰スコア（0〜15点）──
    vol_score = 0
    vol_note = "データ不足"
    if prices_30d and len(prices_30d) >= 20:
        recent_tr = [abs(prices_30d[i] - prices_30d[i - 1]) for i in range(1, len(prices_30d))]
        if recent_tr:
            recent_atr_7d = sum(recent_tr[-7:]) / min(7, len(recent_tr))
            hist_atr_30d = sum(recent_tr) / len(recent_tr)
            if hist_atr_30d > 0:
                vol_ratio = recent_atr_7d / hist_atr_30d
                if vol_ratio >= 2.0:
                    vol_score = 15
                    vol_note = f"直近7日ATRが30日平均の{vol_ratio:.1f}倍（急騰）"
                elif vol_ratio >= 1.5:
                    vol_score = 10
                    vol_note = f"直近7日ATRが30日平均の{vol_ratio:.1f}倍（拡大）"
                elif vol_ratio >= 1.2:
                    vol_score = 5
                    vol_note = f"直近7日ATRが30日平均の{vol_ratio:.1f}倍（やや拡大）"
                else:
                    vol_score = 0
                    vol_note = f"ボラ安定（比率{vol_ratio:.1f}倍）"

    score += vol_score
    components["volatility"] = {"score": vol_score, "note": vol_note}

    # ── DXY急上昇スコア（0〜20点）──
    dxy_score = 0
    dxy_note = "DXYデータなし"
    if sentiment:
        dxy_trend = sentiment.get("dxy_trend", "flat")
        if dxy_trend == "strong":
            dxy_score = 20
            dxy_note = "DXY急上昇（ドル独歩高・介入誘因が高い）"
        elif dxy_trend == "rising":
            dxy_score = 10
            dxy_note = "DXY上昇トレンド"
        else:
            dxy_score = 0
            dxy_note = f"DXYトレンド: {dxy_trend}"

    score += dxy_score
    components["dxy"] = {"score": dxy_score, "note": dxy_note}

    # ── リスクレベルと推奨アクション ──
    if score >= 70:
        risk_level = "CRITICAL"
        risk_label = "介入リスク 極めて高い"
        recommendation = "ロングシグナルを2〜3段階強制降格。新規ロング原則禁止"
    elif score >= 50:
        risk_level = "HIGH"
        risk_label = "介入リスク 高い"
        recommendation = "ロングシグナルを1〜2段階降格。ストップを近めに設定"
    elif score >= 30:
        risk_level = "MEDIUM"
        risk_label = "介入リスク 中程度"
        recommendation = "ロングシグナルを1段階降格。介入口先発言に注意"
    else:
        risk_level = "LOW"
        risk_label = "介入リスク 低い"
        recommendation = "通常通りシグナル評価"

    # 最近傍の過去介入水準
    nearest_hist = None
    min_dist = float("inf")
    for hist_price, hist_label in INTERVENTION_HISTORY:
        dist = abs(price - hist_price)
        if dist < min_dist:
            min_dist = dist
            nearest_hist = hist_label

    return {
        "risk_score": score,
        "risk_level": risk_level,
        "risk_label": risk_label,
        "components": components,
        "nearest_historical_level": nearest_hist,
        "recommendation": recommendation,
    }


# ============================================================
# 統合関数: すべての高度分析を一括実行
# ============================================================

def run_advanced_analytics(
    result: dict,
    prices: list,
    all_pair_prices: dict,
    pair_api: dict,
    cb_rates: dict,
    sentiment: dict,
    all_results: list,
) -> dict:
    """
    1つの通貨ペアのシグナルに対して全高度分析を実行し、
    resultに情報を追加して返す。

    all_resultsはポートフォリオ相関分析に使用（毎回渡すが①⑧は1回計算で良い）。
    """
    pair = result.get("pair", "")
    price = result.get("price", 0)
    atr = result.get("atr")
    direction = result.get("direction", "")
    fa_rate_diff = result.get("fa_rate_diff", 0)

    # ② ボラティリティ・レジーム判定
    regime = detect_volatility_regime(prices, atr)
    result["volatility_regime"] = regime

    # ③ 段階的TP計算（Chandelier Exit 動的SL付き: 2026-06-11 研究C反映）
    # 2026-06-25: spread_pips を動的に計算してスプレッド補正の精度を向上
    #   - セッション乗数（London/NYタイト ×0.8、Asian非JPY ×1.4、Off-hours ×1.6）
    #   - VIX 乗数（VIX>30: ×2.0、VIX>25: ×1.5）
    if direction.endswith(("LONG", "SHORT")) and atr:
        vix_val = sentiment.get("vix") if sentiment else None
        dynamic_spread = get_dynamic_spread_pips(pair, vix=vix_val)
        spread_meta = get_dynamic_spread_metadata(pair, vix=vix_val)
        staged_tp = calc_staged_tp(
            price, direction, atr, regime,
            prices=prices, pair=pair,
            spread_pips=dynamic_spread,
        )
        staged_tp["spread_session"] = spread_meta.get("session_label", "")
        staged_tp["spread_session_mult"] = spread_meta.get("session_mult", 1.0)
        staged_tp["spread_vix_mult"] = spread_meta.get("vix_mult", 1.0)
        result["staged_tp"] = staged_tp

    # ⑤ キャリートレード魅力度スコア
    if fa_rate_diff is not None and atr and price:
        carry = calc_carry_score(pair, fa_rate_diff, atr, price)
        result["carry_score"] = carry

    # ⑥ サポート・レジスタンス
    if prices and len(prices) >= 20:
        sr = detect_support_resistance(prices, price, pair=pair)
        result["support_resistance"] = sr

    # ⑧ 日銀介入リスク（USDJPYのみ）
    if pair == "USDJPY":
        intervention = calc_intervention_risk(
            pair, price,
            prices[-30:] if len(prices) >= 30 else prices,
            sentiment
        )
        result["intervention_risk"] = intervention

        # 介入リスクが高い場合は★を降格
        risk_score = intervention.get("risk_score", 0)
        if risk_score >= 70 and direction.endswith("LONG"):
            result["stars"] = max(1, result.get("stars", 1) - 2)
            result["verdict"] += f" ⚠介入リスクCRITICAL({risk_score})"
        elif risk_score >= 50 and direction.endswith("LONG"):
            result["stars"] = max(1, result.get("stars", 1) - 1)
            result["verdict"] += f" ⚠介入リスクHIGH({risk_score})"

    return result


def calc_portfolio_analytics(all_results: list, pair_api: dict) -> dict:
    """
    全ペア評価後に1回だけ呼ぶ。
    ④ ポジション相関リスクを計算して返す。
    """
    return calc_correlation_risk(all_results, pair_api)


def calc_global_analytics(all_pair_prices: dict, pair_api: dict) -> dict:
    """
    スキャン開始時に1回だけ呼ぶ。
    ① 通貨強弱メーターを計算して返す。
    """
    return calc_currency_strength(all_pair_prices, pair_api)
