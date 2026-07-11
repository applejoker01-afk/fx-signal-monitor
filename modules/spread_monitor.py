"""
spread_monitor.py
動的スプレッドモニタリング（2026-06-25 追加）

FX スプレッドは取引時刻・ボラティリティにより大きく変動する。
本モジュールは静的代表値テーブルに「セッション補正 × VIX 補正」を
適用して実効スプレッドをリアルタイム推定する。

研究根拠:
  - London/NY overlap (13-17 UTC): スプレッド最タイト（代表値の約 80%）
  - Asian session (01-08 UTC):
      JPY ペア = 東京市場で流動性高くタイト (×0.9)
      非JPY ペア = 薄い (×1.4〜1.8)
  - Off-hours (20-01 UTC): ×1.6〜1.8（流動性低下）
  - VIX 20-25: ×1.2、VIX 25-30: ×1.5、VIX>30: ×2.0（2024年8月実例）

セッション時間 (UTC):
  00-01: 深夜クローズ（極薄）
  01-08: アジア・東京
  08-13: ロンドンオープン
  13-17: ロンドン/NY オーバーラップ（最タイト）
  17-20: NY 午後
  20-24: NY クローズ〜深夜（最ワイド）

使い方:
    from modules.spread_monitor import get_dynamic_spread_pips
    pips = get_dynamic_spread_pips("GBPJPY", vix=28.5)
    # → 2.0 (base) × 1.4 (Asian) × 1.5 (VIX) = 4.2 pips

他モジュールとの同期:
    SPREAD_PIPS_BASE は signal_scanner.py の SPREAD_PIPS テーブルと同じ値を維持。
    変更時は両ファイルを同時に更新すること。
"""

from datetime import datetime, timezone
from typing import Optional

# ============================================================
# マスター静的スプレッドテーブル（pips 単位）
# ============================================================
SPREAD_PIPS_BASE: dict = {
    # メジャー JPY（タイト）
    "USDJPY": 0.2, "EURJPY": 0.3, "GBPJPY": 2.0,
    # 流動性中程度 JPY
    "AUDJPY": 1.5, "NZDJPY": 2.0, "CADJPY": 1.5, "CHFJPY": 2.0,
    "SGDJPY": 2.5, "HKDJPY": 3.0,
    # エキゾチック JPY（ワイド・要警戒）
    "CNYJPY": 5.0, "MXNJPY": 5.0, "ZARJPY": 10.0,
    "INRJPY": 8.0, "TRYJPY": 30.0,
    # メジャー非JPY
    "EURUSD": 0.5, "GBPUSD": 1.5, "AUDUSD": 1.0, "NZDUSD": 2.0,
    "USDCAD": 1.5, "USDCHF": 2.0,
    "EURGBP": 1.5, "EURAUD": 3.0,
}

# テーブル未定義ペアのデフォルト（保守的に 3.0 pips）
_DEFAULT_PIPS = 3.0


# ============================================================
# セッション補正乗数
# ============================================================

def get_session_multiplier(utc_hour: int, pair: str = "") -> float:
    """
    UTC 時間帯に基づいてスプレッド乗数を返す。

    JPY ペアは東京時間 (01-08 UTC) に流動性が最も高くスプレッドがタイト。
    非 JPY ペアは同時間帯に薄くなりスプレッドが拡大する（典型的な逆転）。

    Args:
        utc_hour: 0〜23 の UTC 時間
        pair:     通貨ペア名（大文字小文字不問）

    Returns:
        スプレッド乗数（1.0 = 静的代表値そのまま）
    """
    is_jpy = (pair or "").upper().endswith("JPY")

    if 0 <= utc_hour < 1:
        # 深夜クローズ - 最薄流動性
        return 1.0 if is_jpy else 1.8

    elif 1 <= utc_hour < 8:
        # アジア・東京セッション
        # JPY ペア: 東京市場でタイト
        # 非 JPY:  欧米勢不在で薄い
        return 0.9 if is_jpy else 1.4

    elif 8 <= utc_hour < 13:
        # ロンドンオープン（欧州タイト）
        return 0.85

    elif 13 <= utc_hour < 17:
        # ロンドン/NY オーバーラップ（一日で最タイト）
        return 0.80

    elif 17 <= utc_hour < 20:
        # NY 午後（通常流動性）
        return 1.0

    else:
        # NY クローズ〜深夜（流動性低下・ワイド化）
        return 1.6


# ============================================================
# VIX 補正乗数
# ============================================================

def get_vix_multiplier(vix: Optional[float]) -> float:
    """
    VIX 水準に基づいてスプレッド乗数を返す。

    高ボラティリティ時はマーケットメーカーがリスク回避のためスプレッドを
    拡大することが実証されている（特に VIX>25 で明確な拡大が見られる）。

    Args:
        vix: VIX 指数。None の場合は乗数 1.0 を返す（補正なし）。

    Returns:
        スプレッド乗数
    """
    if vix is None:
        return 1.0
    if vix < 15:
        return 0.9   # 超低ボラ: タイト
    elif vix < 20:
        return 1.0   # 平常
    elif vix < 25:
        return 1.2   # 要注意（+20%）
    elif vix < 30:
        return 1.5   # リスクオフ（+50%）
    else:
        return 2.0   # パニック: 2024年8月型（+100%）


# ============================================================
# セッションラベル（表示用）
# ============================================================

def get_session_label(utc_hour: int) -> str:
    """UTC 時間からセッション名を返す（表示用）。"""
    if 0 <= utc_hour < 1:
        return "深夜クローズ"
    elif 1 <= utc_hour < 8:
        return "Asian/Tokyo"
    elif 8 <= utc_hour < 13:
        return "London"
    elif 13 <= utc_hour < 17:
        return "London/NY overlap"
    elif 17 <= utc_hour < 20:
        return "NY afternoon"
    else:
        return "NY close/Off-hours"


# ============================================================
# メイン: 動的スプレッド取得
# ============================================================

def get_dynamic_spread_pips(
    pair: str,
    vix: Optional[float] = None,
    utc_now: Optional[datetime] = None,
) -> float:
    """
    通貨ペアの動的スプレッドを pips 単位で返す。

    算出式:
        dynamic_pips = base_pips × session_multiplier × vix_multiplier

    Args:
        pair:    通貨ペア名（'USDJPY', 'GBPJPY' など。大文字小文字不問）
        vix:     現在の VIX 指数。None = VIX 補正なし（×1.0）
        utc_now: 現在の UTC 時刻。None = datetime.now(UTC) を使用

    Returns:
        動的スプレッド (pips)。テーブル未定義ペアは 3.0 pips ベース。

    Example:
        # GBPJPY を Asian session (UTC 03:00)、VIX=28 で取得
        >>> get_dynamic_spread_pips("GBPJPY", vix=28, utc_now=dt(3))
        # base=2.0 × session=0.9(JPY) × vix=1.5 = 2.7 pips

        # GBPJPY を London/NY overlap (UTC 14:00)、VIX=15 で取得
        >>> get_dynamic_spread_pips("GBPJPY", vix=15, utc_now=dt(14))
        # base=2.0 × session=0.80 × vix=0.9 = 1.44 pips
    """
    base = SPREAD_PIPS_BASE.get((pair or "").upper(), _DEFAULT_PIPS)
    if not pair:
        return base

    now = utc_now if utc_now is not None else datetime.now(timezone.utc)
    utc_hour = now.hour

    session_mult = get_session_multiplier(utc_hour, pair)
    vix_mult = get_vix_multiplier(vix)

    return round(base * session_mult * vix_mult, 2)


def get_dynamic_spread_metadata(
    pair: str,
    vix: Optional[float] = None,
    utc_now: Optional[datetime] = None,
) -> dict:
    """
    動的スプレッドの詳細情報を返す（デバッグ・ログ・表示用）。

    Returns:
        {
            "pair":          str,    # 通貨ペア
            "base_pips":     float,  # 静的代表値
            "dynamic_pips":  float,  # 補正後の動的値
            "session_mult":  float,  # セッション乗数
            "vix_mult":      float,  # VIX 乗数
            "session_label": str,    # セッション名称
            "utc_hour":      int,    # 現在 UTC 時
            "vix":           float|None,
        }
    """
    base = SPREAD_PIPS_BASE.get((pair or "").upper(), _DEFAULT_PIPS)
    now = utc_now if utc_now is not None else datetime.now(timezone.utc)
    utc_hour = now.hour

    session_mult = get_session_multiplier(utc_hour, pair)
    vix_mult = get_vix_multiplier(vix)
    dynamic = round(base * session_mult * vix_mult, 2)

    return {
        "pair": (pair or "").upper(),
        "base_pips": base,
        "dynamic_pips": dynamic,
        "session_mult": session_mult,
        "vix_mult": vix_mult,
        "session_label": get_session_label(utc_hour),
        "utc_hour": utc_hour,
        "vix": vix,
    }
