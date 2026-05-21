"""
中央銀行政策金利の自動取得モジュール

データソース（優先順位順）:
  USD: FRED API (FEDFUNDS) - APIキー必要
  EUR: ECB公式XML - キー不要
  GBP: BOE公式XML - キー不要
  JPY: 日銀公式（手動JSON補完）
  AUD: RBA公式XML - キー不要
  NZD/CAD/CHF/その他: 既存JSONからfallback

取得失敗時は既存 central_bank_rates.json の値を保持する。
"""

import json
import os
import re
import urllib.request
import urllib.error
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

CB_RATES_FILE = "data/central_bank_rates.json"
USER_AGENT = "fx-signal-monitor/1.0"


def http_get(url, timeout=20, headers=None):
    """シンプルなHTTP GET"""
    h = {"User-Agent": USER_AGENT, "Accept": "*/*"}
    if headers:
        h.update(headers)
    req = urllib.request.Request(url, headers=h)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", errors="ignore")


# ===========================================================================
# USD: FRED API（米連邦準備制度経済データ）
# ===========================================================================

def fetch_usd_fed_funds():
    """
    FRED APIから米FF金利を取得。
    https://fred.stlouisfed.org/docs/api/fred/series_observations.html
    series_id=FEDFUNDS（実効FF金利・月次）
           DFF（実効FF金利・日次・遅延あり）
    """
    api_key = os.environ.get("FRED_API_KEY")
    if not api_key:
        print("[INFO] FRED_API_KEY not set, skipping USD auto-fetch")
        return None

    # 最新の月次データを取得
    url = (
        "https://api.stlouisfed.org/fred/series/observations"
        f"?series_id=FEDFUNDS&api_key={api_key}&file_type=json"
        "&sort_order=desc&limit=2"
    )
    try:
        data = json.loads(http_get(url))
        obs = data.get("observations", [])
        if not obs:
            return None
        latest = obs[0]
        rate = float(latest["value"])
        return {
            "rate": rate,
            "date": latest["date"],
            "source": "FRED FEDFUNDS",
        }
    except Exception as e:
        print(f"[WARN] FRED fetch failed: {e}")
        return None


# ===========================================================================
# EUR: ECB公式XML
# ===========================================================================

def fetch_eur_ecb_rate():
    """
    ECB公式のSDMXフォーマットから主要リファイナンス金利を取得。
    https://data-api.ecb.europa.eu/service/data/FM/D.U2.EUR.4F.KR.MRR_FR.LEV
    """
    url = (
        "https://data-api.ecb.europa.eu/service/data/FM/"
        "D.U2.EUR.4F.KR.MRR_FR.LEV?lastNObservations=1&format=jsondata"
    )
    try:
        data = json.loads(http_get(url))
        # SDMXのJSONは複雑なので、observationだけ抽出
        observations = data.get("dataSets", [{}])[0].get("series", {})
        if not observations:
            return None
        # 1つだけの系列から最新値を取得
        series_data = list(observations.values())[0]
        obs = series_data.get("observations", {})
        if not obs:
            return None
        latest_key = sorted(obs.keys(), key=lambda x: int(x))[-1]
        rate = float(obs[latest_key][0])

        # 日付を取り出す
        time_dim = data.get("structure", {}).get("dimensions", {}).get("observation", [])
        date_str = None
        for dim in time_dim:
            if dim.get("id") == "TIME_PERIOD":
                values = dim.get("values", [])
                if values and int(latest_key) < len(values):
                    date_str = values[int(latest_key)].get("id")

        return {
            "rate": rate,
            "date": date_str,
            "source": "ECB SDMX (MRR_FR)",
        }
    except Exception as e:
        print(f"[WARN] ECB fetch failed: {e}")
        return None


# ===========================================================================
# GBP: BOE公式 IADB
# ===========================================================================

def fetch_gbp_boe_rate():
    """
    Bank of EnglandのIADBから政策金利を取得。
    シリーズコード: IUDSOIA（公式銀行レート相当）
    """
    today = datetime.now(timezone.utc).strftime("%d/%b/%Y")
    # 過去30日のレンジで最新値を取得
    from datetime import timedelta
    start = (datetime.now(timezone.utc) - timedelta(days=30)).strftime("%d/%b/%Y")

    url = (
        "https://www.bankofengland.co.uk/boeapps/iadb/fromshowcolumns.asp"
        f"?csv.x=yes&Datefrom={start}&Dateto={today}"
        "&SeriesCodes=IUDBEDR&UsingCodes=Y&CSVF=TN&VPD=Y"
    )
    try:
        text = http_get(url, timeout=15)
        lines = text.strip().split("\n")
        if len(lines) < 2:
            return None
        # CSVフォーマット: "DATE","IUDBEDR"
        last_line = lines[-1]
        parts = [p.strip().strip('"') for p in last_line.split(",")]
        if len(parts) >= 2:
            rate = float(parts[1])
            return {
                "rate": rate,
                "date": parts[0],
                "source": "BOE IADB (IUDBEDR)",
            }
    except Exception as e:
        print(f"[WARN] BOE fetch failed: {e}")
    return None


# ===========================================================================
# AUD: RBA公式XML
# ===========================================================================

def fetch_aud_rba_rate():
    """
    Reserve Bank of Australia の Cash Rate Target を取得。
    F1.1 シリーズ
    """
    url = "https://www.rba.gov.au/statistics/cash-rate/cash-rate-data.csv"
    try:
        text = http_get(url, timeout=15)
        lines = text.strip().split("\n")
        # ヘッダ後の最新行を探す
        for line in reversed(lines):
            parts = [p.strip() for p in line.split(",")]
            if len(parts) >= 3 and parts[0] and re.match(r"\d{4}-\d{2}-\d{2}", parts[0]):
                try:
                    rate = float(parts[1])
                    return {
                        "rate": rate,
                        "date": parts[0],
                        "source": "RBA Cash Rate",
                    }
                except ValueError:
                    continue
    except Exception as e:
        print(f"[WARN] RBA fetch failed: {e}")
    return None


# ===========================================================================
# 統合更新ロジック
# ===========================================================================

# 各通貨ごとの取得関数マッピング
FETCH_FUNCTIONS = {
    "USD": fetch_usd_fed_funds,
    "EUR": fetch_eur_ecb_rate,
    "GBP": fetch_gbp_boe_rate,
    "AUD": fetch_aud_rba_rate,
}


def update_central_bank_rates(dry_run=False):
    """
    既存JSONを読み込み、自動取得可能な通貨だけ更新する。
    手動メンテ通貨（JPY, NZD, CAD, CHF, MXN, TRY, ZAR, INR, SGD, HKD, CNY）はそのまま保持。

    Args:
        dry_run: Trueの場合はファイル書き込みせず結果dictだけ返す

    Returns:
        {"updated": [...], "kept": [...], "errors": [...], "snapshot": {...}}
    """
    if not os.path.exists(CB_RATES_FILE):
        print(f"[ERROR] {CB_RATES_FILE} not found - cannot update")
        return {"updated": [], "kept": [], "errors": ["file not found"]}

    with open(CB_RATES_FILE, "r", encoding="utf-8") as f:
        current = json.load(f)

    rates_section = current.get("rates", {})
    updated = []
    kept = []
    errors = []

    for ccy, fetch_fn in FETCH_FUNCTIONS.items():
        print(f"[INFO] Fetching {ccy} policy rate...")
        try:
            result = fetch_fn()
        except Exception as e:
            errors.append(f"{ccy}: {e}")
            kept.append(ccy)
            continue

        if not result or result.get("rate") is None:
            kept.append(ccy)
            continue

        new_rate = round(result["rate"], 4)
        old_rate = rates_section.get(ccy, {}).get("rate")

        if old_rate is None or abs(new_rate - old_rate) >= 0.001:
            rates_section[ccy]["rate"] = new_rate
            rates_section[ccy]["last_auto_update"] = result.get("date") or datetime.now(timezone.utc).isoformat()[:10]
            rates_section[ccy]["auto_source"] = result.get("source", "unknown")
            updated.append({
                "currency": ccy,
                "old_rate": old_rate,
                "new_rate": new_rate,
                "source": result.get("source"),
            })
            print(f"  ✓ {ccy}: {old_rate}% → {new_rate}% ({result.get('source')})")
        else:
            kept.append(ccy)
            print(f"  = {ccy}: {new_rate}% (no change)")

    # 全自動化されていない通貨もkeptに追加
    auto_currencies = set(FETCH_FUNCTIONS.keys())
    for ccy in rates_section.keys():
        if ccy not in auto_currencies and ccy not in kept:
            kept.append(ccy)

    current["rates"] = rates_section
    current["last_updated"] = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    current["last_auto_run"] = datetime.now(timezone.utc).isoformat()

    if not dry_run:
        with open(CB_RATES_FILE, "w", encoding="utf-8") as f:
            json.dump(current, f, ensure_ascii=False, indent=2)
        print(f"[OK] {CB_RATES_FILE} updated. {len(updated)} changes, {len(kept)} unchanged.")

    return {
        "updated": updated,
        "kept": kept,
        "errors": errors,
        "snapshot": rates_section,
    }


if __name__ == "__main__":
    # スタンドアロン実行用
    result = update_central_bank_rates()
    print(f"\n=== Summary ===")
    print(f"Updated: {len(result['updated'])} currencies")
    print(f"Kept: {len(result['kept'])} currencies (manual or no change)")
    print(f"Errors: {len(result['errors'])}")
    for e in result["errors"]:
        print(f"  {e}")
