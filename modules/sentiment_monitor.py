"""
市場センチメント取得モジュール
- VIX（恐怖指数）
- DXY（ドルインデックス）
- 米10年債利回り
- 金価格 (XAU/USD)

データソースは複数のフォールバック構成:
1. Stooq（最優先・APIキー不要・安定）
2. Yahoo Finance（バックアップ）
3. 過去キャッシュからの推定
"""

import os
import json
import urllib.request
import urllib.parse
from datetime import datetime, timezone, timedelta

SENTIMENT_CACHE_FILE = "data/sentiment_cache.json"


def http_get(url, timeout=15):
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 (compatible; fx-signal-monitor/1.0)",
            "Accept": "*/*",
        }
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", errors="ignore")


# ---------------------------------------------------------------------------
# Stooq CSVパース（最も安定したデータソース）
# ---------------------------------------------------------------------------

def fetch_stooq(symbol):
    """
    Stooqから直近の終値を取得。
    symbolの例: ^vix.us, dx.f, xauusd, ^tnx.us (10年債利回り)
    """
    url = f"https://stooq.com/q/l/?s={symbol}&f=sd2t2ohlcv&h&e=csv"
    try:
        text = http_get(url, timeout=10)
        lines = text.strip().split("\n")
        if len(lines) < 2:
            print(f"[WARN] Stooq {symbol}: レスポンスが空")
            return None
        header = [h.strip().lower() for h in lines[0].split(",")]
        values = lines[1].split(",")
        row = dict(zip(header, values))
        close = row.get("close", "").strip()
        if close in ("N/D", "", None):
            print(f"[WARN] Stooq {symbol}: N/D（データなし）raw={lines[1][:80]}")
            return None
        return float(close)
    except Exception as e:
        print(f"[WARN] Stooq fetch failed for {symbol}: {e}")
        return None


# ---------------------------------------------------------------------------
# Stooq historical（移動平均計算用）
# ---------------------------------------------------------------------------

def fetch_stooq_history(symbol, days=30):
    """Stooqから過去N日の終値時系列を取得"""
    end = datetime.now(timezone.utc).date()
    start = end - timedelta(days=days + 20)  # 土日祝考慮
    d1 = start.strftime("%Y%m%d")
    d2 = end.strftime("%Y%m%d")
    url = f"https://stooq.com/q/d/l/?s={symbol}&d1={d1}&d2={d2}&i=d"
    try:
        text = http_get(url, timeout=15)
        lines = text.strip().split("\n")
        if len(lines) < 2:
            return []
        closes = []
        for line in lines[1:]:
            parts = line.split(",")
            if len(parts) >= 5:
                try:
                    closes.append(float(parts[4]))
                except ValueError:
                    pass
        return closes[-days:] if len(closes) >= days else closes
    except Exception as e:
        print(f"[WARN] Stooq history fetch failed for {symbol}: {e}")
        return []


# ---------------------------------------------------------------------------
# キャッシュ管理（API失敗時に前回値を使用）
# ---------------------------------------------------------------------------

def load_sentiment_cache():
    if not os.path.exists(SENTIMENT_CACHE_FILE):
        return {}
    try:
        with open(SENTIMENT_CACHE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_sentiment_cache(data):
    os.makedirs(os.path.dirname(SENTIMENT_CACHE_FILE), exist_ok=True)
    try:
        with open(SENTIMENT_CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"[WARN] Sentiment cache save failed: {e}")


# ---------------------------------------------------------------------------
# 個別指標の取得（フォールバック付き）
# ---------------------------------------------------------------------------

def fetch_yahoo(symbol):
    """
    Yahoo Finance から最新値を取得。
    VIX: ^VIX, 米10年債: ^TNX
    """
    url = (
        f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
        "?interval=1d&range=5d"
    )
    try:
        text = http_get(url, timeout=15)
        data = json.loads(text)
        closes = (data["chart"]["result"][0]
                  ["indicators"]["quote"][0]["close"])
        # 末尾から最初のNoneでない値を返す
        for v in reversed(closes):
            if v is not None:
                return round(float(v), 4)
    except Exception as e:
        print(f"[WARN] Yahoo Finance fetch failed for {symbol}: {e}")
    return None


def fetch_vix():
    """VIX恐怖指数 - Yahoo Finance優先、Stooqフォールバック"""
    # Yahoo Finance（最優先）
    val = fetch_yahoo("%5EVIX")   # ^VIX をURLエンコード
    if val is not None:
        print(f"[OK] VIX from Yahoo Finance: {val}")
        return val
    # Stooqフォールバック
    for symbol in ("vix.us", "^vix", "^vix.us", "vix"):
        val = fetch_stooq(symbol)
        if val is not None:
            return val
    print("[WARN] VIX: 全データソース取得失敗")
    return None


def fetch_dxy():
    """ドルインデックス"""
    val = fetch_stooq("dx.f")
    if val is None:
        val = fetch_stooq("usdx")
    if val is None:
        val = fetch_yahoo("DX-Y.NYB")
    return val


def fetch_us10y():
    """米10年債利回り - Yahoo Finance優先、Stooqフォールバック"""
    # Yahoo Finance（最優先）
    val = fetch_yahoo("%5ETNX")   # ^TNX をURLエンコード
    if val is not None:
        # TNXは利回り×10で返ってくる場合がある
        if val > 50:
            val = val / 10
        print(f"[OK] US10Y from Yahoo Finance: {val}")
        return val
    # Stooqフォールバック
    for symbol in ("10us.b", "tnx.us", "^tnx", "^tnx.us"):
        val = fetch_stooq(symbol)
        if val is not None:
            if val > 50:
                val = val / 10
            return val
    print("[WARN] US10Y: 全データソース取得失敗")
    return None


def fetch_gold():
    """金価格 (XAU/USD)"""
    val = fetch_stooq("xauusd")
    if val is None:
        val = fetch_stooq("gc.f")  # Gold futures
    return val


# ---------------------------------------------------------------------------
# 市場センチメント総合評価
# ---------------------------------------------------------------------------

def evaluate_market_sentiment():
    """
    全センチメント指標を取得・評価。
    返り値: dict (vix, dxy, us10y, gold, vix_level, dxy_trend, bond_pressure, gold_trend, risk_mode)
    """
    cache = load_sentiment_cache()
    now_iso = datetime.now(timezone.utc).isoformat()

    # 各指標の取得（失敗時はキャッシュから）
    vix = fetch_vix() or cache.get("vix")
    dxy = fetch_dxy() or cache.get("dxy")
    us10y = fetch_us10y() or cache.get("us10y")
    gold = fetch_gold() or cache.get("gold")

    # DXYのトレンド判定用に過去30日を取得
    dxy_history = fetch_stooq_history("dx.f", days=30)
    if not dxy_history:
        dxy_history = fetch_stooq_history("usdx", days=30)
    dxy_30d_avg = sum(dxy_history) / len(dxy_history) if dxy_history else None

    # 金のトレンド判定
    gold_history = fetch_stooq_history("xauusd", days=20)
    gold_20d_avg = sum(gold_history) / len(gold_history) if gold_history else None

    # キャッシュ更新
    cache.update({
        "timestamp": now_iso,
        "vix": vix,
        "dxy": dxy,
        "us10y": us10y,
        "gold": gold,
        "dxy_30d_avg": dxy_30d_avg,
        "gold_20d_avg": gold_20d_avg,
    })
    save_sentiment_cache(cache)

    # ----- VIX判定 -----
    if vix is None:
        vix_level = "unknown"
        risk_off_strength = 0
    elif vix > 30:
        vix_level = "panic"
        risk_off_strength = 100
    elif vix > 25:
        vix_level = "risk_off"
        risk_off_strength = 70
    elif vix > 20:
        vix_level = "caution"
        risk_off_strength = 40
    elif vix < 13:
        vix_level = "complacent"
        risk_off_strength = -20
    else:
        vix_level = "normal"
        risk_off_strength = 0

    # ----- DXYトレンド判定 -----
    if dxy is None or dxy_30d_avg is None:
        dxy_trend = "unknown"
    elif dxy > dxy_30d_avg * 1.02:
        dxy_trend = "strong"   # ドル強い
    elif dxy < dxy_30d_avg * 0.98:
        dxy_trend = "weak"     # ドル弱い
    else:
        dxy_trend = "flat"

    # ----- 米10年債圧力判定 -----
    if us10y is None:
        bond_pressure = "unknown"
    elif us10y > 5.0:
        bond_pressure = "high"
    elif us10y > 4.5:
        bond_pressure = "elevated"
    elif us10y < 3.5:
        bond_pressure = "low"
    else:
        bond_pressure = "normal"

    # ----- 金トレンド（リスクオフサイン）-----
    if gold is None or gold_20d_avg is None:
        gold_trend = "unknown"
    elif gold > gold_20d_avg * 1.03:
        gold_trend = "surging"  # 急騰=リスクオフ
    elif gold > gold_20d_avg * 1.01:
        gold_trend = "rising"
    elif gold < gold_20d_avg * 0.97:
        gold_trend = "falling"
    else:
        gold_trend = "flat"

    # ----- 全体リスクモード判定 -----
    if vix_level == "panic":
        risk_mode = "panic"
    elif vix_level == "risk_off" or gold_trend == "surging":
        risk_mode = "risk_off"
    elif vix_level == "caution":
        risk_mode = "caution"
    elif vix_level == "complacent":
        risk_mode = "complacent"
    else:
        risk_mode = "normal"

    return {
        "vix": round(vix, 2) if vix else None,
        "vix_level": vix_level,
        "risk_off_strength": risk_off_strength,
        "dxy": round(dxy, 2) if dxy else None,
        "dxy_30d_avg": round(dxy_30d_avg, 2) if dxy_30d_avg else None,
        "dxy_trend": dxy_trend,
        "us10y": round(us10y, 3) if us10y else None,
        "bond_pressure": bond_pressure,
        "gold": round(gold, 2) if gold else None,
        "gold_20d_avg": round(gold_20d_avg, 2) if gold_20d_avg else None,
        "gold_trend": gold_trend,
        "risk_mode": risk_mode,
    }


# ---------------------------------------------------------------------------
# 通貨ペア分類
# ---------------------------------------------------------------------------

EM_PAIRS = {"TRYJPY", "ZARJPY", "MXNJPY", "INRJPY", "CNYJPY"}
SAFE_HAVEN_TARGETS = {"USDJPY", "USDCHF", "EURCHF", "CHFJPY"}


def classify_pair(pair):
    if pair in EM_PAIRS:
        return "emerging_market"
    if pair in SAFE_HAVEN_TARGETS:
        return "safe_haven"
    if pair.startswith("USD") or pair.endswith("USD"):
        return "dollar"
    return "major"


# ---------------------------------------------------------------------------
# シグナルへのセンチメント反映
# ---------------------------------------------------------------------------

def apply_sentiment_filter(pair, base_result, sentiment):
    """市場センチメントに基づきシグナルを補正"""
    pair_type = classify_pair(pair)
    notes = []

    # 1. パニック時：新興国通貨を完全ブロック
    if pair_type == "emerging_market" and sentiment["risk_mode"] == "panic":
        base_result["original_stars"] = base_result["stars"]
        base_result["stars"] = 1
        base_result["verdict"] = "⛔ パニック・新興国取引停止"
        base_result["direction"] = "BLOCKED_PANIC"
        notes.append(f"VIX={sentiment['vix']} パニックモード")

    # 2. リスクオフ時：新興国ロング抑制
    elif pair_type == "emerging_market" and sentiment["risk_mode"] == "risk_off":
        if base_result["direction"].endswith("LONG"):
            base_result["original_stars"] = base_result["stars"]
            base_result["stars"] = max(1, base_result["stars"] - 2)
            notes.append(f"VIX={sentiment['vix']} リスクオフ・新興国ロング抑制")

    # 3. 警戒モード：新興国ロングを1段階下げる
    elif pair_type == "emerging_market" and sentiment["risk_mode"] == "caution":
        if base_result["direction"].endswith("LONG") and base_result["stars"] >= 4:
            base_result["original_stars"] = base_result["stars"]
            base_result["stars"] = max(3, base_result["stars"] - 1)
            notes.append(f"VIX={sentiment['vix']} 警戒・新興国シグナル軽減")

    # 4. DXY上昇トレンド時のドル買い加勢
    if sentiment["dxy_trend"] == "strong":
        if pair.startswith("USD") and pair_type == "dollar":
            if base_result["direction"].endswith("LONG"):
                if base_result["stars"] < 5:
                    base_result["stars"] = min(5, base_result["stars"] + 1)
                    notes.append(f"DXY={sentiment['dxy']} 上昇・ドル買い加勢")

    # 5. DXY下降トレンド時のドル売り加勢
    if sentiment["dxy_trend"] == "weak":
        if pair.startswith("USD") and pair_type == "dollar":
            if base_result["direction"].endswith("LONG") and base_result["stars"] >= 4:
                notes.append(f"DXY={sentiment['dxy']} 下降・ドル買い慎重に")

    # 6. 米10年債利回り高水準時：ドル資産有利
    if sentiment["bond_pressure"] in ("high", "elevated"):
        if pair == "USDJPY" and base_result["direction"].endswith("LONG"):
            notes.append(f"米10y={sentiment['us10y']}% ドル買い圧力")

    # 7. リスクオフ時の円買い圧力警戒
    if sentiment["risk_mode"] in ("risk_off", "panic"):
        if pair.endswith("JPY") and base_result["direction"].endswith("LONG"):
            notes.append("リスクオフ・円買い圧力警戒")
            # ★を下げないがwarningを記録
            base_result["sentiment_warning"] = notes[-1]

    # 8. 金急騰時のリスクオフサイン
    if sentiment["gold_trend"] == "surging":
        notes.append(f"金価格={sentiment['gold']} 急騰・有事の安全資産需要")

    if notes:
        base_result["sentiment_notes"] = notes

    return base_result
