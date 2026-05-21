#!/usr/bin/env python3
"""
FX売買シグナル監視スキャナー（レベル3：フルファンダメンタルズ統合版）

統合される要素:
  ① テクニカル: 200/50DMA, MACD, RSI, ATR
  ② ファンダメンタル金利差: 中央銀行政策金利の動的反映
  ③ 経済指標カレンダー: 重要イベント前の取引控え
  ④ 市場センチメント: VIX, DXY, 米10年債, 金価格
  ⑤ 通知: Discord Webhook + Gmail SMTP

データソース:
  - Frankfurter API (ECB公式為替)
  - U.S. Treasury Fiscal Data API (米国債利回り)
  - Stooq (VIX, DXY, 金, 米10年債)
  - 手動メンテJSON (中央銀行金利・経済指標)
"""

import os
import sys
import json
import smtplib
import urllib.request
from datetime import datetime, timedelta, timezone
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.header import Header

# モジュール読込
from modules.rate_fetcher import (
    load_central_bank_rates, fetch_us_treasury_yields, compute_fa_score
)
from modules.event_filter import (
    check_event_proximity, upcoming_events_for
)
from modules.sentiment_monitor import (
    evaluate_market_sentiment, apply_sentiment_filter, classify_pair
)
from modules.obsidian_extractor import fetch_and_save as fetch_obsidian
from modules.custom_rules_engine import apply_obsidian_intelligence


# ============================================================================
# 設定: 22通貨ペア
# ============================================================================

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
}

API_SYMBOLS = "USD,JPY,GBP,AUD,NZD,CAD,CHF,SGD,HKD,CNY,MXN,TRY,ZAR,INR"

LATEST_ENDPOINTS = [
    ("Frankfurter v1",
     f"https://api.frankfurter.dev/v1/latest?base=EUR&symbols={API_SYMBOLS}"),
    ("Frankfurter legacy",
     f"https://api.frankfurter.app/latest?from=EUR&to={API_SYMBOLS}"),
]


# ============================================================================
# 為替データ取得
# ============================================================================

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


# ============================================================================
# テクニカル指標
# ============================================================================

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


# ============================================================================
# テクニカルスコア（既存ロジック）
# ============================================================================

def compute_ta_score(price, prices):
    dma200 = sma(prices, 200) or sma(prices, len(prices))
    dma50 = sma(prices, 50) or sma(prices, min(50, len(prices)))
    rsi_v = rsi(prices, 14)
    macd_v, macd_sig = macd_now(prices)
    atr_v = atr_calc(prices, 14)

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

    return {
        "ta_score": round((dma_score + macd_score + rsi_score) / 3, 1),
        "dma_score": dma_score,
        "macd_score": macd_score,
        "rsi_score": rsi_score,
        "dma200": round(dma200, 5) if dma200 else None,
        "dma50": round(dma50, 5) if dma50 else None,
        "rsi": round(rsi_v, 2) if rsi_v else None,
        "macd": round(macd_v, 5) if macd_v else None,
        "macd_signal": round(macd_sig, 5) if macd_sig else None,
        "atr": round(atr_v, 5) if atr_v else None,
    }


# ============================================================================
# 統合シグナル評価（レベル3: TA + FA + Event + Sentiment）
# ============================================================================

def evaluate_full(pair, price, prices, cb_rates, sentiment, now):
    """
    フル統合評価:
      Step1: テクニカル → TAスコア
      Step2: 金利差 → FAスコア（動的）
      Step3: TA+FA合議で★算出
      Step4: 経済指標近接チェック → 必要なら★抑制
      Step5: 市場センチメントフィルター適用
    """
    # Step1: テクニカル
    ta = compute_ta_score(price, prices)

    # Step2: 動的FAスコア
    fa = compute_fa_score(pair, PAIR_API, cb_rates)

    # Step3: TA+FA統合で★判定
    ta_sign = 1 if ta["ta_score"] > 50 else (-1 if ta["ta_score"] < 50 else 0)
    fa_sign = 1 if fa["direction"] == "buy" else (-1 if fa["direction"] == "sell" else 0)
    agree = ta_sign == fa_sign and ta_sign != 0
    conflict = ta_sign != fa_sign and ta_sign != 0 and fa_sign != 0

    if agree and ta["ta_score"] >= 75 and fa["score"] >= 65:
        stars = 5
        verdict = "◎ 高信頼ロング" if fa_sign > 0 else "◎ 高信頼ショート"
        direction = "LONG" if fa_sign > 0 else "SHORT"
    elif agree and ta["ta_score"] >= 60 and fa["score"] >= 55:
        stars = 4
        verdict = "○ ロング条件成立" if fa_sign > 0 else "○ ショート条件成立"
        direction = "LONG" if fa_sign > 0 else "SHORT"
    elif agree and ta["ta_score"] <= 25 and fa["score"] <= 35:
        stars = 5
        verdict = "◎ 高信頼ショート"
        direction = "SHORT"
    elif agree and ta["ta_score"] <= 40 and fa["score"] <= 45:
        stars = 4
        verdict = "○ ショート条件成立"
        direction = "SHORT"
    elif conflict:
        stars = 1
        verdict = "⚠ 見送り（FA/TA不一致）"
        direction = "NO_TRADE"
    elif fa_sign == 0 and ta_sign == 0:
        stars = 2; verdict = "— レンジ"; direction = "NO_TRADE"
    else:
        stars = 2; verdict = "△ 弱シグナル"
        direction = "LIGHT_" + ("LONG" if (ta_sign + fa_sign) > 0 else "SHORT")

    result = {
        "pair": pair,
        "label": PAIR_LABEL[pair],
        "price": round(price, 5),
        **ta,
        "fa_score": fa["score"],
        "fa_direction": fa["direction"],
        "fa_rate_diff": fa["rate_diff"],
        "fa_detail": fa["detail"],
        "stars": stars,
        "verdict": verdict,
        "direction": direction,
        "pair_type": classify_pair(pair),
    }

    # Step4: 経済指標近接チェック
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

    # Step5: 市場センチメントフィルター
    result = apply_sentiment_filter(pair, result, sentiment)

    # 今後7日のイベント情報を添付
    result["upcoming_events"] = upcoming_events_for(pair, hours_ahead=168)

    return result


# ============================================================================
# 状態管理
# ============================================================================

STATE_FILE = "docs/last_signals.json"


def load_previous_state():
    if not os.path.exists(STATE_FILE): return {}
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f).get("signals", {})
    except Exception:
        return {}


def save_current_state(results, sentiment, us_yields, cb_rates, obsidian_data=None):
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
            "last_fetched": obsidian_data.get("fetched_at"),
        }
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


# ============================================================================
# 通知
# ============================================================================

def stars_to_text(n):
    return "★" * n + "☆" * (5 - n)


def format_event_block(r):
    """イベント警告のブロック生成"""
    if "event_warning" not in r:
        return ""
    ev = r.get("event_info", {})
    return (
        f"  🛑 取引控え: {r['event_warning']}\n"
        f"  📅 {ev.get('name', 'N/A')} | "
        f"重要度: {ev.get('importance', 'N/A')}\n"
    )


def format_sentiment_block(r):
    """センチメント警告のブロック生成"""
    notes = r.get("sentiment_notes", [])
    if not notes:
        return ""
    return "  🌐 センチメント: " + " / ".join(notes) + "\n"


def format_signal_block_text(r):
    txt = (
        f"{stars_to_text(r['stars'])}  {r['label']}  {r['price']}\n"
        f"  {r['verdict']} / {r['direction']}\n"
        f"  TA={r['ta_score']}/100  FA={r['fa_score']}/100 "
        f"(金利差{r.get('fa_rate_diff', 'N/A'):+.2f}%)\n"
        f"  RSI={r['rsi']}  MACD={r['macd']}  ATR={r['atr']}\n"
        f"  📊 {r['fa_detail']}\n"
    )
    txt += format_event_block(r)
    txt += format_sentiment_block(r)

    # Obsidian カスタムルール適用結果
    if r.get("obsidian_rules_applied"):
        txt += "  📚 Wiki規則適用:\n"
        for rule in r["obsidian_rules_applied"]:
            txt += (f"    ・{rule.get('rule_name', 'unnamed')} "
                    f"[{rule.get('action', '?')}] (source: {rule.get('filename', '?')})\n")

    # 関連する過去レッスン（教訓）
    if r.get("relevant_lessons"):
        txt += "  📖 関連する過去の教訓:\n"
        for les in r["relevant_lessons"][:3]:
            txt += f"    ・{les.get('title', 'untitled')}\n"

    # 関連する Wiki 分析記事
    if r.get("wiki_analyses"):
        txt += "  🔍 参照すべきWiki分析:\n"
        for ana in r["wiki_analyses"][:2]:
            txt += f"    ・{ana.get('title', 'untitled')}\n"

    # 過去30日間の取引日記統計
    if r.get("journal_stats_30d"):
        js = r["journal_stats_30d"]
        txt += (f"  📔 過去30日のこのペア取引: {js.get('count', 0)}回 "
                f"(勝率: {js.get('win_rate', 'N/A')})\n")

    upc = r.get("upcoming_events", [])
    if upc:
        txt += "  📅 今後7日: "
        for e in upc[:3]:
            txt += f"{e['name']}({e['hours_until']:.0f}h) "
        txt += "\n"
    return txt


def format_sentiment_summary(s):
    """市場センチメントのサマリー"""
    if not s:
        return "  センチメント取得失敗\n"
    return (
        f"  VIX: {s.get('vix', 'N/A')} [{s.get('vix_level', '?')}]\n"
        f"  DXY: {s.get('dxy', 'N/A')} [{s.get('dxy_trend', '?')}]\n"
        f"  米10y: {s.get('us10y', 'N/A')}% [{s.get('bond_pressure', '?')}]\n"
        f"  ゴールド: {s.get('gold', 'N/A')} [{s.get('gold_trend', '?')}]\n"
        f"  ▶ リスクモード: {s.get('risk_mode', 'N/A').upper()}\n"
    )


def send_discord(webhook_url, newly, upgraded, is_first, all_results, sentiment):
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
        desc = f"監視開始。現在の★4以上: **{len(newly)}件** / 市場: **{risk_mode}**"
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
        return False

    embeds = [{
        "title": title, "description": desc, "color": color,
        "timestamp": jst.isoformat(),
        "footer": {"text": f"Currents FX Terminal L3 | {timestamp}"},
        "fields": []
    }]

    # 市場センチメント概要
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

    for r in newly:
        rate_diff_str = (
            f"{r.get('fa_rate_diff', 0):+.2f}%"
            if r.get("fa_rate_diff") is not None else "N/A"
        )
        value = (
            f"```\n価格: {r['price']}\n"
            f"方向: {r['direction']}\n"
            f"TA: {r['ta_score']}/100  FA: {r['fa_score']}/100\n"
            f"金利差: {rate_diff_str}\n```\n"
            f"📊 {r['fa_detail']}"
        )
        if r.get("event_warning"):
            value += f"\n⚠ {r['event_warning']}"
        if r.get("sentiment_notes"):
            value += f"\n🌐 " + " / ".join(r["sentiment_notes"])
        # Obsidian カスタムルール適用結果
        if r.get("obsidian_rules_applied"):
            rule_names = [
                f"・{rule.get('rule_name', 'unnamed')}"
                for rule in r["obsidian_rules_applied"][:3]
            ]
            value += f"\n📚 Wiki規則:\n" + "\n".join(rule_names)
        # 関連する過去レッスン（教訓）
        if r.get("relevant_lessons"):
            lesson_titles = [
                f"・{les.get('title', 'untitled')}"
                for les in r["relevant_lessons"][:2]
            ]
            value += f"\n📖 関連教訓:\n" + "\n".join(lesson_titles)

        embeds[0]["fields"].append({
            "name": f"{stars_to_text(r['stars'])} {r['label']} - {r['verdict']}",
            "value": value, "inline": False
        })

    for r in upgraded:
        embeds[0]["fields"].append({
            "name": f"⬆ 昇格: {r['label']} ★4 → ★5",
            "value": f"```\n価格: {r['price']}  方向: {r['direction']}\n```",
            "inline": False
        })

    try:
        req = urllib.request.Request(
            webhook_url,
            data=json.dumps({"embeds": embeds}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST"
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            print(f"[OK] Discord sent (HTTP {resp.status})")
            return True
    except Exception as e:
        print(f"[ERROR] Discord send failed: {e}")
        return False


def send_email(smtp_host, smtp_port, smtp_user, smtp_pass,
               from_addr, to_addr, newly, upgraded, is_first,
               all_results, sentiment, us_yields):
    if not all([smtp_host, smtp_user, smtp_pass, from_addr, to_addr]):
        print("[INFO] Email not configured")
        return False

    jst = datetime.now(timezone.utc) + timedelta(hours=9)
    timestamp = jst.strftime("%Y-%m-%d %H:%M JST")
    risk_mode = sentiment.get("risk_mode", "?") if sentiment else "?"

    if is_first:
        subject = f"[FX Monitor L3] 監視開始 - ★4以上{len(newly)}件 / 市場:{risk_mode}"
    else:
        subject = (
            f"[FX Monitor L3] 新規{len(newly)} / 昇格{len(upgraded)} "
            f"/ 市場:{risk_mode} ({timestamp})"
        )

    lines = [
        "━" * 64,
        f"  FX売買シグナル通知（テクノ・ファンダメンタル統合）  {timestamp}",
        "━" * 64, "",
        "【🌐 市場センチメント】",
        format_sentiment_summary(sentiment),
    ]

    if us_yields:
        lines.append("【💵 米国債利回り】")
        lines.append(
            f"  2y: {us_yields.get('2y', 'N/A')}%  "
            f"10y: {us_yields.get('10y', 'N/A')}%  "
            f"30y: {us_yields.get('30y', 'N/A')}%  "
            f"10y-2y: {us_yields.get('spread_10y2y', 'N/A')}%"
        )
        lines.append("")

    if newly:
        lines.append("【★4以上の新規シグナル】")
        lines.append("")
        for r in newly:
            lines.append(format_signal_block_text(r))

    if upgraded:
        lines.append("【★4 → ★5 への昇格】")
        lines.append("")
        for r in upgraded:
            lines.append(format_signal_block_text(r))

    lines.extend([
        "━" * 64,
        "【全22ペアの状態】",
        "━" * 64, "",
    ])
    for r in sorted(all_results, key=lambda x: -x["stars"]):
        fa_diff = r.get("fa_rate_diff")
        diff_str = f"{fa_diff:+.2f}%" if fa_diff is not None else "  N/A "
        lines.append(
            f"  {stars_to_text(r['stars'])} {r['label']:<10} "
            f"{r['price']:>10}  金利差{diff_str}  {r['verdict']}"
        )

    lines.extend([
        "",
        "━" * 64,
        "  Currents FX Signal Monitor L3 / Techno-Fundamental Strategy",
        "  Data: ECB(Frankfurter)+米財務省+Stooq+手動JSON / 完全自動分析",
        "  本通知は教育・研究目的。投資判断は自己責任で行ってください。",
        "━" * 64,
    ])
    body = "\n".join(lines)

    msg = MIMEMultipart("alternative")
    msg["Subject"] = Header(subject, "utf-8")
    msg["From"] = from_addr
    msg["To"] = to_addr
    msg.attach(MIMEText(body, "plain", "utf-8"))

    try:
        with smtplib.SMTP_SSL(smtp_host, int(smtp_port or 465), timeout=20) as s:
            s.login(smtp_user, smtp_pass)
            s.send_message(msg)
        print(f"[OK] Email sent to {to_addr}")
        return True
    except Exception as e:
        print(f"[ERROR] Email send failed: {e}")
        return False


# ============================================================================
# HTMLレポート（ターミナル風リデザイン版）
# ============================================================================

def generate_html_report(results, sentiment, us_yields, cb_rates, generated_at):
    """HTMLターミナルと統一されたデザインの本格ダッシュボードを生成"""
    jst = generated_at + timedelta(hours=9)
    sorted_results = sorted(results, key=lambda x: (-x["stars"], -x["ta_score"]))

    # 市場モード
    risk_mode = sentiment.get("risk_mode", "unknown") if sentiment else "unknown"
    risk_emoji_map = {
        "panic": "🚨🚨🚨", "risk_off": "⚠", "caution": "⚠",
        "normal": "🟢", "complacent": "🟡", "unknown": "❓"
    }
    risk_label_map = {
        "panic": "PANIC モード（新興国通貨取引停止）",
        "risk_off": "RISK-OFF モード（リスクオフ警戒）",
        "caution": "CAUTION モード（注意）",
        "normal": "NORMAL モード（平常）",
        "complacent": "COMPLACENT モード（過信警戒）",
        "unknown": "UNKNOWN（取得失敗）"
    }
    risk_emoji = risk_emoji_map.get(risk_mode, "")
    risk_label = risk_label_map.get(risk_mode, risk_mode)

    # サマリー統計
    strong_count = sum(1 for r in results if r["stars"] >= 4)
    blocked_count = sum(1 for r in results if r.get("event_warning") or r.get("sentiment_notes"))
    long_count = sum(1 for r in results if r["direction"].endswith("LONG") and r["stars"] >= 4)
    short_count = sum(1 for r in results if r["direction"].endswith("SHORT") and r["stars"] >= 4)

    # 各シグナル行
    rows = []
    for r in sorted_results:
        if r["direction"].endswith("LONG"):
            row_cls = "buy"
        elif r["direction"].endswith("SHORT"):
            row_cls = "sell"
        elif r["direction"] in ("WAIT_EVENT", "BLOCKED_PANIC"):
            row_cls = "blocked"
        else:
            row_cls = "neutral"

        fa_diff = r.get("fa_rate_diff")
        diff_str = f"{fa_diff:+.2f}%" if fa_diff is not None else "—"

        warn_badges = []
        if r.get("event_warning"):
            warn_badges.append('<span class="badge badge-event">⏸ EVT</span>')
        if r.get("sentiment_notes"):
            warn_badges.append('<span class="badge badge-sent">🌐 SENT</span>')
        warn_html = " ".join(warn_badges)

        # イベント情報
        event_html = ""
        if r.get("event_warning"):
            event_html = f'<div class="warn-line">⏸ {r["event_warning"]}</div>'
        if r.get("sentiment_notes"):
            event_html += '<div class="warn-line">🌐 ' + " / ".join(r["sentiment_notes"]) + '</div>'

        rows.append(f"""
        <tr class="row-{row_cls}">
          <td class="stars-cell">{stars_to_text(r['stars'])}</td>
          <td class="pair-cell">
            <div class="pair-main">{r['label']} {warn_html}</div>
            {event_html}
          </td>
          <td class="num-cell">{r['price']}</td>
          <td class="num-cell score-ta">{r['ta_score']}</td>
          <td class="num-cell score-fa">{r['fa_score']}</td>
          <td class="num-cell">{diff_str}</td>
          <td class="num-cell">{r['rsi']}</td>
          <td class="verdict-cell">{r['verdict']}</td>
          <td class="fa-detail">{r['fa_detail']}</td>
        </tr>
        """)

    # 中央銀行金利テーブル
    stance_map = {
        "tighten": ("↑", "#4ade80", "引締"),
        "neutral": ("→", "#94a3b8", "中立"),
        "ease": ("↓", "#f87171", "緩和")
    }
    cb_rows = []
    for ccy, info in (cb_rates or {}).items():
        icon, color, label = stance_map.get(info.get("stance", "neutral"), ("?", "#94a3b8", "?"))
        cb_rows.append(f"""
        <tr>
          <td class="ccy-cell">{ccy}</td>
          <td>{info.get('cb_name', '—')}</td>
          <td class="num-cell">{info.get('rate', '—')}%</td>
          <td style="color: {color}; font-family: var(--mono);">{icon} {label}</td>
          <td class="meta-cell">{info.get('next_meeting', '—')}</td>
        </tr>
        """)

    # 近接イベント抽出
    upcoming_events = {}
    for r in results:
        for ev in (r.get("upcoming_events") or []):
            key = ev["date"] + "|" + ev["name"]
            if key not in upcoming_events or upcoming_events[key]["hours_until"] > ev["hours_until"]:
                upcoming_events[key] = ev
    event_list = sorted(upcoming_events.values(), key=lambda e: e["hours_until"])

    event_rows = []
    for ev in event_list[:20]:
        dt = datetime.fromisoformat(ev["date"].replace("Z", "+00:00"))
        jst_event = dt + timedelta(hours=9)
        time_str = jst_event.strftime("%m/%d %H:%M")
        imp = ev["importance"]
        event_rows.append(f"""
        <div class="event-row event-{imp}">
          <div class="event-time">{time_str}<small>+{ev['hours_until']:.1f}h</small></div>
          <div class="event-ccy">{ev['currency']}</div>
          <div class="event-name">{ev['name']}</div>
          <div class="event-imp imp-{imp}">{imp}</div>
        </div>
        """)

    return f"""<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<meta http-equiv="refresh" content="3600">
<title>FX Signal Monitor L3 · Dashboard</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Cormorant+Garamond:ital,wght@1,500;1,600&family=JetBrains+Mono:wght@400;500;700&family=Shippori+Mincho:wght@500;600;700&family=Zen+Kaku+Gothic+New:wght@400;500;700&display=swap" rel="stylesheet">
<style>
:root {{
  --bg-deep: #0a0e1a; --bg-surface: #111726; --bg-card: #141b2d;
  --bg-elev: #1a2235; --border: #2a3450; --border-soft: #1f273d;
  --text-primary: #e8edf7; --text-secondary: #9aa5b8; --text-muted: #5a6378;
  --gold: #d4a574; --amber: #e6b85c;
  --buy: #4ade80; --buy-bg: rgba(74,222,128,0.08);
  --sell: #f87171; --sell-bg: rgba(248,113,113,0.08);
  --caution: #fbbf24; --caution-bg: rgba(251,191,36,0.08);
  --neutral: #94a3b8;
  --display: 'Cormorant Garamond', 'Shippori Mincho', serif;
  --jp: 'Zen Kaku Gothic New', sans-serif;
  --mono: 'JetBrains Mono', monospace;
  --jp-serif: 'Shippori Mincho', serif;
}}
* {{ margin:0; padding:0; box-sizing:border-box; }}
body {{
  background: var(--bg-deep); color: var(--text-primary);
  font-family: var(--jp); line-height: 1.6;
  background-image:
    radial-gradient(circle at 15% 10%, rgba(212,165,116,0.04) 0%, transparent 40%),
    radial-gradient(circle at 85% 80%, rgba(74,222,128,0.03) 0%, transparent 40%);
  min-height: 100vh;
}}
.container {{ max-width: 1400px; margin: 0 auto; padding: 2rem; }}

/* Header */
.app-header {{
  display: flex; justify-content: space-between; align-items: baseline;
  padding-bottom: 1rem; border-bottom: 1px solid var(--border); margin-bottom: 2rem;
  flex-wrap: wrap; gap: 1rem;
}}
.brand {{ display: flex; align-items: baseline; gap: 1rem; }}
.brand-mark {{
  font-family: var(--display); font-size: 2rem; font-weight: 600;
  font-style: italic; color: var(--gold); letter-spacing: -0.02em;
}}
.brand-sub {{
  font-family: var(--jp-serif); font-size: 0.78rem;
  color: var(--text-secondary); letter-spacing: 0.15em;
}}
.header-meta {{ font-family: var(--mono); font-size: 0.78rem; color: var(--text-secondary); }}
.header-meta .live {{
  display: inline-block; background: var(--buy-bg); color: var(--buy);
  padding: 0.2rem 0.6rem; border-radius: 3px; border: 1px solid rgba(74,222,128,0.3);
  font-size: 0.7rem; font-weight: 700; letter-spacing: 0.08em; margin-right: 0.5rem;
}}

/* Top Navigation Bar */
.top-nav {{
  display: flex; justify-content: space-between; align-items: center;
  padding: 0.7rem 1.2rem; margin-bottom: 1.5rem;
  background: var(--bg-card); border: 1px solid var(--border);
  border-radius: 4px; flex-wrap: wrap; gap: 0.5rem;
}}
.top-nav-left {{
  font-family: var(--mono); font-size: 0.72rem;
  color: var(--text-muted); letter-spacing: 0.08em; text-transform: uppercase;
}}
.top-nav-links {{ display: flex; gap: 0.5rem; flex-wrap: wrap; }}
.nav-link {{
  display: inline-flex; align-items: center; gap: 0.4rem;
  padding: 0.45rem 0.9rem; border-radius: 3px;
  font-family: var(--mono); font-size: 0.78rem;
  text-decoration: none; transition: all 0.15s;
  border: 1px solid var(--border);
  color: var(--text-secondary); background: var(--bg-elev);
}}
.nav-link:hover {{
  background: var(--bg-surface); color: var(--gold);
  border-color: var(--gold);
}}
.nav-link.active {{
  background: var(--buy-bg); color: var(--buy);
  border-color: rgba(74,222,128,0.4);
}}
.nav-link .nav-icon {{ font-size: 0.95rem; }}

/* Risk Banner */
.risk-banner {{
  background: linear-gradient(135deg, var(--bg-card) 0%, var(--bg-elev) 100%);
  border: 1px solid var(--border); border-left: 4px solid var(--gold);
  padding: 1.3rem 1.8rem; margin-bottom: 1.5rem; border-radius: 4px;
  position: relative; overflow: hidden;
}}
.risk-banner::before {{
  content: ''; position: absolute; top: -50%; right: -10%;
  width: 300px; height: 300px;
  background: radial-gradient(circle, rgba(212,165,116,0.08), transparent 70%);
  pointer-events: none;
}}
.risk-banner.panic {{ border-left-color: var(--sell); }}
.risk-banner.risk_off, .risk-banner.caution {{ border-left-color: var(--caution); }}
.risk-banner.normal {{ border-left-color: var(--buy); }}
.risk-banner.complacent {{ border-left-color: var(--neutral); }}

.risk-title {{
  font-family: var(--jp-serif); font-size: 1.15rem; color: var(--gold);
  letter-spacing: 0.04em; margin-bottom: 0.4rem; position: relative; z-index: 1;
}}
.risk-banner.panic .risk-title {{ color: var(--sell); }}
.risk-banner.risk_off .risk-title, .risk-banner.caution .risk-title {{ color: var(--caution); }}
.risk-banner.normal .risk-title {{ color: var(--buy); }}

.risk-summary {{
  font-family: var(--mono); font-size: 0.85rem; color: var(--text-secondary);
  position: relative; z-index: 1;
}}

/* Stats grid */
.stats-grid {{
  display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
  gap: 1rem; margin-bottom: 1.5rem;
}}
.stat-card {{
  background: var(--bg-card); border: 1px solid var(--border);
  border-radius: 4px; padding: 1.1rem;
}}
.stat-card .stat-label {{
  font-family: var(--mono); font-size: 0.68rem; color: var(--text-muted);
  letter-spacing: 0.1em; text-transform: uppercase; margin-bottom: 0.3rem;
}}
.stat-card .stat-val {{
  font-family: var(--mono); font-size: 1.6rem; font-weight: 600;
  color: var(--text-primary); line-height: 1;
}}
.stat-card .stat-sub {{
  font-size: 0.75rem; color: var(--text-secondary); margin-top: 0.3rem;
  font-family: var(--mono);
}}
.stat-val.buy {{ color: var(--buy); }}
.stat-val.sell {{ color: var(--sell); }}
.stat-val.caution {{ color: var(--caution); }}

/* Section heads */
.section-head {{
  display: flex; align-items: baseline; gap: 1rem;
  margin: 2rem 0 1rem; padding-bottom: 0.6rem;
  border-bottom: 1px solid var(--border-soft);
}}
.section-num {{
  font-family: var(--display); font-style: italic;
  font-size: 1.8rem; color: var(--gold); font-weight: 500; line-height: 1;
}}
.section-title {{
  font-family: var(--jp-serif); font-size: 1.15rem; font-weight: 600;
  letter-spacing: 0.03em;
}}
.section-sub {{
  font-family: var(--mono); font-size: 0.7rem; color: var(--text-muted);
  letter-spacing: 0.15em; text-transform: uppercase; margin-left: auto;
}}

/* Tables */
.data-table {{
  width: 100%; border-collapse: collapse;
  background: var(--bg-card); border: 1px solid var(--border);
  border-radius: 4px; overflow: hidden;
}}
.data-table th {{
  background: var(--bg-elev); color: var(--gold);
  font-family: var(--jp-serif); font-weight: 600;
  padding: 0.9rem 1rem; text-align: left;
  font-size: 0.78rem; letter-spacing: 0.05em;
  border-bottom: 1px solid var(--border);
}}
.data-table td {{
  padding: 0.75rem 1rem; border-bottom: 1px solid var(--border-soft);
  font-size: 0.82rem;
}}
.data-table tr:last-child td {{ border-bottom: none; }}
.data-table tr:hover td {{ background: var(--bg-surface); }}
.stars-cell {{ color: var(--gold); letter-spacing: 0.08em; font-family: var(--mono); white-space: nowrap; }}
.pair-cell {{ font-family: var(--jp); }}
.pair-main {{ font-family: var(--display); font-style: italic; font-size: 1.05rem; font-weight: 600; }}
.num-cell {{ font-family: var(--mono); color: var(--text-secondary); }}
.score-ta {{ color: var(--text-primary); }}
.score-fa {{ color: var(--gold); }}
.row-buy .verdict-cell {{ color: var(--buy); }}
.row-sell .verdict-cell {{ color: var(--sell); }}
.row-neutral .verdict-cell {{ color: var(--caution); }}
.row-blocked .verdict-cell {{ color: var(--neutral); }}
.row-blocked {{ opacity: 0.7; }}
.verdict-cell {{ font-weight: 500; }}
.fa-detail {{ color: var(--text-secondary); font-size: 0.76rem; font-family: var(--jp); }}
.ccy-cell {{ font-family: var(--mono); font-weight: 600; color: var(--gold); }}
.meta-cell {{ font-family: var(--mono); font-size: 0.78rem; color: var(--text-secondary); }}

/* Badges */
.badge {{
  display: inline-block; padding: 0.15rem 0.45rem; border-radius: 3px;
  font-family: var(--mono); font-size: 0.65rem; letter-spacing: 0.08em;
  font-weight: 700; margin-left: 0.3rem;
}}
.badge-event {{ background: rgba(251,191,36,0.15); color: var(--caution); border: 1px solid rgba(251,191,36,0.3); }}
.badge-sent {{ background: rgba(212,165,116,0.15); color: var(--gold); border: 1px solid rgba(212,165,116,0.3); }}
.warn-line {{
  font-size: 0.72rem; color: var(--caution); margin-top: 0.25rem;
  font-family: var(--mono);
}}

/* Event rows */
.event-row {{
  display: grid; grid-template-columns: 100px 60px 1fr 90px;
  gap: 1rem; align-items: center;
  padding: 0.7rem 1rem; background: var(--bg-card);
  border: 1px solid var(--border-soft); border-left: 3px solid var(--gold);
  margin-bottom: 0.4rem; border-radius: 3px;
  font-family: var(--jp); font-size: 0.85rem;
}}
.event-row.event-critical {{ border-left-color: var(--sell); }}
.event-row.event-high {{ border-left-color: var(--caution); }}
.event-row.event-medium {{ border-left-color: var(--text-muted); }}
.event-time {{ font-family: var(--mono); font-size: 0.85rem; color: var(--gold); font-weight: 600; }}
.event-time small {{ display: block; font-size: 0.7rem; color: var(--text-muted); }}
.event-ccy {{ font-family: var(--mono); font-weight: 600; }}
.event-imp {{
  font-family: var(--mono); font-size: 0.7rem; letter-spacing: 0.1em;
  text-transform: uppercase; text-align: right; font-weight: 700;
}}
.imp-critical {{ color: var(--sell); }}
.imp-high {{ color: var(--caution); }}
.imp-medium {{ color: var(--text-muted); }}

/* Footer */
.app-footer {{
  margin-top: 2rem; padding-top: 1rem;
  border-top: 1px solid var(--border);
  color: var(--text-muted); font-size: 0.74rem;
  font-family: var(--mono); text-align: center;
}}
.app-footer em {{
  font-family: var(--display); font-style: italic; color: var(--gold);
}}

@media (max-width: 768px) {{
  .container {{ padding: 1rem; }}
  .event-row {{ grid-template-columns: 1fr; gap: 0.3rem; }}
}}
</style>
</head>
<body>

<div class="container">

  <!-- Top Navigation Bar -->
  <nav class="top-nav">
    <div class="top-nav-left">◇ Currents FX Suite</div>
    <div class="top-nav-links">
      <a href="./" class="nav-link active">
        <span class="nav-icon">📊</span>
        <span>L3 ダッシュボード</span>
      </a>
      <a href="./terminal.html" class="nav-link">
        <span class="nav-icon">🖥</span>
        <span>分析ターミナル</span>
      </a>
      <a href="./last_signals.json" class="nav-link" target="_blank" rel="noopener">
        <span class="nav-icon">{{}}</span>
        <span>Raw JSON</span>
      </a>
    </div>
  </nav>

  <header class="app-header">
    <div class="brand">
      <span class="brand-mark">Currents</span>
      <span class="brand-sub">FX SIGNAL MONITOR · L3 DASHBOARD</span>
    </div>
    <div class="header-meta">
      <span class="live">● LIVE</span>
      Updated: {jst.strftime('%Y-%m-%d %H:%M JST')} · Auto-refresh: 1h · 22 pairs
    </div>
  </header>

  <!-- Risk Banner -->
  <div class="risk-banner {risk_mode}">
    <div class="risk-title">{risk_emoji} 市場モード: {risk_label}</div>
    <div class="risk-summary">
      VIX: {sentiment.get('vix', '—') if sentiment else '—'} ({sentiment.get('vix_level', '?') if sentiment else '?'}) &nbsp;·&nbsp;
      DXY: {sentiment.get('dxy', '—') if sentiment else '—'} ({sentiment.get('dxy_trend', '?') if sentiment else '?'}) &nbsp;·&nbsp;
      米10年債: {sentiment.get('us10y', '—') if sentiment else '—'}% &nbsp;·&nbsp;
      ゴールド: {sentiment.get('gold', '—') if sentiment else '—'} ({sentiment.get('gold_trend', '?') if sentiment else '?'})
    </div>
  </div>

  <!-- Stats Grid -->
  <div class="stats-grid">
    <div class="stat-card">
      <div class="stat-label">★4以上シグナル</div>
      <div class="stat-val {('buy' if strong_count > 0 else '')}">{strong_count}</div>
      <div class="stat-sub">/ 全{len(results)}ペア</div>
    </div>
    <div class="stat-card">
      <div class="stat-label">ロング推奨</div>
      <div class="stat-val buy">{long_count}</div>
      <div class="stat-sub">高信頼ロング条件成立</div>
    </div>
    <div class="stat-card">
      <div class="stat-label">ショート推奨</div>
      <div class="stat-val sell">{short_count}</div>
      <div class="stat-sub">高信頼ショート条件成立</div>
    </div>
    <div class="stat-card">
      <div class="stat-label">取引控え推奨</div>
      <div class="stat-val caution">{blocked_count}</div>
      <div class="stat-sub">イベント・センチメント警告</div>
    </div>
  </div>

  <!-- Signal Table -->
  <div class="section-head">
    <div class="section-num">I.</div>
    <h2 class="section-title">全22通貨ペアのシグナル評価</h2>
    <span class="section-sub">TA × FA × Event × Sentiment</span>
  </div>

  <table class="data-table">
    <thead>
      <tr>
        <th>シグナル</th><th>通貨ペア</th><th>価格</th>
        <th>TA</th><th>FA</th><th>金利差</th>
        <th>RSI</th><th>判定</th><th>マクロ分析</th>
      </tr>
    </thead>
    <tbody>{''.join(rows)}</tbody>
  </table>

  <!-- Central Bank Rates -->
  <div class="section-head">
    <div class="section-num">II.</div>
    <h2 class="section-title">中央銀行政策金利マップ</h2>
    <span class="section-sub">Central Bank Rates</span>
  </div>

  <table class="data-table">
    <thead>
      <tr>
        <th>通貨</th><th>中央銀行</th><th>政策金利</th>
        <th>スタンス</th><th>次回会合</th>
      </tr>
    </thead>
    <tbody>{''.join(cb_rows)}</tbody>
  </table>

  <!-- Upcoming Events -->
  <div class="section-head">
    <div class="section-num">III.</div>
    <h2 class="section-title">今後7日間の重要マクロイベント</h2>
    <span class="section-sub">Economic Calendar</span>
  </div>

  <div style="margin-top: 1rem;">
    {''.join(event_rows) if event_rows else '<div style="color: var(--text-muted); font-family: var(--mono); font-size: 0.85rem; padding: 1rem;">今後7日間の重要イベントはありません</div>'}
  </div>

  <footer class="app-footer">
    <em>Currents</em> · FX Signal Monitor L3 · Techno-Fundamental Strategy
    <br>Generated by GitHub Actions / Data: ECB + 米財務省 + Stooq + 手動JSON / 投資判断は自己責任で
  </footer>

</div>

</body>
</html>"""


# ============================================================================
# メインフロー
# ============================================================================

def main():
    print("=" * 64)
    print(f"FX Signal Monitor L3 - {datetime.now(timezone.utc).isoformat()}")
    print("=" * 64)

    now = datetime.now(timezone.utc)

    # 1. 為替最新値
    try:
        latest = fetch_latest_rates()
    except Exception as e:
        print(f"[FATAL] Cannot fetch latest rates: {e}")
        sys.exit(1)

    # 2. 中央銀行金利の読込
    cb_rates = load_central_bank_rates()
    print(f"[OK] Central bank rates loaded: {len(cb_rates)} currencies")

    # 3. 米国債利回り
    us_yields = fetch_us_treasury_yields()
    print(f"[OK] US yields: 10y={us_yields.get('10y')}%")

    # 4. 市場センチメント
    print("[INFO] Fetching market sentiment...")
    sentiment = evaluate_market_sentiment()
    print(f"[OK] Sentiment: VIX={sentiment.get('vix')} mode={sentiment.get('risk_mode')}")

    # 4.5. Obsidian Wiki ノート取得（オプション・失敗してもスキャンは継続）
    print("\n[INFO] Fetching Obsidian Vault notes...")
    try:
        obsidian_data = fetch_obsidian()
        if obsidian_data:
            sig_count = len(obsidian_data.get("signal_rules", []))
            ana_count = len(obsidian_data.get("analyses", []))
            jrn_count = len(obsidian_data.get("journals", []))
            les_count = len(obsidian_data.get("lessons", []))
            print(f"[OK] Obsidian: {sig_count} rules / {ana_count} analyses / "
                  f"{jrn_count} journals / {les_count} lessons")
        else:
            print("[INFO] Obsidian Vault not configured (OBSIDIAN_VAULT_PAT missing) - skipping")
            obsidian_data = None
    except Exception as e:
        print(f"[WARN] Obsidian fetch failed (continuing without it): {e}")
        obsidian_data = None

    # 5. 全ペア評価
    print("\n[INFO] Evaluating 22 pairs...")
    results = []
    for pair in PAIR_API:
        price = latest["pairs"].get(pair)
        if not price:
            print(f"[SKIP] {pair}: no price")
            continue
        prices = fetch_history(pair, 280)
        if not prices or len(prices) < 30:
            print(f"[SKIP] {pair}: insufficient history")
            continue
        prices.append(price)
        try:
            r = evaluate_full(pair, price, prices, cb_rates, sentiment, now)
            # 5.5. Obsidian カスタムルール適用
            if obsidian_data:
                r = apply_obsidian_intelligence(r, sentiment, obsidian_data, now)
            results.append(r)
            warn = ""
            if r.get("event_warning"):
                warn = " ⏸EVT"
            if r.get("sentiment_notes"):
                warn += " 🌐SENT"
            if r.get("obsidian_rules_applied"):
                warn += f" 📚OBS({len(r['obsidian_rules_applied'])})"
            print(
                f"  [{stars_to_text(r['stars'])}] {pair:8} {price:>10.4f}  "
                f"TA={r['ta_score']:.0f} FA={r['fa_score']:.0f}  "
                f"{r['verdict']}{warn}"
            )
        except Exception as e:
            print(f"[ERROR] evaluate {pair}: {e}")
            import traceback
            traceback.print_exc()

    if not results:
        print("[FATAL] No results")
        sys.exit(1)

    # 6. 差分検出
    previous_stars = load_previous_state()
    newly, upgraded, is_first = detect_changes(results, previous_stars)
    print(f"\n[INFO] Changes: {len(newly)} new strong, {len(upgraded)} upgraded "
          f"(first run: {is_first})")

    # 7. 通知
    if newly or upgraded or (is_first and newly):
        send_discord(
            os.environ.get("DISCORD_WEBHOOK_URL"),
            newly, upgraded, is_first, results, sentiment
        )
        send_email(
            os.environ.get("SMTP_HOST", "smtp.gmail.com"),
            os.environ.get("SMTP_PORT", "465"),
            os.environ.get("SMTP_USER"),
            os.environ.get("SMTP_PASS"),
            os.environ.get("MAIL_FROM"),
            os.environ.get("MAIL_TO"),
            newly, upgraded, is_first, results, sentiment, us_yields
        )
    else:
        print("[INFO] No significant changes, skipping notifications")

    # 8. 状態保存
    save_current_state(results, sentiment, us_yields, cb_rates, obsidian_data)

    # 9. HTMLレポート
    html = generate_html_report(results, sentiment, us_yields, cb_rates, now)
    os.makedirs("docs", exist_ok=True)
    with open("docs/index.html", "w", encoding="utf-8") as f:
        f.write(html)
    print("[OK] HTML report written")

    print("\n" + "=" * 64)
    print("Done.")
    print("=" * 64)


if __name__ == "__main__":
    main()
