#!/usr/bin/env python3
"""
FX売買シグナル監視スキャナー（レベル3 Advanced）
8つの高度分析機能を統合した最終版。
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
)
from modules.ai_commentary import generate_market_commentary
from modules.ambush_alert import evaluate_ambush, collect_ambush_alerts

PAGES_URL = "https://applejoker01-afk.github.io/fx-signal-monitor/"

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
        "dma_score": dma_score, "macd_score": macd_score, "rsi_score": rsi_score,
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

    if agree and ta["ta_score"] >= 75 and fa["score"] >= 65:
        stars = 5; verdict = "◎ 高信頼ロング" if fa_sign > 0 else "◎ 高信頼ショート"
        direction = "LONG" if fa_sign > 0 else "SHORT"
    elif agree and ta["ta_score"] >= 60 and fa["score"] >= 55:
        stars = 4; verdict = "○ ロング条件成立" if fa_sign > 0 else "○ ショート条件成立"
        direction = "LONG" if fa_sign > 0 else "SHORT"
    elif agree and ta["ta_score"] <= 25 and fa["score"] <= 35:
        stars = 5; verdict = "◎ 高信頼ショート"; direction = "SHORT"
    elif agree and ta["ta_score"] <= 40 and fa["score"] <= 45:
        stars = 4; verdict = "○ ショート条件成立"; direction = "SHORT"
    elif conflict:
        stars = 1; verdict = "⚠ 見送り（FA/TA不一致）"; direction = "NO_TRADE"
    elif fa_sign == 0 and ta_sign == 0:
        stars = 2; verdict = "— レンジ"; direction = "NO_TRADE"
    else:
        stars = 2; verdict = "△ 弱シグナル"
        direction = "LIGHT_" + ("LONG" if (ta_sign + fa_sign) > 0 else "SHORT")

    result = {
        "pair": pair, "label": PAIR_LABEL[pair], "price": round(price, 5),
        **ta,
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
                 drawdown=None, ai_commentary=None, ambush_alerts=None):
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
        # シグナル変化はないが、待ち伏せ・トレード・ドローダウンのいずれかで呼ばれたケース
        has_ambush_now = bool(ambush_alerts and ambush_alerts.get("high_confidence"))
        has_trade_now = bool(trade_update and (trade_update.get("newly_opened") or trade_update.get("newly_closed")))
        has_dd_now = bool(drawdown and drawdown.get("alert"))

        if has_ambush_now:
            title = f"{risk_emoji} 🎯 待ち伏せシグナル発火"
            n_amb = len(ambush_alerts.get("high_confidence", []))
            desc = f"高確度ゾーン到達: **{n_amb}件** / 市場モード: **{risk_mode.upper()}**"
            color = 0x4ADE80
        elif has_trade_now:
            title = f"{risk_emoji} 💼 トレード更新"
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

    # ⑮ AI市況コメンタリー（最上部に表示）
    if ai_commentary:
        embeds[0]["fields"].append({
            "name": "🤖 AI市況コメンタリー",
            "value": ai_commentary[:1024],
            "inline": False
        })

    # 🎯 待ち伏せアラート（高確度ゾーン・最重要）
    if ambush_alerts and ambush_alerts.get("high_confidence"):
        held_pairs = set((open_trades or {}).keys())
        lines = []
        for a in ambush_alerts["high_confidence"][:5]:
            n = a["nearest"]
            # 保有中か新規かを明示
            if a["pair"] in held_pairs:
                hold_tag = "📦保有中（追加せず継続推奨）"
            else:
                hold_tag = "🆕新規候補"
            block = (
                f"{'★'*a['stars']} {a['label']} {a['direction']} [{hold_tag}]\n"
                f"  {n['role']}({n['price']}) あと{n['distance_pct']:.2f}% — {a['quality']}"
            )
            # TP/SL目安を追加
            if a.get("sl") is not None:
                block += (
                    f"\n  SL:{a.get('sl')} / TP1:{a.get('tp1')} / "
                    f"TP2:{a.get('tp2')} / TP3:{a.get('tp3')}"
                )
            lines.append(block)
        embeds[0]["fields"].append({
            "name": "🎯 高確度ゾーン到達（待ち伏せ）",
            "value": "\n\n".join(lines)[:1024],
            "inline": False
        })

    # 🎯 POI接近中（シグナル未成立だが重要価格に近い）
    if ambush_alerts and ambush_alerts.get("approaching"):
        held_pairs = set((open_trades or {}).keys())
        lines = []
        for a in ambush_alerts["approaching"][:6]:
            n = a["nearest"]
            plan = a.get("plan")
            hold = " 📦保有中" if a["pair"] in held_pairs else ""
            block = f"・{a['label']}{hold} | {n['role']}({n['price']}) あと{n['distance_pct']:.2f}%"
            if plan:
                block += (
                    f"\n  → {plan['direction']} "
                    f"SL:{plan['sl']} / TP1:{plan['tp1']} / TP2:{plan['tp2']} / TP3:{plan['tp3']} "
                    f"(RR 1:{plan['rr1']}/1:{plan['rr2']}/1:{plan['rr3']})"
                )
            lines.append(block)
        if lines:
            embeds[0]["fields"].append({
                "name": "👀 重要価格に接近中（反発狙いの監視）",
                "value": "\n\n".join(lines)[:1024],
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

    # ⑦ トレード情報（新規エントリー・決済・保有中）
    if trade_update:
        # 新規エントリー
        newly_opened = trade_update.get("newly_opened", [])
        if newly_opened:
            lines = []
            for t in newly_opened[:5]:
                lines.append(
                    f"+ {t['pair']} {t['direction']} @ {t['entry_price']}\n"
                    f"   SL:{t.get('sl','?')} / TP1:{t.get('tp1','?')} / "
                    f"TP2:{t.get('tp2','?')} / TP3:{t.get('tp3','?')}"
                )
            embeds[0]["fields"].append({
                "name": f"📌 新規エントリー（{len(newly_opened)}件）",
                "value": "```diff\n" + "\n".join(lines) + "\n```",
                "inline": False
            })

        # 決済
        newly_closed = trade_update.get("newly_closed", [])
        if newly_closed:
            reason_label = {
                "TP3_HIT": "🏆TP3到達",
                "TP2_HIT": "✅TP2到達",
                "TP1_HIT": "✅TP1到達",
                "SL_HIT": "❌SL到達",
                "SIGNAL_LOST": "➖シグナル消滅",
                "REVERSED": "🔄方向反転",
            }
            lines = []
            for t in newly_closed[:5]:
                rl = reason_label.get(t.get("exit_reason"), t.get("exit_reason", "?"))
                prefix = "+" if t.get("result") == "WIN" else ("-" if t.get("result") == "LOSS" else " ")
                lines.append(
                    f"{prefix} {t['pair']} {t['direction']} {rl}\n"
                    f"   {t.get('entry_price','?')} → {t.get('exit_price','?')} "
                    f"({t.get('pips',0):+.4f}) 保有{t.get('hold_hours',0)}h"
                )
            embeds[0]["fields"].append({
                "name": f"💼 決済完了（{len(newly_closed)}件）",
                "value": "```diff\n" + "\n".join(lines) + "\n```",
                "inline": False
            })

    # 現在保有中（オープントレード一覧）
    if open_trades:
        from datetime import datetime as _dt
        now_utc = _dt.now(timezone.utc)
        lines = []
        for pair, t in list(open_trades.items())[:8]:
            try:
                entry_dt = _dt.fromisoformat(t["entry_time"])
                if entry_dt.tzinfo is None:
                    entry_dt = entry_dt.replace(tzinfo=timezone.utc)
                hours_held = (now_utc - entry_dt).total_seconds() / 3600
                hold_str = f"{hours_held:.0f}h"
            except Exception:
                hold_str = "?"
            lines.append(
                f"・{pair} {t['direction']} @ {t['entry_price']} (経過{hold_str})"
            )
        if lines:
            embeds[0]["fields"].append({
                "name": f"📦 現在保有中（{len(open_trades)}件）",
                "value": "\n".join(lines),
                "inline": False
            })

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
            f"価格: {r['price']}\n"
            f"方向: {r['direction']}\n"
            f"TA: {r['ta_score']}/100  FA: {r['fa_score']}/100\n"
            f"金利差: {rate_diff_str}\n"
        )

        # ② ボラレジーム + ③ 段階的TP
        if regime:
            value += f"ボラ: {regime.get('regime_label','')} (ATR比{regime.get('atr_ratio',1):.1f}倍)\n"
        if staged:
            value += (
                f"SL: {staged.get('sl','?')} | "
                f"TP1: {staged.get('tp1','?')} | "
                f"TP2: {staged.get('tp2','?')} | "
                f"TP3: {staged.get('tp3','?')}\n"
                f"RR: 1:{staged.get('rr_tp1','?')} / 1:{staged.get('rr_tp2','?')} / 1:{staged.get('rr_tp3','?')}\n"
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

        embeds[0]["fields"].append({
            "name": f"{stars_to_text(r['stars'])} {r['label']} - {r['verdict']}",
            "value": value[:1024], "inline": False
        })

    for r in upgraded:
        embeds[0]["fields"].append({
            "name": f"⬆ 昇格: {r['label']} ★4→★5",
            "value": f"```\n価格: {r['price']}  方向: {r['direction']}\n```",
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


def send_email(smtp_host, smtp_port, smtp_user, smtp_pass,
               from_addr, to_addr, newly, upgraded, is_first,
               all_results, sentiment, us_yields):
    if not all([smtp_host, smtp_user, smtp_pass, from_addr, to_addr]):
        print("[INFO] Email not configured")
        return False

    jst = datetime.now(timezone.utc) + timedelta(hours=9)
    timestamp = jst.strftime("%Y-%m-%d %H:%M JST")
    risk_mode = sentiment.get("risk_mode", "?") if sentiment else "?"

    subject = (
        f"[FX Monitor L3] 監視開始 - ★4以上{len(newly)}件 / 市場:{risk_mode}"
        if is_first else
        f"[FX Monitor L3] 新規{len(newly)} / 昇格{len(upgraded)} / 市場:{risk_mode} ({timestamp})"
    )

    def fmt_signal(r):
        staged = r.get("staged_tp", {})
        regime = r.get("volatility_regime", {})
        carry = r.get("carry_score", {})
        sr = r.get("support_resistance", {})
        interv = r.get("intervention_risk", {})
        lines = [
            f"{stars_to_text(r['stars'])} {r['label']} @ {r['price']}",
            f"  {r['verdict']} / {r['direction']}",
            f"  TA={r['ta_score']}/100  FA={r['fa_score']}/100  金利差{r.get('fa_rate_diff','N/A'):+.2f}%",
            f"  {r['fa_detail']}",
        ]
        if regime:
            lines.append(f"  ② ボラ: {regime.get('regime_label','')} (比率{regime.get('atr_ratio',1):.1f}x)")
        if staged:
            lines.append(f"  ③ SL:{staged.get('sl','?')} TP1:{staged.get('tp1','?')} TP2:{staged.get('tp2','?')} TP3:{staged.get('tp3','?')}")
            lines.append(f"     戦略: {staged.get('strategy','')}")
        if carry and r.get("fa_rate_diff", 0) and r.get("fa_rate_diff", 0) > 0:
            lines.append(f"  ⑤ キャリー: {carry.get('label','')} | SL回収{carry.get('breakeven_days','?')}日")
        if sr and sr.get("context"):
            lines.append(f"  ⑥ SR: {sr.get('context','')}")
        if interv and interv.get("risk_level") in ("HIGH", "CRITICAL"):
            lines.append(f"  ⑧ 介入リスク: {interv.get('risk_label','')} ({interv.get('risk_score',0)}/100)")
        return "\n".join(lines)

    body_lines = ["=" * 60, f"  FX売買シグナル通知  {timestamp}", "=" * 60, ""]
    if newly:
        body_lines.append("【★4以上の新規シグナル】")
        for r in newly:
            body_lines.append(fmt_signal(r))
            body_lines.append("")
    if upgraded:
        body_lines.append("【★4→★5 昇格】")
        for r in upgraded:
            body_lines.append(fmt_signal(r))
            body_lines.append("")
    body_lines.extend([
        "=" * 60,
        f"  ダッシュボード: {PAGES_URL}",
        "  本通知は教育・研究目的。投資判断は自己責任で。",
        "=" * 60,
    ])

    msg = MIMEMultipart("alternative")
    msg["Subject"] = Header(subject, "utf-8")
    msg["From"] = from_addr; msg["To"] = to_addr
    msg.attach(MIMEText("\n".join(body_lines), "plain", "utf-8"))

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

        # SR最近傍
        sr = r.get("support_resistance", {})
        sr_str = ""
        if sr.get("nearest_resistance") and row_cls == "buy":
            nr = sr["nearest_resistance"]
            if nr["distance_pct"] < 0.5:
                sr_str = f'<div class="warn-line">⚠ レジスタンス({nr["price"]})まで{nr["distance_pct"]:.2f}%</div>'
        elif sr.get("nearest_support") and row_cls == "sell":
            ns = sr["nearest_support"]
            if ns["distance_pct"] < 0.5:
                sr_str = f'<div class="warn-line">⚠ サポート({ns["price"]})まで{ns["distance_pct"]:.2f}%</div>'

        # TP情報
        staged = r.get("staged_tp", {})
        tp_str = ""
        if staged:
            tp_str = (
                f'<div style="font-family:var(--mono);font-size:0.7rem;color:var(--text-muted);margin-top:0.2rem">'
                f'SL:{staged.get("sl","?")} TP1:{staged.get("tp1","?")} '
                f'TP2:{staged.get("tp2","?")} TP3:{staged.get("tp3","?")}'
                f'</div>'
            )

        warn_html = " ".join(badges)
        event_html = ""
        if r.get("event_warning"):
            event_html = f'<div class="warn-line">⏸ {r["event_warning"]}</div>'
        if r.get("sentiment_notes"):
            event_html += '<div class="warn-line">🌐 ' + " / ".join(r["sentiment_notes"]) + '</div>'

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
          <td class="num-cell">{r['price']}</td>
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

    # 2. 中央銀行金利
    cb_rates = load_central_bank_rates()
    print(f"[OK] Central bank rates: {len(cb_rates)} currencies")

    # 3. 米国債利回り
    us_yields = fetch_us_treasury_yields()
    print(f"[OK] US yields: 10y={us_yields.get('10y')}%")

    # 4. 市場センチメント
    print("[INFO] Fetching market sentiment...")
    sentiment = evaluate_market_sentiment()
    print(f"[OK] Sentiment: VIX={sentiment.get('vix')} mode={sentiment.get('risk_mode')}")

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
    print("\n[INFO] Evaluating 22 pairs...")

    # ⑩ 自己学習: 過去の決済実績からペア別信頼度マップを構築
    from modules.trade_tracker import load_closed_trades
    closed_trades_all = load_closed_trades(days_back=90)
    perf_map = build_pair_performance_map(closed_trades_all, min_trades=5)
    if perf_map:
        adjusted = [p for p, v in perf_map.items() if v.get("adjustment", 0) != 0]
        if adjusted:
            print(f"[OK] 自己学習: {len(adjusted)}ペアに実績調整を適用")

    results = []
    all_pair_prices = {}   # ① 通貨強弱メーター用
    all_histories = {}     # ⑨ バックテスト用（フル履歴）

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

            results.append(r)

            regime = r.get("volatility_regime", {})
            staged = r.get("staged_tp", {})
            warn = ""
            if r.get("event_warning"): warn += " EVT"
            if r.get("sentiment_notes"): warn += " SENT"
            if regime.get("regime") == "high": warn += f" ⚡HighVol"
            if r.get("intervention_risk", {}).get("risk_level") in ("HIGH","CRITICAL"):
                warn += f" 🚨介入"
            print(
                f"  [{stars_to_text(r['stars'])}] {pair:8} {price:>10.4f}  "
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
    from modules.trade_tracker import load_open_trades
    trade_update = update_trades(results, now)
    if trade_update["newly_opened"]:
        print(f"\n[TRADE] 新規エントリー {len(trade_update['newly_opened'])}件:")
        for t in trade_update["newly_opened"]:
            print(f"  + {t['pair']} {t['direction']} @ {t['entry_price']} "
                  f"(SL:{t.get('sl')} TP1:{t.get('tp1')})")
    if trade_update["newly_closed"]:
        print(f"[TRADE] 決済 {len(trade_update['newly_closed'])}件:")
        for t in trade_update["newly_closed"]:
            print(f"  - {t['pair']} {t['direction']} {t['result']} "
                  f"({t['exit_reason']}) {t.get('pips',0):+.4f} "
                  f"保有{t.get('hold_hours',0)}h")
    print(f"[TRADE] 現在保有中: {trade_update['still_open']}件")
    open_trades = load_open_trades()

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

    # ⑮ AI市況コメンタリー（ANTHROPIC_API_KEY設定時のみ）
    ai_commentary = None
    if newly or upgraded or (is_first and newly):
        ai_commentary = generate_market_commentary(
            results, sentiment, currency_strength, market_regime
        )
        if ai_commentary:
            print(f"[AI] 市況コメンタリー生成完了")

    # 7. 通知（トレード情報も同梱・決済が出たら必ず送信）
    has_trade_change = bool(trade_update.get("newly_opened") or trade_update.get("newly_closed"))
    has_ambush = bool(ambush_alerts.get("high_confidence"))
    if newly or upgraded or (is_first and newly) or has_trade_change or drawdown.get("alert") or has_ambush:
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
            ambush_alerts=ambush_alerts,
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
