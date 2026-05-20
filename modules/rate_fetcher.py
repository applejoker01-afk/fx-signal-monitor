"""
金利・債券利回り取得モジュール
- 中央銀行政策金利（手動メンテのJSONから読込）
- 米国債利回り（U.S. Treasury Fiscal Data API）
- 金利差スコアの計算
"""

import json
import os
import urllib.request
from datetime import datetime, timezone, timedelta

CENTRAL_BANK_FILE = "data/central_bank_rates.json"


def load_central_bank_rates():
    """JSONファイルから中央銀行政策金利を読込"""
    if not os.path.exists(CENTRAL_BANK_FILE):
        print(f"[WARN] {CENTRAL_BANK_FILE} not found, using fallback")
        return _fallback_rates()
    try:
        with open(CENTRAL_BANK_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data.get("rates", {})
    except Exception as e:
        print(f"[WARN] Failed to load central bank rates: {e}")
        return _fallback_rates()


def _fallback_rates():
    """JSONが読めない場合のフォールバック（2026年5月時点）"""
    return {
        "USD": {"rate": 4.75, "stance": "tighten", "cb_name": "FRB"},
        "EUR": {"rate": 2.65, "stance": "ease", "cb_name": "ECB"},
        "JPY": {"rate": 0.75, "stance": "tighten", "cb_name": "日銀"},
        "GBP": {"rate": 4.25, "stance": "neutral", "cb_name": "BOE"},
        "AUD": {"rate": 4.10, "stance": "neutral", "cb_name": "RBA"},
        "NZD": {"rate": 4.50, "stance": "ease", "cb_name": "RBNZ"},
        "CAD": {"rate": 3.25, "stance": "neutral", "cb_name": "BOC"},
        "CHF": {"rate": 0.50, "stance": "ease", "cb_name": "SNB"},
        "SGD": {"rate": 3.00, "stance": "tighten", "cb_name": "MAS"},
        "HKD": {"rate": 4.75, "stance": "tighten", "cb_name": "HKMA"},
        "CNY": {"rate": 3.10, "stance": "ease", "cb_name": "PBOC"},
        "MXN": {"rate": 7.00, "stance": "ease", "cb_name": "Banxico"},
        "TRY": {"rate": 38.00, "stance": "ease", "cb_name": "CBRT"},
        "ZAR": {"rate": 7.50, "stance": "ease", "cb_name": "SARB"},
        "INR": {"rate": 6.00, "stance": "neutral", "cb_name": "RBI"},
    }


def fetch_us_treasury_yields():
    """
    米国財務省のFiscalデータAPIから米国債利回りを取得。
    APIキー不要・無制限。
    返り値: {"2y": 4.85, "5y": 4.60, "10y": 4.35, "30y": 4.55, "spread_10y2y": -0.50}
    """
    today = datetime.now(timezone.utc).date()
    # 過去14日のレンジで最新値を取得（土日祝で空く可能性を考慮）
    start_date = today - timedelta(days=14)
    url = (
        "https://api.fiscaldata.treasury.gov/services/api/fiscal_service/"
        "v2/accounting/od/avg_interest_rates"
        f"?filter=record_date:gte:{start_date}"
        "&sort=-record_date"
        "&page[size]=100"
    )
    try:
        req = urllib.request.Request(
            url, headers={"User-Agent": "fx-signal-monitor/1.0"}
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        if not data.get("data"):
            print("[WARN] US Treasury API returned no data")
            return _fallback_yields()

        # security_descに「Treasury Notes」「Treasury Bonds」等が含まれる行から取得
        yields = {}
        for row in data["data"]:
            desc = row.get("security_desc", "").lower()
            try:
                rate = float(row.get("avg_interest_rate_amt", 0))
            except (ValueError, TypeError):
                continue
            if "treasury notes" in desc and "2y" not in yields:
                yields["10y"] = rate  # 国債総合（10年に近似）
            elif "treasury bonds" in desc and "30y" not in yields:
                yields["30y"] = rate
            elif "treasury bills" in desc and "short" not in yields:
                yields["short"] = rate

        if not yields:
            return _fallback_yields()

        # Estimateで埋める
        result = {
            "short": yields.get("short", 5.00),
            "2y": yields.get("short", 5.00) - 0.15,
            "10y": yields.get("10y", 4.35),
            "30y": yields.get("30y", 4.55),
        }
        result["spread_10y2y"] = result["10y"] - result["2y"]
        result["source"] = "U.S. Treasury Fiscal Data API"
        return result
    except Exception as e:
        print(f"[WARN] US Treasury fetch failed: {e}")
        return _fallback_yields()


def _fallback_yields():
    """API失敗時のフォールバック（2026年5月時点）"""
    return {
        "short": 4.85, "2y": 4.85, "10y": 4.35, "30y": 4.55,
        "spread_10y2y": -0.50,
        "source": "fallback (estimated)",
    }


# ---------------------------------------------------------------------------
# 金利差ベースのFAスコア計算（添付資料に基づく）
# ---------------------------------------------------------------------------

def compute_fa_score(pair, pair_api, central_bank_rates):
    """
    金利差と中銀スタンスから動的にFAスコアを算出。
    返り値: dict (score: 0-100, direction: buy/sell/neutral, detail: str, rate_diff: float)
    """
    from_ccy, to_ccy = pair_api[pair]
    rate_from = central_bank_rates.get(from_ccy, {}).get("rate")
    rate_to = central_bank_rates.get(to_ccy, {}).get("rate")
    stance_from = central_bank_rates.get(from_ccy, {}).get("stance", "neutral")
    stance_to = central_bank_rates.get(to_ccy, {}).get("stance", "neutral")

    if rate_from is None or rate_to is None:
        return {
            "score": 50.0,
            "direction": "neutral",
            "rate_diff": None,
            "detail": "金利データ取得失敗",
            "stance_from": stance_from,
            "stance_to": stance_to,
        }

    rate_diff = rate_from - rate_to  # FROM買い・TO売りした際の年間スワップ相当

    # 基本スコア: 金利差の絶対値（最大±30点）
    diff_magnitude = min(abs(rate_diff) * 7.5, 30)
    diff_score = diff_magnitude if rate_diff > 0 else -diff_magnitude

    # スタンスダイバージェンスボーナス（金利差が将来も拡大か縮小か）
    stance_bonus = 0
    if rate_diff > 0:
        if stance_from == "tighten" and stance_to == "ease":
            stance_bonus = 15
        elif stance_from == "ease":
            stance_bonus = -10
        elif stance_from == "tighten":
            stance_bonus = 5
    elif rate_diff < 0:
        if stance_from == "ease" and stance_to == "tighten":
            stance_bonus = -15
        elif stance_from == "tighten":
            stance_bonus = 10

    # 最終スコア（50を中立基準）
    final_score = 50 + diff_score + stance_bonus
    final_score = max(0, min(100, final_score))

    if final_score >= 60:
        direction = "buy"
    elif final_score <= 40:
        direction = "sell"
    else:
        direction = "neutral"

    cb_from = central_bank_rates.get(from_ccy, {}).get("cb_name", from_ccy)
    cb_to = central_bank_rates.get(to_ccy, {}).get("cb_name", to_ccy)
    detail = (
        f"{cb_from}({rate_from:.2f}% {stance_from}) vs "
        f"{cb_to}({rate_to:.2f}% {stance_to}) "
        f"差{rate_diff:+.2f}%"
    )

    return {
        "score": round(final_score, 1),
        "direction": direction,
        "rate_diff": round(rate_diff, 2),
        "detail": detail,
        "rate_from": rate_from,
        "rate_to": rate_to,
        "stance_from": stance_from,
        "stance_to": stance_to,
        "cb_from": cb_from,
        "cb_to": cb_to,
    }
