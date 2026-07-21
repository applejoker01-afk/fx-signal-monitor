#!/usr/bin/env python3
"""
FX売買シグナル監視スキャナー（レベル3 Advanced）
8つの高度分析機能を統合した最終版。
"""

import os
import sys

# Windows の cp932 コンソールでも絵文字・日本語を安全に出力する
# （encode 不可な文字は ? に置換。Discord/ファイル出力には影響しない）
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
import json
import smtplib
import urllib.request
from datetime import datetime, timedelta, timezone
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.header import Header

from modules.rate_fetcher import (
    load_central_bank_rates, fetch_live_central_bank_rates,
    fetch_us_treasury_yields, compute_fa_score
)
from modules.event_filter import (
    check_event_proximity, upcoming_events_for
)
from modules.sentiment_monitor import (
    evaluate_market_sentiment, apply_sentiment_filter, classify_pair
)
from modules.obsidian_extractor import fetch_and_save as fetch_obsidian
from modules.custom_rules_engine import apply_obsidian_intelligence
from modules.advanced_analytics import (
    run_advanced_analytics,
    calc_portfolio_analytics,
    calc_global_analytics,
    get_pair_strength_context,
)
from modules.trade_tracker import update_trades
from modules.performance_intelligence import (
    build_pair_performance_map, apply_performance_weighting,
    check_drawdown_alert, detect_market_regime,
    PAIR_EXCLUDE, apply_static_baseline,       # 2026-06-16 軌道修正
    apply_boj_cycle_directional_filter,        # 2026-06-19 BOJサイクル方向フィルタ
    apply_vix_regime_filter,                   # 2026-06-22 VIXレジームフィルタ
    apply_spread_filter,                       # 2026-06-23 スプレッド/ATR比フィルタ
    apply_session_filter,                      # 2026-07-20 セッションフィルタ（London単独枠）
    apply_seasonal_filter,                     # 2026-07-20 季節性フィルタ（AUDJPY 8月）
)
from modules.ai_commentary import generate_market_commentary, generate_exit_advice, has_ai_key
from modules.ambush_alert import evaluate_ambush, collect_ambush_alerts
from modules.geopolitical_risk import apply_geopolitical_filter
from modules.drl_collector import collect_scan_results, get_drl_stats  # 2026-06-11 研究A
from modules.entry_validator import (                                   # 2026-06-25 エントリー有効性
    validate_entry_for_result, format_entry_block, format_entry_block_short,
)
from modules.position_sizing import (                                   # 2026-07-20 シミュレーション口座連動
    calc_position_size, calc_maintenance_ratio, load_virtual_account,
    LOSS_CUT_MAINTENANCE_RATIO,
)

PAGES_URL = "https://applejoker01-afk.github.io/fx-signal-monitor/"


# ============================================================
# CB会合ブラックアウト（2026-06-16追加）
# 中央銀行会合の前後 BLACKOUT_HOURS 時間はJPYシグナルを警告付きに降格
# ============================================================

CB_MEETING_BLACKOUT_HOURS = 36   # 会合後36時間は要注意
CB_MEETING_WARN_HOURS     = 48   # 会合48時間前から警告


def check_cb_meeting_blackout(pair: str, cb_rates: dict, now: datetime) -> dict:
    """
    指定ペアの通貨が中央銀行会合のブラックアウト期間内かチェック。
    next_meeting（次回予定）と last_meeting（直近実施済み）の両方を確認。

    Returns:
        {"active": bool, "reason": str, "severity": "warn"|"blackout"|"none"}
    """
    if pair not in PAIR_API:
        return {"active": False, "severity": "none", "reason": ""}

    currencies = list(PAIR_API[pair])
    rates_data = cb_rates.get("rates", cb_rates)

    def _parse_dt(s):
        if not s:
            return None
        try:
            return datetime.fromisoformat(s).replace(tzinfo=timezone.utc)
        except ValueError:
            try:
                return datetime.strptime(s, "%Y-%m-%d").replace(tzinfo=timezone.utc)
            except ValueError:
                return None

    # 全通貨を評価してから、最も重篤なステータスを返す
    # （blackout > warn > none の優先順位）
    best_blackout = None
    best_warn = None

    for ccy in currencies:
        info = rates_data.get(ccy, {})

        # ① last_meeting: 会合後ブラックアウト（優先度最高）
        last_dt = _parse_dt(info.get("last_meeting"))
        if last_dt:
            hours_since = (now - last_dt).total_seconds() / 3600
            if 0 <= hours_since <= CB_MEETING_BLACKOUT_HOURS:
                best_blackout = {
                    "active": True,
                    "severity": "blackout",
                    "reason": f"{ccy} 中銀会合後 {hours_since:.0f}h（ブラックアウト {CB_MEETING_BLACKOUT_HOURS}h）",
                    "currency": ccy,
                }

        # ② next_meeting: 会合前警告 & 会合直後（last_meetingがない場合）
        next_dt = _parse_dt(info.get("next_meeting"))
        if next_dt:
            hours_diff = (now - next_dt).total_seconds() / 3600
            if 0 <= hours_diff <= CB_MEETING_BLACKOUT_HOURS and not best_blackout:
                best_blackout = {
                    "active": True,
                    "severity": "blackout",
                    "reason": f"{ccy} 中銀会合後 {hours_diff:.0f}h（ブラックアウト {CB_MEETING_BLACKOUT_HOURS}h）",
                    "currency": ccy,
                }
            elif -CB_MEETING_WARN_HOURS <= hours_diff < 0 and not best_warn:
                best_warn = {
                    "active": True,
                    "severity": "warn",
                    "reason": f"{ccy} 中銀会合まで {abs(hours_diff):.0f}h（要注意期間）",
                    "currency": ccy,
                }

    if best_blackout:
        return best_blackout
    if best_warn:
        return best_warn
    return {"active": False, "severity": "none", "reason": ""}


def _hours_between_iso(iso_start: str, dt_end: datetime) -> float:
    """ISO形式の文字列と datetime の差分時間を返す（Discord通知用）"""
    if not iso_start:
        return 0.0
    try:
        start = datetime.fromisoformat(iso_start)
        if start.tzinfo is None:
            start = start.replace(tzinfo=timezone.utc)
        return round((dt_end - start).total_seconds() / 3600, 1)
    except Exception:
        return 0.0

PAIR_API = {
    "USDJPY": ("USD", "JPY"), "EURJPY": ("EUR", "JPY"),
    "GBPJPY": ("GBP", "JPY"), "AUDJPY": ("AUD", "JPY"),
    "NZDJPY": ("NZD", "JPY"), "CADJPY": ("CAD", "JPY"),
    "CHFJPY": ("CHF", "JPY"), "SGDJPY": ("SGD", "JPY"),
    "HKDJPY": ("HKD", "JPY"), "CNYJPY": ("CNY", "JPY"),
    "MXNJPY": ("MXN", "JPY"), "TRYJPY": ("TRY", "JPY"),
    "ZARJPY": ("ZAR", "JPY"), "INRJPY": ("INR", "JPY"),
    "EURUSD": ("EUR", "USD"), "GBPUSD": ("GBP", "USD"),
    "AUDUSD": ("AUD", "USD"), "NZDUSD": ("NZD", "USD"),
    "USDCAD": ("USD", "CAD"), "USDCHF": ("USD", "CHF"),
    "EURGBP": ("EUR", "GBP"), "EURAUD": ("EUR", "AUD"),
    # 2026-07-20追加: SBI証券の取扱ペアに合わせて拡張
    # (Frankfurter API のSEK/NOK/BRL/PLN/KRW対応を事前確認済み)
    "SEKJPY": ("SEK", "JPY"), "NOKJPY": ("NOK", "JPY"),
    "BRLJPY": ("BRL", "JPY"), "PLNJPY": ("PLN", "JPY"),
    "KRWJPY": ("KRW", "JPY"),
    "GBPAUD": ("GBP", "AUD"), "GBPCHF": ("GBP", "CHF"),
    "AUDCHF": ("AUD", "CHF"), "EURCHF": ("EUR", "CHF"),
    "AUDNZD": ("AUD", "NZD"), "EURNZD": ("EUR", "NZD"),
    "USDCNY": ("USD", "CNY"),
}

PAIR_LABEL = {
    "USDJPY": "USD/JPY", "EURJPY": "EUR/JPY", "GBPJPY": "GBP/JPY",
    "AUDJPY": "AUD/JPY", "NZDJPY": "NZD/JPY", "CADJPY": "CAD/JPY",
    "CHFJPY": "CHF/JPY", "SGDJPY": "SGD/JPY", "HKDJPY": "HKD/JPY",
    "CNYJPY": "CNY/JPY", "MXNJPY": "MXN/JPY", "TRYJPY": "TRY/JPY",
    "ZARJPY": "ZAR/JPY", "INRJPY": "INR/JPY", "EURUSD": "EUR/USD",
    "GBPUSD": "GBP/USD", "AUDUSD": "AUD/USD", "NZDUSD": "NZD/USD",
    "USDCAD": "USD/CAD", "USDCHF": "USD/CHF", "EURGBP": "EUR/GBP",
    "EURAUD": "EUR/AUD",
    # 2026-07-20追加
    "SEKJPY": "SEK/JPY", "NOKJPY": "NOK/JPY", "BRLJPY": "BRL/JPY",
    "PLNJPY": "PLN/JPY", "KRWJPY": "KRW/JPY", "GBPAUD": "GBP/AUD",
    "GBPCHF": "GBP/CHF", "AUDCHF": "AUD/CHF", "EURCHF": "EUR/CHF",
    "AUDNZD": "AUD/NZD", "EURNZD": "EUR/NZD", "USDCNY": "USD/CNY",
}

API_SYMBOLS = "USD,JPY,GBP,AUD,NZD,CAD,CHF,SGD,HKD,CNY,MXN,TRY,ZAR,INR,SEK,NOK,BRL,PLN,KRW"


# === 価格表示桁数の一元管理 ===
# JPYクロス: 小数3桁 / それ以外: 小数6桁
# 取引シグナルとして適切な精度で出力するための共通ヘルパー
def pair_decimals(pair: str) -> int:
    """ペアに応じた小数桁数を返す。JPYクロス=3、それ以外=6。"""
    return 3 if pair and pair.upper().endswith("JPY") else 6


def fmt_price(pair: str, value) -> str:
    """価格を pair に応じた固定桁数の文字列で返す。None/非数は '—'。"""
    if value is None:
        return "—"
    try:
        return f"{float(value):.{pair_decimals(pair)}f}"
    except (TypeError, ValueError):
        return str(value)


# === スプレッド代表値テーブル ===
# 単位: pip（JPYクロスは 0.01、非JPYは 0.0001 が 1pip）
# Frankfurter は中値のみ提供のため、bid/ask の実効スプレッドはここで補正する。
# 2026-07-20: SBI証券公式のコアタイム(9:00-翌3:00)固定スプレッドが判明した5ペアは
# 実測値に更新（★マーク）。それ以外は引き続き類似ペアからの推定値。
SPREAD_PIPS = {
    # メジャー JPY（タイト）★SBI公式コアタイム値
    "USDJPY": 0.2, "EURJPY": 0.5, "GBPJPY": 0.9,
    # 流動性中程度 JPY
    "AUDJPY": 0.6,   # ★SBI公式（旧推定1.5から実測値へ更新）
    "NZDJPY": 2.0, "CADJPY": 1.5, "CHFJPY": 2.0,
    "SGDJPY": 2.5, "HKDJPY": 3.0,
    # エキゾチック JPY（ワイド・要警戒）
    "CNYJPY": 5.0, "MXNJPY": 5.0, "ZARJPY": 10.0,
    "INRJPY": 8.0, "TRYJPY": 30.0,
    # メジャー非JPY
    "EURUSD": 0.4,   # ★SBI公式（旧推定0.5から実測値へ更新）
    "GBPUSD": 1.5, "AUDUSD": 1.0, "NZDUSD": 2.0,
    "USDCAD": 1.5, "USDCHF": 2.0,
    "EURGBP": 1.5, "EURAUD": 3.0,
    # 2026-07-20追加: SBI証券の取扱ペア拡張分
    # 注意: 実測値ではなく類似ペアからの初期推定値（要検証・実運用で乖離すれば調整）
    "SEKJPY": 5.0, "NOKJPY": 5.0,          # マイナーJPYクロス（CNYJPY/MXNJPY相当）
    "BRLJPY": 12.0,                          # 高ボラエキゾチック（ZARJPY同等〜やや上）
    "PLNJPY": 6.0, "KRWJPY": 4.0,           # 中欧・アジア新興国
    "GBPAUD": 4.0, "GBPCHF": 3.5, "AUDCHF": 3.5,
    "EURCHF": 2.0,                           # 主要通貨クロスでタイト
    "AUDNZD": 2.5,                           # 流動性高い南半球クロス
    "EURNZD": 5.0,
    "USDCNY": 6.0,                           # 管理相場でワイド
}


def pip_size(pair: str) -> float:
    """1 pip の価格単位。JPYクロス=0.01、それ以外=0.0001。"""
    return 0.01 if pair and pair.upper().endswith("JPY") else 0.0001


def typical_spread_price(pair: str) -> float:
    """ペアの代表スプレッドを価格単位で返す。未定義は 0 で扱う。"""
    pips = SPREAD_PIPS.get(pair.upper() if pair else "", 0.0)
    return pips * pip_size(pair)


def typical_spread_pips(pair: str) -> float:
    """ペアの代表スプレッドを pip 単位で返す。"""
    return SPREAD_PIPS.get(pair.upper() if pair else "", 0.0)


LATEST_ENDPOINTS = [
    ("Frankfurter v1",
     f"https://api.frankfurter.dev/v1/latest?base=EUR&symbols={API_SYMBOLS}"),
    ("Frankfurter legacy",
     f"https://api.frankfurter.app/latest?from=EUR&to={API_SYMBOLS}"),
]


def http_get_json(url, timeout=15):
    req = urllib.request.Request(
        url, headers={"User-Agent": "fx-signal-monitor/1.0"}
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def fetch_latest_rates():
    for name, url in LATEST_ENDPOINTS:
        try:
            print(f"[INFO] Latest rates: trying {name}")
            data = http_get_json(url)
            if data and "rates" in data:
                rates = {"EUR": 1.0, **data["rates"]}
                pairs = {}
                for key, (frm, to) in PAIR_API.items():
                    if frm in rates and to in rates and rates[frm]:
                        pairs[key] = rates[to] / rates[frm]
                print(f"[OK] {len(pairs)} pairs from {name} ({data.get('date')})")
                return {"date": data.get("date"), "pairs": pairs}
        except Exception as e:
            print(f"[WARN] {name} failed: {e}")
    raise RuntimeError("All latest-rate endpoints failed")


def fetch_history(pair, days=280):
    frm, to = PAIR_API[pair]
    end = datetime.now(timezone.utc).date()
    start = end - timedelta(days=days)
    endpoints = [
        f"https://api.frankfurter.dev/v1/{start}..{end}?base={frm}&symbols={to}",
        f"https://api.frankfurter.app/{start}..{end}?from={frm}&to={to}",
    ]
    for url in endpoints:
        try:
            data = http_get_json(url)
            if data and "rates" in data:
                series = sorted(
                    (d, v[to]) for d, v in data["rates"].items() if to in v
                )
                closes = [v for _, v in series]
                if len(closes) >= 30:
                    return closes
        except Exception as e:
            print(f"[WARN] history failed for {pair}: {e}")
    return None


def sma(arr, n):
    return sum(arr[-n:]) / n if len(arr) >= n else None


def ema_series(arr, n):
    if len(arr) < n: return []
    k = 2 / (n + 1)
    out = [sum(arr[:n]) / n]
    for v in arr[n:]:
        out.append(v * k + out[-1] * (1 - k))
    return out


def rsi(prices, period=14):
    if len(prices) < period + 1: return None
    gains = losses = 0.0
    for i in range(1, period + 1):
        d = prices[i] - prices[i - 1]
        gains += max(d, 0); losses += max(-d, 0)
    avg_g = gains / period
    avg_l = losses / period
    for i in range(period + 1, len(prices)):
        d = prices[i] - prices[i - 1]
        avg_g = (avg_g * (period - 1) + max(d, 0)) / period
        avg_l = (avg_l * (period - 1) + max(-d, 0)) / period
    if avg_l == 0: return 100.0
    return 100 - 100 / (1 + avg_g / avg_l)


def macd_now(prices):
    if len(prices) < 35: return None, None
    ema12 = ema_series(prices, 12)
    ema26 = ema_series(prices, 26)
    offset = len(ema12) - len(ema26)
    macd_arr = [ema12[i + offset] - ema26[i] for i in range(len(ema26))]
    sig_arr = ema_series(macd_arr, 9)
    return macd_arr[-1], (sig_arr[-1] if sig_arr else None)


def atr_calc(prices, period=14):
    if len(prices) < period + 1: return None
    tr = [abs(prices[i] - prices[i - 1]) for i in range(1, len(prices))]
    a = sum(tr[:period]) / period
    for i in range(period, len(tr)):
        a = (a * (period - 1) + tr[i]) / period
    return a


def compute_ta_score(price, prices):
    dma200 = sma(prices, 200) or sma(prices, len(prices))
    dma50 = sma(prices, 50) or sma(prices, min(50, len(prices)))
    rsi_v = rsi(prices, 14)
    macd_v, macd_sig = macd_now(prices)
    atr_v = atr_calc(prices, 14)

    # === 既存ロジック ===
    dma_score = 50
    if dma200:
        if price > dma200: dma_score = 70
        elif price < dma200: dma_score = 30
        if dma50 and dma50 > dma200 * 1.005:
            dma_score = min(95, dma_score + 15)
        if dma50 and dma50 < dma200 * 0.995:
            dma_score = max(5, dma_score - 15)

    macd_score = 50
    if macd_v is not None and macd_sig is not None:
        diff = macd_v - macd_sig
        if diff > 0 and macd_v > 0: macd_score = 85
        elif diff > 0: macd_score = 65
        elif diff < 0 and macd_v < 0: macd_score = 15
        else: macd_score = 35

    rsi_score = 50
    if rsi_v is not None:
        if rsi_v >= 70: rsi_score = 20
        elif rsi_v <= 30: rsi_score = 80
        elif rsi_v > 55: rsi_score = 65
        elif rsi_v < 45: rsi_score = 35

    # === ダマシ低減のための新フィルタ（2026-06追加） ===
    # 1. トレンド強度（DMA50とDMA200の乖離率）
    trend_strength = 0
    if dma200 and dma50:
        separation = abs(dma50 - dma200) / dma200
        if separation >= 0.018:          # 1.8%以上 → 強いトレンド
            trend_strength = 18
        elif separation >= 0.012:        # 1.2%以上 → 普通のトレンド
            trend_strength = 10
        elif separation < 0.005:         # 0.5%未満 → ほぼレンジ（大幅減点）
            trend_strength = -20

    # 2. ATRボラティリティ品質（低ボラ = ダマシ多発）
    atr_quality = 0
    if atr_v and len(prices) >= 40:
        recent_atrs = []
        for i in range(len(prices) - 35, len(prices) - 14):
            a = atr_calc(prices[i:i+20], 14)
            if a: recent_atrs.append(a)
        if recent_atrs:
            avg_atr = sum(recent_atrs) / len(recent_atrs)
            ratio = atr_v / avg_atr if avg_atr > 0 else 1.0
            if ratio < 0.65:             # 明らかに低ボラ
                atr_quality = -25
            elif ratio > 1.4:            # ボラ拡大中
                atr_quality = 12

    # 3. 直近ブレイクアウト確認（20期間高値/安値）
    breakout_bonus = 0
    if len(prices) >= 20:
        recent_high = max(prices[-20:])
        recent_low = min(prices[-20:])
        if price >= recent_high * 0.997:     # 高値近辺 or 更新
            breakout_bonus = 15
        elif price <= recent_low * 1.003:    # 安値近辺 or 更新
            breakout_bonus = 15

    # 4. RSIダイバージェンス検知（2026-06-22追加）
    # 価格方向とRSI方向が乖離している場合 = トレンド転換の先行シグナル
    # ノイズ除去: RSI変化が±5pt以上の場合のみ発火（誤発火防止）
    divergence_adj = 0
    if len(prices) >= 25 and rsi_v is not None:
        rsi_prev = rsi(prices[:-10], 14)  # 10バー前のRSI
        if rsi_prev is not None:
            rsi_delta = rsi_v - rsi_prev              # RSI変化量
            price_rose = prices[-1] > prices[-10]     # 直近10バーで価格上昇？
            # 弱気ダイバージェンス: 価格↑ + RSI有意に低下(-5pt以上)
            if price_rose and rsi_delta < -5 and rsi_v > 45:
                divergence_adj = -18
            # 強気ダイバージェンス: 価格↓ + RSI有意に上昇(+5pt以上)
            elif not price_rose and rsi_delta > +5 and rsi_v < 55:
                divergence_adj = +18

    # 5. EMA20短期トレンド確認（2026-06-22追加）
    # DMA50/200 の長期トレンドに加え、EMA20/50 の短期アライメントを確認
    ema_short_bonus = 0
    if len(prices) >= 50:
        e20s = ema_series(prices, 20)
        e50s = ema_series(prices, 50)
        if e20s and e50s:
            e20 = e20s[-1]; e50 = e50s[-1]
            if price > e20 > e50:                           # 強い上昇トレンド
                ema_short_bonus = +12
            elif price < e20 < e50:                         # 強い下降トレンド
                ema_short_bonus = -12
            elif abs(e20 - e50) / e50 < 0.002:             # EMAが絡み合い = レンジ
                ema_short_bonus = -8

    # 総合TAスコア（全フィルタ反映）
    base_score = (dma_score + macd_score + rsi_score) / 3
    adjusted_score = (base_score + trend_strength + atr_quality
                      + breakout_bonus + divergence_adj + ema_short_bonus)
    ta_score = max(5, min(98, round(adjusted_score, 1)))

    return {
        "ta_score": ta_score,
        "dma_score": dma_score,
        "macd_score": macd_score,
        "rsi_score": rsi_score,
        "trend_strength": trend_strength,
        "atr_quality": atr_quality,
        "breakout_bonus": breakout_bonus,
        "divergence_adj": divergence_adj,      # 2026-06-22: RSIダイバージェンス
        "ema_short_bonus": ema_short_bonus,    # 2026-06-22: EMA20短期トレンド
        # 価格スケールの指標(DMA/MACD/ATR)はペア別精度で丸める
        # → ここでは pair が未確定なので一旦5桁で保持し、evaluate_full で再丸めする
        "dma200": round(dma200, 5) if dma200 else None,
        "dma50": round(dma50, 5) if dma50 else None,
        "rsi": round(rsi_v, 2) if rsi_v else None,
        "macd": round(macd_v, 5) if macd_v else None,
        "macd_signal": round(macd_sig, 5) if macd_sig else None,
        "atr": round(atr_v, 5) if atr_v else None,
    }


def evaluate_full(pair, price, prices, cb_rates, sentiment, now):
    ta = compute_ta_score(price, prices)
    fa = compute_fa_score(pair, PAIR_API, cb_rates)

    ta_sign = 1 if ta["ta_score"] > 50 else (-1 if ta["ta_score"] < 50 else 0)
    fa_sign = 1 if fa["direction"] == "buy" else (-1 if fa["direction"] == "sell" else 0)
    agree = ta_sign == fa_sign and ta_sign != 0
    conflict = ta_sign != fa_sign and ta_sign != 0 and fa_sign != 0

    # ダマシ低減のため閾値を厳しめに設定（2026-06改訂）
    if agree and ta["ta_score"] >= 78 and fa["score"] >= 68:
        stars = 5; verdict = "◎ 高信頼ロング" if fa_sign > 0 else "◎ 高信頼ショート"
        direction = "LONG" if fa_sign > 0 else "SHORT"
    elif agree and ta["ta_score"] >= 65 and fa["score"] >= 58:
        stars = 4; verdict = "○ ロング条件成立" if fa_sign > 0 else "○ ショート条件成立"
        direction = "LONG" if fa_sign > 0 else "SHORT"
    elif agree and ta["ta_score"] <= 22 and fa["score"] <= 32:
        stars = 5; verdict = "◎ 高信頼ショート"; direction = "SHORT"
    elif agree and ta["ta_score"] <= 35 and fa["score"] <= 42:
        stars = 4; verdict = "○ ショート条件成立"; direction = "SHORT"
    elif conflict:
        stars = 1; verdict = "⚠ 見送り（FA/TA不一致）"; direction = "NO_TRADE"
    elif fa_sign == 0 and ta_sign == 0:
        stars = 2; verdict = "— レンジ"; direction = "NO_TRADE"
    else:
        stars = 2; verdict = "△ 弱シグナル"
        direction = "LIGHT_" + ("LONG" if (ta_sign + fa_sign) > 0 else "SHORT")

    # ペア別精度で価格と価格スケール指標を再丸め
    _d = pair_decimals(pair)
    ta_rounded = dict(ta)
    for _k in ("dma200", "dma50", "macd", "macd_signal", "atr"):
        if ta_rounded.get(_k) is not None:
            ta_rounded[_k] = round(ta_rounded[_k], _d)
    result = {
        "pair": pair, "label": PAIR_LABEL[pair], "price": round(price, _d),
        **ta_rounded,
        "fa_score": fa["score"], "fa_direction": fa["direction"],
        "fa_rate_diff": fa["rate_diff"], "fa_detail": fa["detail"],
        "stars": stars, "verdict": verdict, "direction": direction,
        "pair_type": classify_pair(pair),
    }

    event_check = check_event_proximity(pair, now)
    if event_check["status"] == "block":
        result["original_stars"] = stars
        result["stars"] = 1
        result["verdict"] = "⏸ イベント前自動見送り"
        result["direction"] = "WAIT_EVENT"
        result["event_warning"] = event_check["reason"]
        result["event_info"] = event_check["event"]
    elif event_check["status"] == "warn":
        result["event_warning"] = event_check["reason"]
        result["event_info"] = event_check["event"]

    result = apply_sentiment_filter(pair, result, sentiment)
    result = apply_geopolitical_filter(pair, result)
    result["upcoming_events"] = upcoming_events_for(pair, hours_ahead=168)
    return result


STATE_FILE = "docs/last_signals.json"


def load_previous_state():
    if not os.path.exists(STATE_FILE): return {}
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f).get("signals", {})
    except Exception:
        return {}


def save_current_state(results, sentiment, us_yields, cb_rates,
                       obsidian_data=None, currency_strength=None,
                       portfolio_risk=None):
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    state = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "signals": {r["pair"]: r["stars"] for r in results},
        "results": results,
        "sentiment": sentiment,
        "us_yields": us_yields,
        "central_bank_rates": cb_rates,
    }
    if obsidian_data:
        state["obsidian_summary"] = {
            "signal_rules_count": len(obsidian_data.get("signal_rules", [])),
            "analyses_count": len(obsidian_data.get("analyses", [])),
            "journals_count": len(obsidian_data.get("journals", [])),
            "lessons_count": len(obsidian_data.get("lessons", [])),
        }
    if currency_strength:
        state["currency_strength"] = currency_strength
    if portfolio_risk:
        state["portfolio_risk"] = portfolio_risk
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2, default=str)


def detect_changes(current, previous_stars):
    newly = []; upgraded = []
    is_first = not previous_stars
    for r in current:
        prev = previous_stars.get(r["pair"], 0)
        if r["stars"] >= 4:
            if is_first or prev < 4: newly.append(r)
            elif r["stars"] == 5 and prev == 4: upgraded.append(r)
    return newly, upgraded, is_first


def stars_to_text(n):
    return "★" * n + "☆" * (5 - n)


def send_discord(webhook_url, newly, upgraded, is_first, all_results,
                 sentiment, currency_strength=None, portfolio_risk=None,
                 trade_update=None, open_trades=None,
                 drawdown=None, ai_commentary=None, ambush_alerts=None,
                 rate_warnings=None, latest_pairs=None):
    if not webhook_url:
        print("[INFO] Discord webhook not configured")
        return False

    jst = datetime.now(timezone.utc) + timedelta(hours=9)
    timestamp = jst.strftime("%Y-%m-%d %H:%M JST")
    risk_mode = sentiment.get("risk_mode", "normal") if sentiment else "unknown"
    risk_emoji = {
        "panic": "🚨🚨🚨", "risk_off": "⚠️", "caution": "⚠",
        "normal": "🟢", "complacent": "🟡", "unknown": "❓"
    }.get(risk_mode, "")

    if is_first:
        title = f"🚀 FXシグナル監視開始 {risk_emoji}"
        desc = f"監視開始。★4以上: **{len(newly)}件** / 市場: **{risk_mode}**"
        color = 0xD4A574
    elif newly or upgraded:
        title = f"{risk_emoji} FXシグナル変化検出"
        desc = (
            f"新規★4以上: **{len(newly)}件** / ★4→5昇格: **{len(upgraded)}件**\n"
            f"市場モード: **{risk_mode.upper()}**"
        )
        if risk_mode in ("panic", "risk_off"):
            color = 0xF87171
        elif any(r["direction"].endswith("LONG") for r in newly + upgraded):
            color = 0x4ADE80
        else:
            color = 0xFBBF24
    else:
        # シグナル変化はないが、決済・ドローダウンで呼ばれたケース
        # （待ち伏せ・反発監視はデイトレ画面に集約したため中長期通知では扱わない）
        has_trade_now = bool(trade_update and trade_update.get("newly_closed"))
        has_dd_now = bool(drawdown and drawdown.get("alert"))

        if has_trade_now:
            title = f"{risk_emoji} 💼 シグナル決着"
            desc = f"市場モード: **{risk_mode.upper()}**"
            color = 0xD4A574
        elif has_dd_now:
            title = f"🛑 ドローダウン警告"
            desc = drawdown.get("message", "連敗を検知しました")
            color = 0xF87171
        else:
            # 本当に何もない場合のみ送信しない
            return False

    embeds = [{
        "title": title, "description": desc, "color": color,
        "url": PAGES_URL,
        "timestamp": jst.isoformat(),
        "footer": {"text": f"Currents FX Terminal L3 | {timestamp}"},
        "fields": []
    }]

    # 🔴 金利スタンス矛盾警告（最優先・手動更新リマインダー）
    if rate_warnings:
        warn_text = "\n".join(f"・{w['message']}" for w in rate_warnings[:5])
        embeds[0]["fields"].append({
            "name": f"🔴 金利スタンス見直し要 ({len(rate_warnings)}件)",
            "value": (warn_text[:900] +
                      "\n→ central_bank_rates.json のstanceを更新してください"),
            "inline": False
        })

    # ⑮ AI市況コメンタリー（最上部に表示）
    if ai_commentary:
        embeds[0]["fields"].append({
            "name": "🤖 AI市況コメンタリー",
            "value": ai_commentary[:1024],
            "inline": False
        })

    # ⑪ ドローダウン警告（重要なので上部に）
    if drawdown and drawdown.get("alert"):
        lv = drawdown.get("level", "warning")
        embeds[0]["fields"].append({
            "name": f"🛑 ドローダウン警告 [{lv.upper()}]",
            "value": (
                f"{drawdown.get('message','')}\n"
                f"直近{drawdown.get('recent_total',0)}件の損益: {drawdown.get('recent_pips',0):+.4f}\n"
                f"→ {drawdown.get('recommendation','')}"
            ),
            "inline": False
        })

    # 市場センチメント
    if sentiment:
        embeds[0]["fields"].append({
            "name": "🌐 市場センチメント",
            "value": (
                f"```\n"
                f"VIX: {sentiment.get('vix', 'N/A')} ({sentiment.get('vix_level', '?')})\n"
                f"DXY: {sentiment.get('dxy', 'N/A')} ({sentiment.get('dxy_trend', '?')})\n"
                f"米10y: {sentiment.get('us10y', 'N/A')}%\n"
                f"金: {sentiment.get('gold', 'N/A')}\n"
                f"```"
            ),
            "inline": False
        })

    # ① 通貨強弱メーター（上位3・下位3）
    if currency_strength:
        sorted_ccys = sorted(currency_strength.items(), key=lambda x: -x[1]["score"])
        strong = [f"{c}({v['score']:+.0f})" for c, v in sorted_ccys[:3]]
        weak = [f"{c}({v['score']:+.0f})" for c, v in sorted_ccys[-3:]]
        embeds[0]["fields"].append({
            "name": "💪 通貨強弱",
            "value": f"強: {' > '.join(strong)}\n弱: {' < '.join(reversed(weak))}",
            "inline": False
        })

    # ④ 相関リスク警告
    if portfolio_risk and portfolio_risk.get("warnings"):
        warnings_text = "\n".join(portfolio_risk["warnings"][:3])
        embeds[0]["fields"].append({
            "name": f"⚠ ポジション相関リスク [{portfolio_risk.get('risk_level','').upper()}]",
            "value": warnings_text + f"\n→ {portfolio_risk.get('recommendation', '')}",
            "inline": False
        })

    # ⑦ トレード情報（決済 + 状態変化 + 保有ポジション一覧）
    if trade_update:
        # ──（1）決済 ──
        newly_closed = trade_update.get("newly_closed", [])
        if newly_closed:
            reason_label = {
                "TP_HIT": "✅TP到達(+トレール開始)",
                "TRAIL_HIT": "🏆トレーリング決済",
                "BE_HIT": "⚖️BE+0.5R戻り",
                "TP3_HIT": "🏆TP3到達",  # 旧データ互換
                "TP2_HIT": "✅TP2到達",  # 旧データ互換
                "TP1_HIT": "✅TP1到達",  # 旧データ互換
                "SL_HIT": "❌SL到達",
                "SIGNAL_LOST": "➖シグナル消滅",
                "REVERSED": "🔄方向反転",
            }
            lines = []
            for t in newly_closed[:5]:
                rl = reason_label.get(t.get("exit_reason"), t.get("exit_reason", "?"))
                prefix = "+" if t.get("result") == "WIN" else ("-" if t.get("result") == "LOSS" else " ")
                _p = t.get("pair", "")
                _d = pair_decimals(_p)
                lines.append(
                    f"{prefix} {_p} {t['direction']} {rl}\n"
                    f"   {fmt_price(_p, t.get('entry_price'))} → {fmt_price(_p, t.get('exit_price'))} "
                    f"({t.get('pips',0):+.{_d}f}) 保有{t.get('hold_hours',0)}h"
                )
            embeds[0]["fields"].append({
                "name": f"💼 シグナル決着（{len(newly_closed)}件）",
                "value": "```diff\n" + "\n".join(lines) + "\n```",
                "inline": False
            })

        # ──（2）状態変化（TP到達でトレーリング有効化など）──
        state_changes = trade_update.get("state_changes", [])
        if state_changes:
            lines = []
            for sc in state_changes[:5]:
                t = sc["trade"]
                upd = sc["update"]
                _p = t.get("pair", "")
                if upd.get("tp_hit"):
                    lines.append(
                        f"🎯 {_p} {t['direction']} TP到達!\n"
                        f"   SLを BE+0.5R({fmt_price(_p, upd.get('sl'))}) へ移動 → トレーリング発動"
                    )
                elif "sl" in upd and "extreme_price" in upd:
                    lines.append(
                        f"📈 {_p} {t['direction']} トレール更新\n"
                        f"   高値/安値: {fmt_price(_p, upd.get('extreme_price'))} | SL: {fmt_price(_p, upd.get('sl'))}"
                    )
            if lines:
                embeds[0]["fields"].append({
                    "name": f"🔄 ポジション状態変化（{len(state_changes)}件）",
                    "value": "```\n" + "\n".join(lines) + "\n```",
                    "inline": False
                })

        # ──（3）保有中ポジション一覧（新機能！）──
        open_trades_dict = trade_update.get("open_trades", {}) or open_trades or {}
        if open_trades_dict:
            lines = []
            for pair, t in list(open_trades_dict.items())[:10]:
                cur_price = t.get("current_price") or t.get("entry_price")
                entry = t.get("entry_price")
                direction = t.get("direction", "")
                is_long = direction.endswith("LONG")
                # 含み損益（pips相当）
                if is_long:
                    unreal = cur_price - entry
                else:
                    unreal = entry - cur_price
                # アイコン
                if t.get("tp_hit"):
                    icon = "🎯"  # TP到達済み（トレーリング中）
                    phase = "トレール中"
                else:
                    icon = "💼"  # 保有中（TP未到達）
                    phase = "保有中"
                # 進捗（TP/SLまでの距離）
                tp = t.get("tp") or t.get("tp1")
                sl = t.get("sl")
                if tp is not None and sl is not None and entry is not None:
                    if is_long:
                        tp_pct = ((cur_price - entry) / (tp - entry) * 100) if tp != entry else 0
                    else:
                        tp_pct = ((entry - cur_price) / (entry - tp) * 100) if entry != tp else 0
                    progress = f"進捗{tp_pct:+.0f}%"
                else:
                    progress = ""
                # 保有時間
                try:
                    hold_h = _hours_between_iso(t.get("entry_time", ""), datetime.now(timezone.utc))
                    hold_str = f"{hold_h:.0f}h" if hold_h else ""
                except Exception:
                    hold_str = ""

                sign = "+" if unreal >= 0 else ""
                _d = pair_decimals(pair)
                sl_str = f"逆指値{fmt_price(pair, sl)}" if sl is not None else ""
                lines.append(
                    f"{icon} {pair} {direction[:5]} {phase} | "
                    f"@{fmt_price(pair, entry)}→{fmt_price(pair, cur_price)} "
                    f"({sign}{unreal:+.{_d}f}) {progress} {sl_str} {hold_str}"
                )
            # 💳 証拠金維持率（2026-07-20追加、SBI公式ロスカット基準=50%を可視化）
            margin_note = ""
            if latest_pairs is not None:
                mr = calc_maintenance_ratio(
                    load_virtual_account(), open_trades_dict, PAIR_API, latest_pairs
                )
                ratio = mr.get("maintenance_ratio")
                if ratio is not None:
                    alert_icon = "🚨" if ratio < LOSS_CUT_MAINTENANCE_RATIO * 1.5 else "✅"
                    margin_note = (
                        f"\n{alert_icon} 証拠金維持率: {ratio:.0f}%"
                        f"（資産評価額¥{mr['equity_jpy']:.0f} / 必要証拠金¥{mr['total_margin_jpy']:.0f}、"
                        f"ロスカット基準50%）"
                    )
            embeds[0]["fields"].append({
                "name": f"📋 保有中ポジション（{len(open_trades_dict)}件）",
                "value": "```\n" + "\n".join(lines) + "\n```" + margin_note,
                "inline": False
            })

    # trade_trackerが新規エントリー時に確定させたポジションサイジングのpair→値マップ
    # （2026-07-20追加、下記シグナルループでの二重計算防止用）
    _newly_opened_sizing = {
        t.get("pair"): t.get("position_sizing")
        for t in (trade_update or {}).get("newly_opened", [])
        if t.get("position_sizing")
    }

    # 各シグナル
    for r in newly:
        rate_diff_str = (
            f"{r.get('fa_rate_diff', 0):+.2f}%"
            if r.get("fa_rate_diff") is not None else "N/A"
        )
        regime = r.get("volatility_regime", {})
        staged = r.get("staged_tp", {})
        carry = r.get("carry_score", {})
        sr = r.get("support_resistance", {})
        interv = r.get("intervention_risk", {})
        strength_ctx = r.get("strength_context", {})

        # 基本情報
        value = (
            f"```\n"
            f"価格: {fmt_price(r['pair'], r['price'])}\n"
            f"方向: {r['direction']}\n"
            f"TA: {r['ta_score']}/100  FA: {r['fa_score']}/100\n"
            f"金利差: {rate_diff_str}\n"
        )

        # ② ボラレジーム + ③ 単一TP + トレーリング戦略
        if regime:
            value += f"ボラ: {regime.get('regime_label','')} (ATR比{regime.get('atr_ratio',1):.1f}倍)\n"
        if staged:
            _p = r["pair"]
            tp_main = staged.get("tp") or staged.get("tp1")
            tp_mode = staged.get("tp_mode", "")
            # スプレッド情報（中値ベース RR と 実効 RR の併記）
            sp_pips = staged.get("spread_pips", 0)
            rr_eff = staged.get("rr_effective")
            sp_ratio = staged.get("spread_atr_ratio", 0)
            spread_note = ""
            if sp_pips and rr_eff is not None:
                pct = sp_ratio * 100
                if pct > 10:
                    spread_note = f" ⚠スプレッド{sp_pips:.1f}pips({pct:.0f}%) 実効RR1:{rr_eff}"
                else:
                    spread_note = f" (spread {sp_pips:.1f}pips, 実効RR1:{rr_eff})"
            if tp_mode == "single_with_trail":
                # 新方式: 単一TP + トレーリング
                value += (
                    f"SL: {fmt_price(_p, staged.get('sl'))} | TP: {fmt_price(_p, tp_main)} (RR 1:{staged.get('rr_tp','?')}){spread_note}\n"
                    f"📍TP到達後: SL→BE+0.5R({fmt_price(_p, staged.get('be_target_after_tp'))}) "
                    f"+ トレール{staged.get('trail_atr_mult','3.0')}×ATR\n"
                    f"🎯理論最大: {fmt_price(_p, staged.get('max_target'))} (参考のみ)\n"
                )
                # 💰 ポジションサイジング（2026-07-20追加、シミュレーション口座連動）
                # trade_trackerが新規エントリー時に既に確定させた値があればそれを使う
                # （ここで再計算すると、既存ポジション控除後の証拠金余力を二重減算する
                # おそれがあるため）。無い場合（例: ★4→5昇格などtrade_tracker側で
                # 新規記録されていないケース）のみ、現在のopen_tradesを渡して見積もる。
                _sizing = _newly_opened_sizing.get(_p)
                if _sizing is None and latest_pairs is not None and staged.get("sl") is not None:
                    _sizing = calc_position_size(
                        _p, r.get("price"), staged.get("sl"), PAIR_API, latest_pairs,
                        open_trades=open_trades,
                    )
                if _sizing:
                    if _sizing.get("tradable"):
                        value += f"💰 推奨ロット: {_sizing['note']}\n"
                    else:
                        value += f"💰 {_sizing.get('note', '取引不可')}\n"
            else:
                # 旧方式（後方互換）
                value += (
                    f"SL: {fmt_price(_p, staged.get('sl'))} | "
                    f"TP1: {fmt_price(_p, staged.get('tp1'))} | "
                    f"TP2: {fmt_price(_p, staged.get('tp2'))} | "
                    f"TP3: {fmt_price(_p, staged.get('tp3'))}\n"
                    f"RR: 1:{staged.get('rr_tp1','?')} / 1:{staged.get('rr_tp2','?')} / 1:{staged.get('rr_tp3','?')}{spread_note}\n"
                )
        value += "```\n"
        value += f"📊 {r['fa_detail']}"

        # ① 通貨強弱コンテキスト
        if strength_ctx:
            value += f"\n💪 {strength_ctx.get('context', '')}"

        # ⑤ キャリースコア
        if carry and r.get("fa_rate_diff", 0) and r.get("fa_rate_diff", 0) > 0:
            value += (
                f"\n💰 キャリー: {carry.get('label','')} "
                f"(スコア{carry.get('carry_score','?'):.1f} | "
                f"SL回収{carry.get('breakeven_days','?')}日)"
            )

        # ⑥ SR
        if sr and sr.get("context"):
            value += f"\n📐 {sr.get('context','')}"

        # ⑧ 介入リスク
        if interv:
            risk_lv = interv.get("risk_level", "")
            if risk_lv in ("HIGH", "CRITICAL"):
                value += (
                    f"\n🚨 介入リスク{risk_lv} (スコア{interv.get('risk_score','?')}/100)\n"
                    f"   {interv.get('recommendation','')}"
                )

        if r.get("event_warning"):
            value += f"\n⚠ {r['event_warning']}"
        if r.get("sentiment_notes"):
            value += f"\n🌐 " + " / ".join(r["sentiment_notes"])
        # 💸 スプレッドフィルタ警告/注意
        if r.get("spread_filter_applied"):
            value += f"\n{r.get('spread_filter_reason', '')}"
        elif r.get("spread_caution"):
            value += f"\n{r.get('spread_caution_reason', '')}"

        # 🎯 エントリー有効性チェック（2026-06-25 追加）
        ev = validate_entry_for_result(r)
        if ev:
            value += f"\n{format_entry_block_short(ev, r['pair'])}"

        embeds[0]["fields"].append({
            "name": f"{stars_to_text(r['stars'])} {r['label']} - {r['verdict']}",
            "value": value[:1024], "inline": False
        })

    for r in upgraded:
        _p = r["pair"]
        staged = r.get("staged_tp", {})
        tp_sl = ""
        if staged:
            tp_main = staged.get("tp") or staged.get("tp1")
            if staged.get("tp_mode") == "single_with_trail":
                tp_sl = (
                    f"\nSL: {fmt_price(_p, staged.get('sl'))} | TP: {fmt_price(_p, tp_main)} (RR 1:{staged.get('rr_tp','?')})"
                    f"\nTP到達後→トレール {staged.get('trail_atr_mult','3.0')}×ATR"
                )
            else:
                tp_sl = (
                    f"\nSL: {fmt_price(_p, staged.get('sl'))} | "
                    f"TP1: {fmt_price(_p, staged.get('tp1'))} | "
                    f"TP2: {fmt_price(_p, staged.get('tp2'))} | "
                    f"TP3: {fmt_price(_p, staged.get('tp3'))}"
                )
        ev_up = validate_entry_for_result(r)
        ev_line = f"\n{format_entry_block_short(ev_up, _p)}" if ev_up else ""
        embeds[0]["fields"].append({
            "name": f"⬆ 昇格: {r['label']} ★4→★5",
            "value": f"```\n価格: {fmt_price(_p, r['price'])}  方向: {r['direction']}{tp_sl}\n```{ev_line}",
            "inline": False
        })

    # 🔗 ダッシュボードリンク（最後に必ず追加）
    embeds[0]["fields"].append({
        "name": "🔗 ダッシュボード",
        "value": (
            f"**[📊 L3 ダッシュボードを開く]({PAGES_URL})**\n"
            f"[🖥 分析ターミナル]({PAGES_URL}terminal.html) ・ "
            f"[⚡ デイトレ]({PAGES_URL}daytrade.html) ・ "
            f"[📋 ポジション管理]({PAGES_URL}position_manager.html)\n"
            f"[{{}} Raw JSON]({PAGES_URL}last_signals.json)"
        ),
        "inline": False
    })

    try:
        payload = json.dumps({"embeds": embeds}).encode("utf-8")
        req = urllib.request.Request(
            webhook_url, data=payload,
            headers={
                "Content-Type": "application/json",
                "User-Agent": "DiscordBot (fx-signal-monitor, 1.0)",
                "X-RateLimit-Precision": "millisecond",
            },
            method="POST"
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            print(f"[OK] Discord sent (HTTP {resp.status})")
            return True
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="ignore")[:200]
        print(f"[ERROR] Discord send failed: HTTP {e.code} | {body}")
        return False
    except Exception as e:
        print(f"[ERROR] Discord send failed: {e}")
        return False


def send_discord_pending(webhook_url, newly_created, newly_filled, expired, heartbeat=None):
    """
    指値待機シグナル専用のDiscord通知（2026-07-20追加）。
    1日3回（07:00/13:00/21:00 JST）の指値スキャンでの新規登録、
    毎時スキャンでの約定・失効を通知する。
    newly_created: {pair: order} — 今回の指値スキャンで新規登録した注文
    newly_filled:  [trade, ...]   — 今回約定してopen_tradesへ追加されたトレード
    expired:       {pair: order} — 有効期限切れで削除された注文
    heartbeat: {open_count, pending_count} または None（2026-07-21追加）。
      3件とも0件のとき、Noneなら何も送らず「本当に動いているか」が外から
      分からなくなるため、limitモード（1日3回）呼び出し時のみ渡し、
      短い「変化なし」メッセージを送って死活監視を兼ねる。
    """
    if not webhook_url:
        return False
    has_update = bool(newly_created or newly_filled or expired)
    if not has_update and heartbeat is None:
        return False

    jst = datetime.now(timezone.utc) + timedelta(hours=9)
    timestamp = jst.strftime("%Y-%m-%d %H:%M JST")
    fields = []

    if newly_created:
        lines = []
        for pair, order in newly_created.items():
            _p = pair
            valid_until_jst = "?"
            try:
                vu = datetime.fromisoformat(order["valid_until"]) + timedelta(hours=9)
                valid_until_jst = vu.strftime("%m/%d %H:%M JST")
            except Exception:
                pass
            lines.append(
                f"📌 {_p} {order['direction']} ★{order.get('initial_stars','?')}\n"
                f"   指値: {fmt_price(_p, order.get('limit_price'))} "
                f"(現在値{fmt_price(_p, order.get('scan_price'))}から"
                f"{order.get('pullback_atr_mult', 0.3)}×ATR押し目)\n"
                f"   SL: {fmt_price(_p, order.get('sl'))} | TP: {fmt_price(_p, order.get('tp'))}\n"
                f"   有効期限: {valid_until_jst}まで（未到達なら自動失効）"
            )
        fields.append({
            "name": f"📌 指値待機シグナル 新規登録（{len(newly_created)}件）",
            "value": "```\n" + "\n".join(lines) + "\n```",
            "inline": False,
        })

    if newly_filled:
        lines = []
        for t in newly_filled:
            _p = t.get("pair", "")
            sizing = t.get("position_sizing") or {}
            lines.append(
                f"✅ {_p} {t['direction']} 指値約定 @ {fmt_price(_p, t['entry_price'])}\n"
                f"   {sizing.get('note', '')}"
            )
        fields.append({
            "name": f"✅ 指値約定（{len(newly_filled)}件）",
            "value": "```\n" + "\n".join(lines) + "\n```",
            "inline": False,
        })

    if expired:
        lines = [
            f"⌛ {pair} {order.get('direction','')} "
            f"指値{fmt_price(pair, order.get('limit_price'))}（未到達のまま失効）"
            for pair, order in expired.items()
        ]
        fields.append({
            "name": f"⌛ 指値失効（{len(expired)}件）",
            "value": "```\n" + "\n".join(lines) + "\n```",
            "inline": False,
        })

    if has_update:
        embed = {
            "title": "📌 指値待機シグナル アップデート",
            "description": f"更新: {timestamp}",
            "color": 0x60A5FA,
            "fields": fields,
        }
    else:
        embed = {
            "title": "📌 指値待機シグナル（定期チェック・変化なし）",
            "description": (
                f"更新: {timestamp}\n"
                f"新規シグナル・約定・失効なし。ワークフローは正常稼働中です。\n"
                f"保有中ポジション: {heartbeat.get('open_count', 0)}件 / "
                f"指値待機中: {heartbeat.get('pending_count', 0)}件"
            ),
            "color": 0x94A3B8,
        }
    payload = {"embeds": [embed]}
    try:
        req = urllib.request.Request(
            webhook_url,
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "User-Agent": "DiscordBot (fx-signal-monitor, 1.0)",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            kind = "pending" if has_update else "pending heartbeat"
            print(f"[OK] Discord ({kind}) sent (HTTP {resp.status})")
            return True
    except Exception as e:
        print(f"[ERROR] Discord (pending) send failed: {e}")
        return False


def send_email(smtp_host, smtp_port, smtp_user, smtp_pass,
               from_addr, to_addr, newly, upgraded, is_first,
               all_results, sentiment, us_yields):
    """
    シグナル通知メール送信（2026-06-25 モバイル最適化 + エントリー有効性追加）

    スマートフォン通知での視認性を最優先に設計:
      - Subject: 即読み可能な1行サマリー（ペア名・方向・星数）
      - Body: エントリー上限価格を各シグナルの先頭に配置
      - HTML版: 重要情報を色分け表示
    """
    if not all([smtp_host, smtp_user, smtp_pass, from_addr, to_addr]):
        print("[INFO] Email not configured")
        return False

    jst = datetime.now(timezone.utc) + timedelta(hours=9)
    timestamp = jst.strftime("%Y-%m-%d %H:%M JST")
    risk_mode = sentiment.get("risk_mode", "?") if sentiment else "?"
    risk_emoji = {
        "panic": "🚨", "risk_off": "⚠️", "caution": "⚠",
        "normal": "🟢", "complacent": "🟡",
    }.get(risk_mode, "🔵")

    # ── Subject: スマホのプッシュ通知で内容がわかる1行 ──────────────────
    if is_first:
        subject = f"[FX] 監視開始 {risk_emoji} ★4以上{len(newly)}件 / {risk_mode}"
    else:
        # 新規シグナルの先頭1〜2件を件名に含める
        sig_parts = []
        for r in (newly + upgraded)[:2]:
            dir_short = "↑" if "LONG" in r.get("direction", "") else "↓"
            sig_parts.append(f"{r['pair']}{dir_short}★{r.get('stars','?')}")
        sig_str = " ".join(sig_parts) if sig_parts else "変化なし"
        subject = (
            f"[FX] {risk_emoji}{sig_str} / 新規{len(newly)}昇格{len(upgraded)} {timestamp}"
        )

    # ── 各シグナルのテキストブロック ─────────────────────────────────────
    def fmt_signal_block(r, label_prefix=""):
        _p = r["pair"]
        staged = r.get("staged_tp", {})
        regime = r.get("volatility_regime", {})
        carry = r.get("carry_score", {})
        sr = r.get("support_resistance", {})
        interv = r.get("intervention_risk", {})
        dir_arrow = "▲ LONG" if "LONG" in r.get("direction", "") else "▼ SHORT"

        lines = [
            "━" * 50,
            f"{label_prefix}{stars_to_text(r['stars'])}  {_p}  {dir_arrow}",
            f"価格: {fmt_price(_p, r['price'])}  |  {r.get('verdict','')}",
            f"TA={r['ta_score']}/100  FA={r['fa_score']}/100  金利差"
            + (f"{r.get('fa_rate_diff',0):+.2f}%" if r.get('fa_rate_diff') is not None else "N/A"),
            "",
        ]

        # SL / TP（最重要情報）
        if staged:
            tp_mode = staged.get("tp_mode", "")
            tp_main = staged.get("tp") or staged.get("tp1")
            rr = staged.get("rr_tp") or staged.get("rr_tp1") or "?"
            sp_pips = staged.get("spread_pips_dynamic") or staged.get("spread_pips", 0)
            rr_eff = staged.get("rr_effective")
            lines.append("【価格目標】")
            lines.append(f"  SL: {fmt_price(_p, staged.get('sl'))}")
            if tp_mode == "single_with_trail":
                lines.append(f"  TP: {fmt_price(_p, tp_main)}  (RR 1:{rr})")
                lines.append(f"  TP後→トレール {staged.get('trail_atr_mult','3.0')}×ATR")
                lines.append(f"  理論最大: {fmt_price(_p, staged.get('max_target'))}")
            else:
                lines.append(f"  TP1:{fmt_price(_p, staged.get('tp1'))}  TP2:{fmt_price(_p, staged.get('tp2'))}")
            if sp_pips and rr_eff is not None:
                pct = staged.get("spread_atr_ratio", 0) * 100
                lines.append(f"  スプレッド: {sp_pips:.1f}pips ({pct:.0f}%) 実効RR 1:{rr_eff}")
            lines.append("")

        # ▼ エントリー有効性チェック（新機能 2026-06-25）
        ev = validate_entry_for_result(r)
        if ev:
            lines.append(format_entry_block(ev, _p))
            lines.append("")

        # その他分析
        if regime:
            lines.append(f"ボラ: {regime.get('regime_label','')} (ATR比{regime.get('atr_ratio',1):.1f}x)")
        lines.append(f"FA: {r.get('fa_detail','')}")
        if carry and r.get("fa_rate_diff", 0) and r.get("fa_rate_diff", 0) > 0:
            lines.append(f"キャリー: {carry.get('label','')} / SL回収{carry.get('breakeven_days','?')}日")
        if sr and sr.get("context"):
            lines.append(f"SR: {sr.get('context','')}")
        if interv and interv.get("risk_level") in ("HIGH", "CRITICAL"):
            lines.append(f"🚨 介入リスク: {interv.get('risk_label','')} ({interv.get('risk_score',0)}/100)")
        if r.get("event_warning"):
            lines.append(f"⚠ {r['event_warning']}")

        return "\n".join(lines)

    # ── メール本文 ─────────────────────────────────────────────────────────
    body_lines = [
        "=" * 55,
        f"  FX売買シグナル通知  {timestamp}",
        f"  市場モード: {risk_emoji} {risk_mode.upper()}",
        "=" * 55,
        "",
    ]

    # 市場センチメント（コンパクト）
    if sentiment:
        vix = sentiment.get("vix", "?")
        dxy = sentiment.get("dxy", "?")
        body_lines += [
            f"VIX: {vix} / DXY: {dxy} / 米10y: {sentiment.get('us10y','?')}%",
            "",
        ]

    if newly:
        body_lines.append("【★4以上 新規シグナル】")
        body_lines.append("")
        for r in newly:
            body_lines.append(fmt_signal_block(r, label_prefix="[新規] "))
            body_lines.append("")

    if upgraded:
        body_lines.append("【★4→★5 昇格】")
        body_lines.append("")
        for r in upgraded:
            body_lines.append(fmt_signal_block(r, label_prefix="[昇格] "))
            body_lines.append("")

    body_lines += [
        "=" * 55,
        f"  ダッシュボード: {PAGES_URL}",
        "  ※本通知は教育・研究目的。投資判断は自己責任で。",
        "  ※エントリー上限価格はシグナル時の計算値。発注前に現在価格を確認。",
        "=" * 55,
    ]

    plain_body = "\n".join(body_lines)

    # ── HTML版（スマホで色分け表示） ─────────────────────────────────────
    def _html_signal_card(r):
        _p = r["pair"]
        staged = r.get("staged_tp", {})
        ev = validate_entry_for_result(r)
        is_long = "LONG" in r.get("direction", "")
        dir_color = "#2ecc71" if is_long else "#e74c3c"
        dir_label = "▲ LONG" if is_long else "▼ SHORT"
        status = ev.get("status", "") if ev else ""
        status_color = {"ENTER": "#27ae60", "LIMIT": "#f39c12", "SKIP": "#c0392b"}.get(status, "#888")

        tp_main = staged.get("tp") or staged.get("tp1") if staged else None
        sl = staged.get("sl") if staged else None
        rr = staged.get("rr_tp") or staged.get("rr_tp1", "?") if staged else "?"

        ev_html = ""
        if ev and status != "SKIP":
            ev_html = f"""
            <div style="background:#f8f9fa;border-left:4px solid {status_color};
                        padding:8px 12px;margin:8px 0;border-radius:0 4px 4px 0;">
              <div style="font-weight:bold;color:{status_color};">
                {'✅ ENTER可' if status=='ENTER' else '⚠️ 指値推奨' if status=='LIMIT' else '🚫 SKIP'}
              </div>
              <div>最大ASK: <strong style="font-size:1.1em;">{ev.get('max_entry_exec','?')}</strong></div>
              <div>推奨指値(mid): {ev.get('limit_order_price','?')}</div>
              <div>スリッページ許容: {ev.get('slip_budget_pips',0):.1f}pips
                   | spread: {ev.get('spread_pips',0):.1f}pips</div>
              <div>RR: シグナル時1:{ev.get('rr_at_signal','?')} → MAX時1:{ev.get('rr_at_max_entry','?')}</div>
            </div>"""
        elif ev and status == "SKIP":
            ev_html = f"""
            <div style="background:#fdf0f0;border-left:4px solid #c0392b;
                        padding:8px 12px;margin:8px 0;border-radius:0 4px 4px 0;">
              <div style="font-weight:bold;color:#c0392b;">🚫 エントリー不可</div>
              <div>{ev.get('note','')}</div>
            </div>"""

        return f"""
        <div style="border:1px solid #ddd;border-radius:8px;padding:12px;
                    margin:12px 0;background:#fff;">
          <div style="font-size:1.2em;font-weight:bold;color:{dir_color};">
            {stars_to_text(r['stars'])} {_p} {dir_label}
          </div>
          <div style="color:#666;font-size:0.9em;">{r.get('verdict','')}</div>
          <div style="margin:8px 0;">
            <span style="background:#f1f2f6;padding:2px 6px;border-radius:3px;
                         font-size:0.85em;">TA {r['ta_score']}/100</span>
            <span style="background:#f1f2f6;padding:2px 6px;border-radius:3px;
                         font-size:0.85em;margin-left:4px;">FA {r['fa_score']}/100</span>
          </div>
          {"<div style='margin:4px 0;'>" +
           f"SL: <code>{fmt_price(_p, sl)}</code> &nbsp;|&nbsp; "
           f"TP: <code>{fmt_price(_p, tp_main)}</code> &nbsp;|&nbsp; RR 1:{rr}"
           + "</div>" if staged else ""}
          {ev_html}
          <div style="font-size:0.85em;color:#555;margin-top:6px;">{r.get('fa_detail','')}</div>
        </div>"""

    risk_bg = {"panic": "#ffe0e0", "risk_off": "#fff3e0"}.get(risk_mode, "#f0fff4")
    html_signals = "".join(_html_signal_card(r) for r in newly + upgraded)
    html_body = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
  body{{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
        max-width:600px;margin:0 auto;padding:16px;background:#f5f5f5;}}
  .header{{background:{risk_bg};border-radius:8px;padding:12px 16px;margin-bottom:16px;}}
  .footer{{font-size:0.8em;color:#888;margin-top:20px;text-align:center;}}
  code{{background:#f1f2f6;padding:1px 4px;border-radius:3px;}}
  a{{color:#2980b9;}}
</style></head>
<body>
  <div class="header">
    <div style="font-size:1.3em;font-weight:bold;">{risk_emoji} FXシグナル通知</div>
    <div style="color:#555;">{timestamp} / 市場: {risk_mode.upper()}</div>
    {"<div>VIX: " + str(sentiment.get('vix','?')) + " / DXY: " + str(sentiment.get('dxy','?')) + "</div>"
     if sentiment else ""}
  </div>

  {"<h3 style='margin:16px 0 4px;'>★4以上 新規シグナル</h3>" if newly else ""}
  {"".join(_html_signal_card(r) for r in newly)}

  {"<h3 style='margin:16px 0 4px;'>★4→★5 昇格</h3>" if upgraded else ""}
  {"".join(_html_signal_card(r) for r in upgraded)}

  <div class="footer">
    <a href="{PAGES_URL}">📊 ダッシュボードを開く</a><br>
    ※本通知は教育・研究目的。投資判断は自己責任で。<br>
    ※エントリー上限価格はシグナル時の計算値。発注前に現在価格を確認してください。
  </div>
</body></html>"""

    # ── 送信 ─────────────────────────────────────────────────────────────
    msg = MIMEMultipart("alternative")
    msg["Subject"] = Header(subject, "utf-8")
    msg["From"] = from_addr
    msg["To"] = to_addr
    msg.attach(MIMEText(plain_body, "plain", "utf-8"))
    msg.attach(MIMEText(html_body, "html", "utf-8"))   # HTML版（後勝ち）

    try:
        with smtplib.SMTP_SSL(smtp_host, int(smtp_port or 465), timeout=20) as s:
            s.login(smtp_user, smtp_pass)
            s.send_message(msg)
        print(f"[OK] Email sent to {to_addr}")
        return True
    except Exception as e:
        print(f"[ERROR] Email send failed: {e}")
        return False


def generate_html_report(results, sentiment, us_yields, cb_rates,
                         generated_at, currency_strength=None, portfolio_risk=None):
    jst = generated_at + timedelta(hours=9)
    sorted_results = sorted(results, key=lambda x: (-x["stars"], -x["ta_score"]))

    risk_mode = sentiment.get("risk_mode", "unknown") if sentiment else "unknown"
    risk_label_map = {
        "panic": "🚨 PANIC モード", "risk_off": "⚠ RISK-OFF モード",
        "caution": "⚠ CAUTION モード", "normal": "🟢 NORMAL モード",
        "complacent": "🟡 COMPLACENT モード", "unknown": "❓ UNKNOWN",
    }
    risk_label = risk_label_map.get(risk_mode, risk_mode)

    strong_count = sum(1 for r in results if r["stars"] >= 4)
    blocked_count = sum(1 for r in results if r.get("event_warning") or r.get("sentiment_notes"))
    long_count = sum(1 for r in results if r["direction"].endswith("LONG") and r["stars"] >= 4)
    short_count = sum(1 for r in results if r["direction"].endswith("SHORT") and r["stars"] >= 4)

    # 通貨強弱テーブル
    strength_html = ""
    if currency_strength:
        sorted_strength = sorted(currency_strength.items(), key=lambda x: -x[1]["score"])
        rows = []
        for ccy, info in sorted_strength:
            score = info["score"]
            bar_width = abs(score)
            bar_color = "#4ade80" if score >= 0 else "#f87171"
            rows.append(f"""
            <tr>
              <td style="font-family:var(--mono);font-weight:600;color:var(--gold)">{ccy}</td>
              <td>
                <div style="display:flex;align-items:center;gap:0.5rem">
                  <div style="width:{bar_width:.0f}px;height:8px;background:{bar_color};border-radius:2px;min-width:2px"></div>
                  <span style="font-family:var(--mono);font-size:0.8rem;color:{'#4ade80' if score>=0 else '#f87171'}">{score:+.1f}</span>
                </div>
              </td>
              <td style="font-size:0.8rem;color:var(--text-secondary)">{info['label']}</td>
              <td style="font-family:var(--mono);color:var(--text-muted);font-size:0.75rem">#{info['rank']}</td>
            </tr>""")
        strength_html = f"""
        <div class="section-head">
          <div class="section-num">Ⅰ.</div>
          <h2 class="section-title">通貨強弱メーター</h2>
          <span class="section-sub">Currency Strength · 30d</span>
        </div>
        <table class="data-table" style="margin-bottom:1.5rem">
          <thead><tr><th>通貨</th><th>強弱スコア</th><th>判定</th><th>ランク</th></tr></thead>
          <tbody>{''.join(rows)}</tbody>
        </table>"""

    # 相関リスクバナー
    portfolio_html = ""
    if portfolio_risk and portfolio_risk.get("warnings"):
        risk_lv = portfolio_risk.get("risk_level", "low")
        lv_color = {"high": "#f87171", "medium": "#fbbf24", "low": "#4ade80"}.get(risk_lv, "#94a3b8")
        warnings_html = "".join(f"<div>・{w}</div>" for w in portfolio_risk["warnings"])
        portfolio_html = f"""
        <div style="background:var(--bg-card);border:1px solid var(--border);border-left:4px solid {lv_color};
                    padding:1rem 1.5rem;margin-bottom:1.5rem;border-radius:4px">
          <div style="font-family:var(--jp-serif);font-size:1rem;color:{lv_color};margin-bottom:0.5rem">
            ⚠ ポジション相関リスク [{risk_lv.upper()}]
          </div>
          <div style="font-size:0.83rem;color:var(--text-secondary)">{warnings_html}</div>
          <div style="margin-top:0.5rem;font-size:0.8rem;color:var(--text-muted)">
            → {portfolio_risk.get('recommendation','')}
          </div>
        </div>"""

    # シグナルテーブル行
    rows_html = []
    for r in sorted_results:
        row_cls = (
            "buy" if r["direction"].endswith("LONG") else
            "sell" if r["direction"].endswith("SHORT") else
            "blocked" if r["direction"] in ("WAIT_EVENT", "BLOCKED_PANIC") else
            "neutral"
        )
        fa_diff = r.get("fa_rate_diff")
        diff_str = f"{fa_diff:+.2f}%" if fa_diff is not None else "—"

        badges = []
        if r.get("event_warning"):
            badges.append('<span class="badge badge-event">⏸EVT</span>')
        if r.get("sentiment_notes"):
            badges.append('<span class="badge badge-sent">🌐SENT</span>')
        regime = r.get("volatility_regime", {})
        if regime.get("regime") == "high":
            badges.append('<span class="badge" style="background:rgba(248,113,113,0.15);color:#f87171;border:1px solid rgba(248,113,113,0.3)">⚡高ボラ</span>')
        interv = r.get("intervention_risk", {})
        if interv.get("risk_level") in ("HIGH", "CRITICAL"):
            badges.append(f'<span class="badge" style="background:rgba(248,113,113,0.15);color:#f87171;border:1px solid rgba(248,113,113,0.3)">🚨介入{interv.get("risk_score",0)}</span>')
        # 💸 スプレッドバッジ
        _staged_for_badge = r.get("staged_tp", {})
        _sp_ratio = _staged_for_badge.get("spread_atr_ratio", 0) if _staged_for_badge else 0
        if _sp_ratio > 0.30:
            badges.append(f'<span class="badge" style="background:rgba(248,113,113,0.15);color:#f87171;border:1px solid rgba(248,113,113,0.3)">💸spread致命{_sp_ratio*100:.0f}%</span>')
        elif _sp_ratio > 0.10:
            badges.append(f'<span class="badge" style="background:rgba(251,191,36,0.15);color:#fbbf24;border:1px solid rgba(251,191,36,0.3)">💸spread{_sp_ratio*100:.0f}%</span>')

        # SR最近傍
        _p = r["pair"]
        sr = r.get("support_resistance", {})
        sr_str = ""
        if sr.get("nearest_resistance") and row_cls == "buy":
            nr = sr["nearest_resistance"]
            if nr["distance_pct"] < 0.5:
                sr_str = f'<div class="warn-line">⚠ レジスタンス({fmt_price(_p, nr["price"])})まで{nr["distance_pct"]:.2f}%</div>'
        elif sr.get("nearest_support") and row_cls == "sell":
            ns = sr["nearest_support"]
            if ns["distance_pct"] < 0.5:
                sr_str = f'<div class="warn-line">⚠ サポート({fmt_price(_p, ns["price"])})まで{ns["distance_pct"]:.2f}%</div>'

        # TP情報
        staged = r.get("staged_tp", {})
        tp_str = ""
        if staged:
            # スプレッド情報を併記（広い場合のみ表示）
            sp_pips = staged.get("spread_pips", 0)
            rr_mid = staged.get("rr_tp")
            rr_eff = staged.get("rr_effective")
            sp_ratio = staged.get("spread_atr_ratio", 0)
            spread_line = ""
            if sp_pips and sp_ratio > 0.05 and rr_eff is not None:
                color = "#f87171" if sp_ratio > 0.30 else ("#fbbf24" if sp_ratio > 0.10 else "var(--text-muted)")
                spread_line = (
                    f'<div style="font-family:var(--mono);font-size:0.7rem;color:{color};margin-top:0.15rem">'
                    f'💸 spread {sp_pips:.1f}pips ({sp_ratio*100:.0f}% of ATR) | '
                    f'実効RR 1:{rr_mid}→1:{rr_eff}'
                    f'</div>'
                )
            tp_str = (
                f'<div style="font-family:var(--mono);font-size:0.7rem;color:var(--text-muted);margin-top:0.2rem">'
                f'SL:{fmt_price(_p, staged.get("sl"))} TP1:{fmt_price(_p, staged.get("tp1"))} '
                f'TP2:{fmt_price(_p, staged.get("tp2"))} TP3:{fmt_price(_p, staged.get("tp3"))}'
                f'</div>'
                f'{spread_line}'
            )

        warn_html = " ".join(badges)
        event_html = ""
        if r.get("event_warning"):
            event_html = f'<div class="warn-line">⏸ {r["event_warning"]}</div>'
        if r.get("sentiment_notes"):
            event_html += '<div class="warn-line">🌐 ' + " / ".join(r["sentiment_notes"]) + '</div>'
        if r.get("spread_filter_applied"):
            event_html += f'<div class="warn-line">{r.get("spread_filter_reason","")}</div>'

        # 通貨強弱コンテキスト
        sc = r.get("strength_context", {})
        strength_ctx_str = ""
        if sc:
            base = sc.get("base_currency", "")
            quote = sc.get("quote_currency", "")
            bs = sc.get("base_score", 0)
            qs = sc.get("quote_score", 0)
            strength_ctx_str = (
                f'<div style="font-size:0.7rem;color:var(--text-muted);font-family:var(--mono)">'
                f'{base}({bs:+.0f}) vs {quote}({qs:+.0f})'
                f'</div>'
            )

        # キャリースコア
        carry = r.get("carry_score", {})
        carry_str = ""
        if carry and fa_diff and fa_diff > 0:
            carry_str = (
                f'<div style="font-size:0.7rem;color:var(--gold);font-family:var(--mono)">'
                f'💰キャリー:{carry.get("carry_score","?"):.1f} | {carry.get("breakeven_days","?")}日でSL回収'
                f'</div>'
            )

        rows_html.append(f"""
        <tr class="row-{row_cls}">
          <td class="stars-cell">{stars_to_text(r['stars'])}</td>
          <td class="pair-cell">
            <div class="pair-main">{r['label']} {warn_html}</div>
            {strength_ctx_str}{event_html}{sr_str}{tp_str}{carry_str}
          </td>
          <td class="num-cell">{fmt_price(_p, r['price'])}</td>
          <td class="num-cell score-ta">{r['ta_score']}</td>
          <td class="num-cell score-fa">{r['fa_score']}</td>
          <td class="num-cell">{diff_str}</td>
          <td class="num-cell">{r['rsi']}</td>
          <td class="verdict-cell">{r['verdict']}</td>
          <td class="fa-detail">{r['fa_detail']}</td>
        </tr>""")

    # 中央銀行金利テーブル
    stance_map = {
        "tighten": ("↑", "#4ade80", "引締"),
        "neutral": ("→", "#94a3b8", "中立"),
        "ease": ("↓", "#f87171", "緩和")
    }
    cb_rows_html = []
    for ccy, info in (cb_rates or {}).items():
        icon, color, label = stance_map.get(info.get("stance", "neutral"), ("?", "#94a3b8", "?"))
        # キャリースコアを表示（いずれかのJPYペアから取得）
        cb_rows_html.append(f"""
        <tr>
          <td class="ccy-cell">{ccy}</td>
          <td>{info.get('cb_name', '—')}</td>
          <td class="num-cell">{info.get('rate', '—')}%</td>
          <td style="color:{color};font-family:var(--mono)">{icon} {label}</td>
          <td class="meta-cell">{info.get('next_meeting', '—')}</td>
        </tr>""")

    # 経済指標
    upcoming_events = {}
    for r in results:
        for ev in (r.get("upcoming_events") or []):
            key = ev["date"] + "|" + ev["name"]
            if key not in upcoming_events or upcoming_events[key]["hours_until"] > ev["hours_until"]:
                upcoming_events[key] = ev
    event_list = sorted(upcoming_events.values(), key=lambda e: e["hours_until"])
    event_rows_html = []
    for ev in event_list[:20]:
        dt = datetime.fromisoformat(ev["date"].replace("Z", "+00:00"))
        jst_ev = dt + timedelta(hours=9)
        time_str = jst_ev.strftime("%m/%d %H:%M")
        imp = ev["importance"]
        event_rows_html.append(f"""
        <div class="event-row event-{imp}">
          <div class="event-time">{time_str}<small>+{ev['hours_until']:.1f}h</small></div>
          <div class="event-ccy">{ev['currency']}</div>
          <div class="event-name">{ev['name']}</div>
          <div class="event-imp imp-{imp}">{imp}</div>
        </div>""")

    return f"""<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<meta http-equiv="refresh" content="3600">
<title>FX Signal Monitor L3 Advanced · Dashboard</title>
<link href="https://fonts.googleapis.com/css2?family=Cormorant+Garamond:ital,wght@1,500;1,600&family=JetBrains+Mono:wght@400;500;700&family=Shippori+Mincho:wght@500;600;700&family=Zen+Kaku+Gothic+New:wght@400;500;700&display=swap" rel="stylesheet">
<style>
:root{{--bg-deep:#0a0e1a;--bg-surface:#111726;--bg-card:#141b2d;--bg-elev:#1a2235;--border:#2a3450;--border-soft:#1f273d;--text-primary:#e8edf7;--text-secondary:#9aa5b8;--text-muted:#5a6378;--gold:#d4a574;--buy:#4ade80;--buy-bg:rgba(74,222,128,0.08);--sell:#f87171;--sell-bg:rgba(248,113,113,0.08);--caution:#fbbf24;--neutral:#94a3b8;--display:'Cormorant Garamond','Shippori Mincho',serif;--jp:'Zen Kaku Gothic New',sans-serif;--mono:'JetBrains Mono',monospace;--jp-serif:'Shippori Mincho',serif;}}
*{{margin:0;padding:0;box-sizing:border-box;}}
body{{background:var(--bg-deep);color:var(--text-primary);font-family:var(--jp);line-height:1.6;min-height:100vh;}}
.container{{max-width:1400px;margin:0 auto;padding:2rem;}}
.app-header{{display:flex;justify-content:space-between;align-items:baseline;padding-bottom:1rem;border-bottom:1px solid var(--border);margin-bottom:2rem;flex-wrap:wrap;gap:1rem;}}
.brand{{display:flex;align-items:baseline;gap:1rem;}}
.brand-mark{{font-family:var(--display);font-size:2rem;font-weight:600;font-style:italic;color:var(--gold);}}
.brand-sub{{font-family:var(--jp-serif);font-size:0.78rem;color:var(--text-secondary);letter-spacing:0.15em;}}
.header-meta{{font-family:var(--mono);font-size:0.78rem;color:var(--text-secondary);}}
.live{{display:inline-block;background:var(--buy-bg);color:var(--buy);padding:0.2rem 0.6rem;border-radius:3px;border:1px solid rgba(74,222,128,0.3);font-size:0.7rem;font-weight:700;margin-right:0.5rem;}}
.top-nav{{display:flex;justify-content:space-between;align-items:center;padding:0.7rem 1.2rem;margin-bottom:1.5rem;background:var(--bg-card);border:1px solid var(--border);border-radius:4px;flex-wrap:wrap;gap:0.5rem;}}
.top-nav-left{{font-family:var(--mono);font-size:0.72rem;color:var(--text-muted);text-transform:uppercase;}}
.top-nav-links{{display:flex;gap:0.5rem;flex-wrap:wrap;}}
.nav-link{{display:inline-flex;align-items:center;gap:0.4rem;padding:0.45rem 0.9rem;border-radius:3px;font-family:var(--mono);font-size:0.78rem;text-decoration:none;transition:all 0.15s;border:1px solid var(--border);color:var(--text-secondary);background:var(--bg-elev);}}
.nav-link:hover{{background:var(--bg-surface);color:var(--gold);border-color:var(--gold);}}
.nav-link.active{{background:var(--buy-bg);color:var(--buy);border-color:rgba(74,222,128,0.4);}}
.risk-banner{{background:linear-gradient(135deg,var(--bg-card) 0%,var(--bg-elev) 100%);border:1px solid var(--border);border-left:4px solid var(--gold);padding:1.3rem 1.8rem;margin-bottom:1.5rem;border-radius:4px;}}
.risk-banner.panic{{border-left-color:var(--sell);}}
.risk-banner.risk_off,.risk-banner.caution{{border-left-color:var(--caution);}}
.risk-banner.normal{{border-left-color:var(--buy);}}
.risk-title{{font-family:var(--jp-serif);font-size:1.15rem;color:var(--gold);margin-bottom:0.4rem;}}
.risk-banner.normal .risk-title{{color:var(--buy);}}
.risk-banner.panic .risk-title{{color:var(--sell);}}
.risk-summary{{font-family:var(--mono);font-size:0.85rem;color:var(--text-secondary);}}
.stats-grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:1rem;margin-bottom:1.5rem;}}
.stat-card{{background:var(--bg-card);border:1px solid var(--border);border-radius:4px;padding:1.1rem;}}
.stat-label{{font-family:var(--mono);font-size:0.68rem;color:var(--text-muted);letter-spacing:0.1em;text-transform:uppercase;margin-bottom:0.3rem;}}
.stat-val{{font-family:var(--mono);font-size:1.6rem;font-weight:600;color:var(--text-primary);line-height:1;}}
.stat-sub{{font-size:0.75rem;color:var(--text-secondary);margin-top:0.3rem;font-family:var(--mono);}}
.stat-val.buy{{color:var(--buy);}} .stat-val.sell{{color:var(--sell);}} .stat-val.caution{{color:var(--caution);}}
.section-head{{display:flex;align-items:baseline;gap:1rem;margin:2rem 0 1rem;padding-bottom:0.6rem;border-bottom:1px solid var(--border-soft);}}
.section-num{{font-family:var(--display);font-style:italic;font-size:1.8rem;color:var(--gold);font-weight:500;line-height:1;}}
.section-title{{font-family:var(--jp-serif);font-size:1.15rem;font-weight:600;}}
.section-sub{{font-family:var(--mono);font-size:0.7rem;color:var(--text-muted);letter-spacing:0.15em;text-transform:uppercase;margin-left:auto;}}
.data-table{{width:100%;border-collapse:collapse;background:var(--bg-card);border:1px solid var(--border);border-radius:4px;overflow:hidden;}}
.data-table th{{background:var(--bg-elev);color:var(--gold);font-family:var(--jp-serif);font-weight:600;padding:0.9rem 1rem;text-align:left;font-size:0.78rem;border-bottom:1px solid var(--border);}}
.data-table td{{padding:0.75rem 1rem;border-bottom:1px solid var(--border-soft);font-size:0.82rem;}}
.data-table tr:last-child td{{border-bottom:none;}}
.data-table tr:hover td{{background:var(--bg-surface);}}
.stars-cell{{color:var(--gold);font-family:var(--mono);white-space:nowrap;}}
.pair-main{{font-family:var(--display);font-style:italic;font-size:1.05rem;font-weight:600;}}
.num-cell{{font-family:var(--mono);color:var(--text-secondary);}}
.score-ta{{color:var(--text-primary);}} .score-fa{{color:var(--gold);}}
.row-buy .verdict-cell{{color:var(--buy);}} .row-sell .verdict-cell{{color:var(--sell);}}
.row-neutral .verdict-cell{{color:var(--caution);}} .row-blocked{{opacity:0.7;}}
.verdict-cell{{font-weight:500;}}
.fa-detail{{color:var(--text-secondary);font-size:0.76rem;}}
.ccy-cell{{font-family:var(--mono);font-weight:600;color:var(--gold);}}
.meta-cell{{font-family:var(--mono);font-size:0.78rem;color:var(--text-secondary);}}
.badge{{display:inline-block;padding:0.15rem 0.45rem;border-radius:3px;font-family:var(--mono);font-size:0.65rem;font-weight:700;margin-left:0.3rem;}}
.badge-event{{background:rgba(251,191,36,0.15);color:var(--caution);border:1px solid rgba(251,191,36,0.3);}}
.badge-sent{{background:rgba(212,165,116,0.15);color:var(--gold);border:1px solid rgba(212,165,116,0.3);}}
.warn-line{{font-size:0.72rem;color:var(--caution);margin-top:0.25rem;font-family:var(--mono);}}
.event-row{{display:grid;grid-template-columns:100px 60px 1fr 90px;gap:1rem;align-items:center;padding:0.7rem 1rem;background:var(--bg-card);border:1px solid var(--border-soft);border-left:3px solid var(--gold);margin-bottom:0.4rem;border-radius:3px;font-size:0.85rem;}}
.event-row.event-critical{{border-left-color:var(--sell);}} .event-row.event-high{{border-left-color:var(--caution);}}
.event-time{{font-family:var(--mono);color:var(--gold);font-weight:600;}}
.event-time small{{display:block;font-size:0.7rem;color:var(--text-muted);}}
.event-ccy{{font-family:var(--mono);font-weight:600;}}
.event-imp{{font-family:var(--mono);font-size:0.7rem;text-transform:uppercase;text-align:right;font-weight:700;}}
.imp-critical{{color:var(--sell);}} .imp-high{{color:var(--caution);}} .imp-medium{{color:var(--text-muted);}}
.app-footer{{margin-top:2rem;padding-top:1rem;border-top:1px solid var(--border);color:var(--text-muted);font-size:0.74rem;font-family:var(--mono);text-align:center;}}
.app-footer em{{font-family:var(--display);font-style:italic;color:var(--gold);}}
@media(max-width:768px){{.container{{padding:1rem;}} .event-row{{grid-template-columns:1fr;}}}}
</style>
</head>
<body>
<div class="container">
  <nav class="top-nav">
    <div class="top-nav-left">◇ Currents FX Suite</div>
    <div class="top-nav-links">
      <a href="./" class="nav-link active"><span>📊</span><span>L3 ダッシュボード</span></a>
      <a href="./terminal.html" class="nav-link"><span>🖥</span><span>分析ターミナル</span></a>
      <a href="./daytrade.html" class="nav-link"><span>⚡</span><span>デイトレ</span></a>
      <a href="./position_manager.html" class="nav-link"><span>📋</span><span>ポジション管理</span></a>
      <a href="./last_signals.json" class="nav-link" target="_blank"><span>{{}}</span><span>Raw JSON</span></a>
    </div>
  </nav>
  <header class="app-header">
    <div class="brand">
      <span class="brand-mark">Currents</span>
      <span class="brand-sub">FX SIGNAL MONITOR · L3 ADVANCED</span>
    </div>
    <div class="header-meta">
      <span class="live">● LIVE</span>
      {jst.strftime('%Y-%m-%d %H:%M JST')} · Auto-refresh: 1h · 22 pairs · 8 Analytics
    </div>
  </header>
  <div class="risk-banner {risk_mode}">
    <div class="risk-title">{risk_label}</div>
    <div class="risk-summary">
      VIX: {sentiment.get('vix','—') if sentiment else '—'} ({sentiment.get('vix_level','?') if sentiment else '?'}) &nbsp;·&nbsp;
      DXY: {sentiment.get('dxy','—') if sentiment else '—'} &nbsp;·&nbsp;
      米10y: {sentiment.get('us10y','—') if sentiment else '—'}% &nbsp;·&nbsp;
      Gold: {sentiment.get('gold','—') if sentiment else '—'}
    </div>
  </div>
  {portfolio_html}
  <div class="stats-grid">
    <div class="stat-card"><div class="stat-label">★4以上</div><div class="stat-val {'buy' if strong_count>0 else ''}">{strong_count}</div><div class="stat-sub">/ 全{len(results)}ペア</div></div>
    <div class="stat-card"><div class="stat-label">ロング推奨</div><div class="stat-val buy">{long_count}</div><div class="stat-sub">高信頼ロング</div></div>
    <div class="stat-card"><div class="stat-label">ショート推奨</div><div class="stat-val sell">{short_count}</div><div class="stat-sub">高信頼ショート</div></div>
    <div class="stat-card"><div class="stat-label">取引控え</div><div class="stat-val caution">{blocked_count}</div><div class="stat-sub">イベント・センチメント</div></div>
  </div>
  {strength_html}
  <div class="section-head"><div class="section-num">Ⅱ.</div><h2 class="section-title">全22通貨ペア シグナル評価</h2><span class="section-sub">TA × FA × Event × Sentiment × SR × TP</span></div>
  <table class="data-table">
    <thead><tr><th>シグナル</th><th>通貨ペア / 分析</th><th>価格</th><th>TA</th><th>FA</th><th>金利差</th><th>RSI</th><th>判定</th><th>ファンダ詳細</th></tr></thead>
    <tbody>{''.join(rows_html)}</tbody>
  </table>
  <div class="section-head"><div class="section-num">Ⅲ.</div><h2 class="section-title">中央銀行政策金利</h2><span class="section-sub">Central Bank Rates</span></div>
  <table class="data-table">
    <thead><tr><th>通貨</th><th>中央銀行</th><th>政策金利</th><th>スタンス</th><th>次回会合</th></tr></thead>
    <tbody>{''.join(cb_rows_html)}</tbody>
  </table>
  <div class="section-head"><div class="section-num">Ⅳ.</div><h2 class="section-title">今後7日間の重要イベント</h2><span class="section-sub">Economic Calendar</span></div>
  <div style="margin-top:1rem">
    {''.join(event_rows_html) if event_rows_html else '<div style="color:var(--text-muted);font-family:var(--mono);font-size:0.85rem;padding:1rem">今後7日間の重要イベントはありません</div>'}
  </div>
  <footer class="app-footer">
    <em>Currents</em> · FX Signal Monitor L3 Advanced · <a href="{PAGES_URL}" style="color:var(--gold)">{PAGES_URL}</a>
    <br>8 Analytics: 通貨強弱 / ボラレジーム / 段階TP / 相関リスク / キャリー / SR / 介入リスク / 週次レポート
    <br>Data: ECB Frankfurter + US Treasury + Yahoo Finance + 手動JSON / 投資判断は自己責任で
  </footer>
</div>
</body>
</html>"""


def main():
    print("=" * 64)
    print(f"FX Signal Monitor L3 Advanced - {datetime.now(timezone.utc).isoformat()}")
    print("=" * 64)

    now = datetime.now(timezone.utc)

    # 1. 最新レート
    try:
        latest = fetch_latest_rates()
    except Exception as e:
        print(f"[FATAL] Cannot fetch latest rates: {e}")
        sys.exit(1)

    # 2. 中央銀行金利（FRED APIで最新値を自動取得・失敗時は手動値）
    cb_rates = fetch_live_central_bank_rates()
    print(f"[OK] Central bank rates: {len(cb_rates)} currencies")

    # 2b. stance矛盾検知（FRED金利の変化と手動stanceの整合性チェック）
    from modules.rate_fetcher import (
        check_stance_consistency, save_rates_snapshot, generate_rates_html
    )
    rate_consistency = check_stance_consistency(cb_rates)
    generate_rates_html(cb_rates, rate_consistency)
    save_rates_snapshot(cb_rates)
    if rate_consistency.get("warnings"):
        print(f"[WARN] stance矛盾 {len(rate_consistency['warnings'])}件検知")

    # 3. 米国債利回り（カーブ形状の参考。10年は後で市場値に一本化）
    us_yields = fetch_us_treasury_yields()

    # 4. 市場センチメント
    print("[INFO] Fetching market sentiment...")
    sentiment = evaluate_market_sentiment()
    print(f"[OK] Sentiment: VIX={sentiment.get('vix')} mode={sentiment.get('risk_mode')}")

    # 4b. 10年金利を市場値(^TNX)に一本化
    #     fetch_us_treasury_yields() の "10y" は発行済み国債の平均クーポン利率
    #     (Treasury avg_interest_rates) で市場利回りではないため、表示・stateは
    #     センチメント側の市場値(^TNX)に統一し、二重表示・不整合を解消する。
    market_10y = sentiment.get("us10y")
    if market_10y is not None:
        us_yields["avg_note_coupon"] = us_yields.get("10y")  # 旧値（参考保持）
        us_yields["10y"] = market_10y
        us_yields["10y_source"] = "market yield (^TNX via Yahoo Finance)"
        if us_yields.get("2y") is not None:
            us_yields["spread_10y2y"] = round(market_10y - us_yields["2y"], 3)
    print(f"[OK] US 10Y (market): {us_yields.get('10y')}%")

    # 4.5 Obsidian Wiki
    print("\n[INFO] Fetching Obsidian Vault notes...")
    try:
        obsidian_data = fetch_obsidian()
        if obsidian_data:
            print(f"[OK] Obsidian: {len(obsidian_data.get('signal_rules',[]))} rules")
        else:
            print("[INFO] Obsidian not configured - skipping")
            obsidian_data = None
    except Exception as e:
        print(f"[WARN] Obsidian failed: {e}")
        obsidian_data = None

    # 5. 全ペア評価（価格履歴も保存して通貨強弱計算に使う）
    print(f"\n[INFO] Evaluating {len(PAIR_API)} pairs (除外: {sorted(PAIR_EXCLUDE)})...")

    # ⑩ 自己学習: 過去の決済実績からペア別信頼度マップを構築
    from modules.trade_tracker import load_closed_trades
    closed_trades_all = load_closed_trades(days_back=90)
    perf_map = build_pair_performance_map(closed_trades_all, min_trades=5)
    # 2026-06-16: 静的ベースライン（バックテスト実証値）をマージ
    perf_map = apply_static_baseline(perf_map)
    if perf_map:
        adjusted = [p for p, v in perf_map.items() if v.get("adjustment", 0) != 0]
        if adjusted:
            print(f"[OK] ペア信頼度調整: {len(adjusted)}ペア → {adjusted}")

    results = []
    all_pair_prices = {}   # ① 通貨強弱メーター用
    all_histories = {}     # ⑨ バックテスト用（フル履歴）

    for pair in PAIR_API:
        # 2026-06-16/19: 構造的不振ペアを除外
        # INRJPY/TRYJPY: 流動性・政治リスク
        # EURUSD/USDCHF: 慢性不振（実証40%/37.5%）
        # NZDJPY/CADJPY: BOJ局面ダブル逆風（実証33%/0%）2026-06-19追加
        if pair in PAIR_EXCLUDE:
            print(f"[SKIP] {pair}: 除外リスト")
            continue

        price = latest["pairs"].get(pair)
        if not price:
            print(f"[SKIP] {pair}: no price")
            continue
        prices = fetch_history(pair, 280)
        if not prices or len(prices) < 30:
            print(f"[SKIP] {pair}: insufficient history")
            continue
        prices.append(price)

        # 通貨強弱用に30日分を保存
        all_pair_prices[pair] = {"current": price, "prices_30d": prices[-30:]}
        # バックテスト用にフル履歴を保存
        all_histories[pair] = prices

        try:
            r = evaluate_full(pair, price, prices, cb_rates, sentiment, now)

            # Obsidian Wikiルール適用
            if obsidian_data:
                r = apply_obsidian_intelligence(r, sentiment, obsidian_data, now)

            # 高度分析（②③⑤⑥⑧）を適用
            r = run_advanced_analytics(
                r, prices, all_pair_prices, PAIR_API, cb_rates, sentiment, results
            )

            # ⑬ 相場局面判定（トレンド/レンジ）
            r["market_regime"] = detect_market_regime(prices)

            # ⑩ 自己学習による信頼度調整
            r = apply_performance_weighting(r, perf_map)

            # 🎯 待ち伏せ型アラート（重要価格への接近を判定）
            r = evaluate_ambush(r, prices, atr_threshold=0.5)

            # 📉 BOJサイクル方向性フィルタ（2026-06-19追加）
            # JPYクロスのLONGシグナルを中銀スタンスで制限する
            # ease通貨ロング = ダブル逆風（JPY強 + 対通貨弱）→ ブロックまたは降格
            r = apply_boj_cycle_directional_filter(r, cb_rates)
            if r.get("regime_filter_applied"):
                print(f"         └─ {r.get('regime_filter_reason', '')}")

            # 📊 VIXレジームフィルタ（2026-06-22追加）
            # VIX>30(panic): JPYクロスLONG完全ブロック（キャリー崩壊リスク）
            # VIX>25(risk_off): JPYクロスLONG ★1段降格
            # VIX>20(caution): 警告のみ
            r = apply_vix_regime_filter(r, sentiment)
            if r.get("vix_filter_applied"):
                print(f"         └─ {r.get('vix_filter_reason', '')}")
            elif r.get("vix_caution"):
                print(f"         └─ {r.get('vix_caution_reason', '')}")

            # 💸 スプレッド/ATR比フィルタ（2026-06-23追加）
            # spread/ATR > 30%: ★≤2 強制（実質取引禁止・主にエキゾチック）
            # spread/ATR > 10%: ★≤3 上限
            # bid/ask スプレッドが ATR に対して大きいペアは、実効SL距離が
            # 中値ベース計算より狭くなり SL に引っかかりやすいため降格。
            r = apply_spread_filter(r)
            if r.get("spread_filter_applied"):
                print(f"         └─ {r.get('spread_filter_reason', '')}")
            elif r.get("spread_caution"):
                print(f"         └─ {r.get('spread_caution_reason', '')}")

            # 🕐 セッションフィルタ（2026-07-20追加）
            # London単独枠(08-13 UTC)は実データで決着勝率11.1%(n=9)と最弱 → ★≤2
            r = apply_session_filter(r, now)
            if r.get("session_filter_applied"):
                print(f"         └─ {r.get('session_filter_reason', '')}")
            elif r.get("session_caution"):
                print(f"         └─ {r.get('session_caution_reason', '')}")

            # 📅 季節性フィルタ（2026-07-20追加）
            # AUDJPY LONGは8月が過去20-24年で最弱の月(下落率70%) → ★≤3
            r = apply_seasonal_filter(r, now)
            if r.get("seasonal_filter_applied"):
                print(f"         └─ {r.get('seasonal_filter_reason', '')}")
            elif r.get("seasonal_caution"):
                print(f"         └─ {r.get('seasonal_caution_reason', '')}")

            # 🏦 CB会合ブラックアウト（2026-06-16追加）
            cb_blackout = check_cb_meeting_blackout(pair, cb_rates, now)
            r["cb_meeting_blackout"] = cb_blackout
            if cb_blackout["active"]:
                currency = cb_blackout.get("currency", "")
                if cb_blackout["severity"] == "blackout":
                    if currency == "JPY":
                        # 日銀会合後ブラックアウト: JPYペアは完全ブロック（★1固定）
                        # 2026-06-18: ★3以下に降格しても実際に取引されていたため強化
                        r["stars"] = 1
                        r["verdict"] = "🏦 日銀会合後ブラックアウト（新規禁止）"
                        r["direction"] = "WAIT_BOJ"
                    else:
                        # 他中銀: ★を最大2段階降格
                        r["stars"] = max(1, r.get("stars", 1) - 2)
                    r["blackout_degraded"] = True
                elif cb_blackout["severity"] == "warn":
                    # ★を1段階降格
                    r["stars"] = max(1, r.get("stars", 1) - 1)

            results.append(r)

            regime = r.get("volatility_regime", {})
            staged = r.get("staged_tp", {})
            warn = ""
            if r.get("event_warning"): warn += " EVT"
            if r.get("sentiment_notes"): warn += " SENT"
            if regime.get("regime") == "high": warn += f" ⚡HighVol"
            if r.get("blackout_degraded"): warn += " 🏦BLACKOUT"
            elif r.get("cb_meeting_blackout", {}).get("severity") == "warn": warn += " 🏦会合前"
            if r.get("intervention_risk", {}).get("risk_level") in ("HIGH","CRITICAL"):
                warn += f" 🚨介入"
            print(
                f"  [{stars_to_text(r['stars'])}] {pair:8} {fmt_price(pair, price):>12}  "
                f"TA={r['ta_score']:.0f} FA={r['fa_score']:.0f}  "
                f"{r['verdict']}{warn}"
            )
        except Exception as e:
            print(f"[ERROR] evaluate {pair}: {e}")
            import traceback; traceback.print_exc()

    if not results:
        print("[FATAL] No results")
        sys.exit(1)

    # ① 通貨強弱メーター（全ペア評価後に計算）
    print("\n[INFO] Calculating currency strength...")
    currency_strength = calc_global_analytics(all_pair_prices, PAIR_API)
    if currency_strength:
        print("[OK] Currency strength:")
        sorted_strength = sorted(currency_strength.items(), key=lambda x: -x[1]["score"])
        for ccy, info in sorted_strength:
            print(f"  {ccy}: {info['score']:+.1f} ({info['label']}) #{info['rank']}")

        # 各ペアに通貨強弱コンテキストを付与
        for r in results:
            ctx = get_pair_strength_context(r["pair"], PAIR_API, currency_strength)
            r["strength_context"] = ctx

    # 🤖 DRL 学習データ収集（2026-06-11 研究A反映）
    try:
        drl_saved = collect_scan_results(results, cb_rates=cb_rates)
        if drl_saved > 0:
            drl_stats = get_drl_stats()
            print(f"[DRL] 状態ベクトル保存: {drl_saved}件 "
                  f"(累計 {drl_stats.get('total_rows', 0):,}行 / "
                  f"{drl_stats.get('days_collected', 0)}日分 / "
                  f"{drl_stats.get('file_size_kb', 0)} KB)")
    except Exception as e:
        print(f"[WARN] DRL collect failed: {e}")

    # ④ ポートフォリオ相関リスク
    print("\n[INFO] Calculating correlation risk...")
    portfolio_risk = calc_portfolio_analytics(results, PAIR_API)
    if portfolio_risk.get("warnings"):
        print(f"[WARN] Correlation risk [{portfolio_risk['risk_level'].upper()}]:")
        for w in portfolio_risk["warnings"]:
            print(f"  {w}")
    else:
        print(f"[OK] Correlation risk: {portfolio_risk.get('risk_level','low')}")

    # 6. 差分検出
    previous_stars = load_previous_state()
    newly, upgraded, is_first = detect_changes(results, previous_stars)
    print(f"\n[INFO] Changes: {len(newly)} new strong, {len(upgraded)} upgraded "
          f"(first run: {is_first})")

    # ⑦ トレードのライフサイクル管理（通知の前に実行して情報を取得）
    # ENTRY_MODE（2026-07-20追加）:
    #   market（デフォルト、毎時cron）: 従来通り★4以上を即座に成行相当でオープン
    #   limit（1日3回07:00/13:00/21:00 JSTのcron専用）: 即座にオープンせず、
    #     押し目狙いの指値待機注文として登録する（下記参照）
    from modules.trade_tracker import load_open_trades, open_trade_from_pending_fill
    from modules.pending_orders import (
        load_pending_orders, save_pending_orders, check_pending_fills,
        create_pending_order, pending_order_to_trade,
    )
    entry_mode = os.environ.get("ENTRY_MODE", "market").strip().lower()
    trade_update = update_trades(results, now, PAIR_API, latest["pairs"], entry_mode=entry_mode)
    if trade_update["newly_opened"]:
        print(f"\n[TRADE] 新規エントリー {len(trade_update['newly_opened'])}件:")
        for t in trade_update["newly_opened"]:
            _p = t.get("pair", "")
            print(f"  + {_p} {t['direction']} @ {fmt_price(_p, t['entry_price'])} "
                  f"(SL:{fmt_price(_p, t.get('sl'))} TP1:{fmt_price(_p, t.get('tp1'))})")
            sizing = t.get("position_sizing") or {}
            if sizing:
                print(f"    💰 {sizing.get('note', '')}")
    if trade_update["newly_closed"]:
        print(f"[TRADE] 決済 {len(trade_update['newly_closed'])}件:")
        for t in trade_update["newly_closed"]:
            _p = t.get("pair", "")
            _d = pair_decimals(_p)
            pnl_note = f" 損益¥{t['pnl_jpy']:+.0f}" if "pnl_jpy" in t else ""
            print(f"  - {_p} {t['direction']} {t['result']} "
                  f"({t['exit_reason']}) {t.get('pips',0):+.{_d}f} "
                  f"保有{t.get('hold_hours',0)}h{pnl_note}")
    print(f"[TRADE] 現在保有中: {trade_update['still_open']}件")
    open_trades = load_open_trades()

    # 📌 指値待機シグナル（2026-07-20追加）
    # (a) 毎回: 保留中の指値注文が約定/失効していないかチェック（market/limit両モードで実行）
    pending_orders = load_pending_orders()
    filled_orders, remaining_orders, expired_orders = check_pending_fills(
        pending_orders, latest["pairs"], now
    )
    newly_filled_trades = []
    for pair, order in filled_orders.items():
        trade = pending_order_to_trade(order, now)
        trade = open_trade_from_pending_fill(trade, PAIR_API, latest["pairs"])
        newly_filled_trades.append(trade)
        open_trades = load_open_trades()  # サイジングに反映させるため再読込
        print(f"[PENDING] ✅約定: {pair} {order['direction']} "
              f"指値{fmt_price(pair, order.get('limit_price'))}で約定")

    # (b) limitモードのみ: 新規★4以上シグナルを指値待機として登録
    newly_created_orders = {}
    if entry_mode == "limit":
        closed_this_cycle_pairs = {t.get("pair") for t in trade_update.get("newly_closed", [])}
        for r in results:
            pair = r["pair"]
            if (r.get("stars", 0) >= 4
                    and r.get("direction", "").endswith(("LONG", "SHORT"))
                    and pair not in open_trades
                    and pair not in remaining_orders
                    and pair not in closed_this_cycle_pairs):
                order = create_pending_order(r, now)
                remaining_orders[pair] = order
                newly_created_orders[pair] = order
                print(f"[PENDING] 📌新規指値登録: {pair} {order['direction']} "
                      f"指値{fmt_price(pair, order.get('limit_price'))} "
                      f"(有効期限 {order.get('valid_until')})")

    if expired_orders:
        for pair in expired_orders:
            print(f"[PENDING] ⌛失効: {pair}（次回スキャンまでに約定せず）")

    save_pending_orders(remaining_orders)

    # ⑪ ドローダウン監視（決済後の全履歴で連敗チェック）
    closed_after = load_closed_trades(days_back=14)
    drawdown = check_drawdown_alert(closed_after, recent_n=5)
    if drawdown.get("alert"):
        print(f"[DRAWDOWN] {drawdown['message']} → {drawdown['recommendation']}")

    # ⑬ 主要ペアの相場局面（USDJPYを代表として全体表示用に）
    market_regime = None
    for r in results:
        if r["pair"] == "USDJPY":
            market_regime = r.get("market_regime")
            break

    # 🎯 待ち伏せアラート集約
    ambush_alerts = collect_ambush_alerts(results)
    if ambush_alerts["high_confidence"]:
        print(f"\n[AMBUSH] 高確度ゾーン {len(ambush_alerts['high_confidence'])}件:")
        for a in ambush_alerts["high_confidence"]:
            print(f"  ★{a['stars']} {a['pair']} {a['direction']} "
                  f"→ {a['nearest']['role']} あと{a['nearest']['distance_pct']:.2f}%")
    if ambush_alerts["approaching"]:
        print(f"[AMBUSH] POI接近中 {len(ambush_alerts['approaching'])}件")

    # ⑮ AI市況コメンタリー（ANTHROPIC_API_KEY / GEMINI_API_KEY設定時のみ）
    ai_commentary = None
    if newly or upgraded or (is_first and newly):
        ai_commentary = generate_market_commentary(
            results, sentiment, currency_strength, market_regime
        )
        if ai_commentary:
            print(f"[AI] 市況コメンタリー生成完了")

    # 🤖 AI決済アドバイス（★4以上のペアに生成し、resultsに埋め込む）
    #    position_manager.html が last_signals.json からこれを読んで表示する
    if has_ai_key():
        advice_count = 0
        for r in results:
            if r.get("stars", 0) >= 4:
                advice = generate_exit_advice(r)
                if advice:
                    r["ai_exit_advice"] = advice
                    advice_count += 1
        if advice_count:
            print(f"[AI] 決済アドバイス生成: {advice_count}件")

    # 7. 通知（中長期シグナル変化・決済・ドローダウンで送信）
    #    待ち伏せ・反発監視（短期）はデイトレ画面に集約したため中長期通知では送らない
    has_close = bool(trade_update.get("newly_closed"))
    has_state_change = bool(trade_update.get("state_changes"))
    has_rate_warn = bool(rate_consistency.get("warnings"))
    if (newly or upgraded or (is_first and newly) or has_close
            or has_state_change or drawdown.get("alert") or has_rate_warn):
        _wh = os.environ.get("DISCORD_WEBHOOK_URL", "")
        _wh = _wh.replace("discordapp.com", "discord.com")
        send_discord(
            _wh, newly, upgraded, is_first, results, sentiment,
            currency_strength=currency_strength,
            portfolio_risk=portfolio_risk,
            trade_update=trade_update,
            open_trades=open_trades,
            drawdown=drawdown,
            ai_commentary=ai_commentary,
            ambush_alerts=None,
            rate_warnings=rate_consistency.get("warnings"),
            latest_pairs=latest["pairs"],
        )
        send_email(
            os.environ.get("SMTP_HOST", "smtp.gmail.com"),
            os.environ.get("SMTP_PORT", "465"),
            os.environ.get("SMTP_USER"), os.environ.get("SMTP_PASS"),
            os.environ.get("MAIL_FROM"), os.environ.get("MAIL_TO"),
            newly, upgraded, is_first, results, sentiment, us_yields
        )
    else:
        print("[INFO] No significant changes, skipping notifications")

    # 📌 指値待機シグナルの通知（新規登録・約定・失効があれば、上のnewly/upgraded判定とは無関係に送信）
    has_pending_update = bool(newly_created_orders or newly_filled_trades or expired_orders)
    if has_pending_update:
        _wh_pending = os.environ.get("DISCORD_WEBHOOK_URL", "").replace("discordapp.com", "discord.com")
        send_discord_pending(_wh_pending, newly_created_orders, newly_filled_trades, expired_orders)
    elif entry_mode == "limit":
        # 変化が0件でも、1日3回の指値スキャンでは「稼働中で変化なし」を短く通知する。
        # 沈黙のままだとワークフロー停止と見分けがつかないため（2026-07-21対応）。
        _wh_pending = os.environ.get("DISCORD_WEBHOOK_URL", "").replace("discordapp.com", "discord.com")
        send_discord_pending(
            _wh_pending, newly_created_orders, newly_filled_trades, expired_orders,
            heartbeat={
                "open_count": trade_update.get("still_open", 0),
                "pending_count": len(remaining_orders),
            },
        )

    # 8. 状態保存
    save_current_state(
        results, sentiment, us_yields, cb_rates, obsidian_data,
        currency_strength=currency_strength,
        portfolio_risk=portfolio_risk,
    )

    # 9. HTMLレポート
    html = generate_html_report(
        results, sentiment, us_yields, cb_rates, now,
        currency_strength=currency_strength,
        portfolio_risk=portfolio_risk,
    )
    os.makedirs("docs", exist_ok=True)
    with open("docs/index.html", "w", encoding="utf-8") as f:
        f.write(html)
    print("[OK] HTML report written")

    print("\n" + "=" * 64)
    print("Done.")
    print("=" * 64)


if __name__ == "__main__":
    main()
