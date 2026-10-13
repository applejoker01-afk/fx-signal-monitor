"""
中央銀行政策金利の自動取得モジュール

データソース（優先順位順）:
  USD: FRED API (DFEDTARU) - APIキー必要
  EUR: ECB公式SDMX - キー不要
  GBP: BOE公式IADB - キー不要
  AUD: RBA F1.1 CSV → 失敗時 BIS
  JPY/NZD/CAD/CHF/NOK/SEK/HKD/CNY/MXN/TRY/ZAR/INR/BRL/PLN/KRW: BIS WS_CBPOL（2026-10-13〜）
  SGD ほか上記以外: 既存JSONを保持（手動メンテ）

取得失敗時、または取得値の観測日が人による検証日以前の場合は既存値を保持する。
"""

import json
import os
import re
import urllib.request
import urllib.error
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

CB_RATES_FILE = "data/central_bank_rates.json"
CALENDAR_FILE = "data/economic_calendar.json"
USER_AGENT = "fx-signal-monitor/1.0"

# 経済カレンダー上で政策決定会合を識別するための語。議事要旨・講演は除く。
MEETING_EVENT_TERMS = (
    "interest rate decision",
    "rate decision",
    "monetary policy decision",
    "cash rate target",
)
NON_DECISION_EVENT_TERMS = ("minutes", "speech", "testimony", "report")


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

    # 2026-10-13: FEDFUNDS（実効金利・月次平均）→ DFEDTARU（誘導目標上限・日次）に変更。
    # FEDFUNDSは月次平均＋公表遅延のため、2026-09-16のFOMC利上げ(→3.75-4.00%)が
    # 4週間経っても JSON に反映されず 3.75% のまま、last_auto_update も 09-01 で止まり、
    # 「利上げ後にstanceが見直されていない」ことを検知できなかった。
    # rate_fetcher.FRED_RATE_SERIES（スキャン時の上書き）も DFEDTARU なので揃える。
    url = (
        "https://api.stlouisfed.org/fred/series/observations"
        f"?series_id=DFEDTARU&api_key={api_key}&file_type=json"
        "&sort_order=desc&limit=5"
    )
    try:
        data = json.loads(http_get(url))
        # 日次系列は休日等で value="." の欠損行があるため数値の最新行を使う
        for latest in data.get("observations", []):
            try:
                rate = float(latest["value"])
            except (TypeError, ValueError):
                continue
            return {
                "rate": rate,
                "date": latest["date"],
                "source": "FRED DFEDTARU",
            }
        return None
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

RBA_F11_URL = "https://www.rba.gov.au/statistics/tables/csv/f1.1-data.csv"
_RBA_DATE_FORMATS = ("%d-%b-%Y", "%Y-%m-%d", "%d/%m/%Y")


def _parse_rba_date(s):
    for fmt in _RBA_DATE_FORMATS:
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def parse_rba_f11_csv(text):
    """RBA F1.1 CSV（先頭列=日付、2列目=Cash Rate Target）から最新の値を返す。
    F1.1 はメタデータ行（Title/Description/Series ID 等）が先頭に並ぶため、
    「1列目が日付・2列目が数値」の行だけを拾う。"""
    for line in reversed(text.strip().splitlines()):
        parts = [p.strip().strip('"') for p in line.split(",")]
        if len(parts) < 2 or not parts[0]:
            continue
        d = _parse_rba_date(parts[0])
        if d is None:
            continue
        try:
            rate = float(parts[1])
        except ValueError:
            continue
        return {"rate": rate, "date": d.isoformat(), "source": "RBA F1.1"}
    return None


def fetch_aud_rba_rate():
    """
    Reserve Bank of Australia の Cash Rate Target を取得。

    2026-10-13修正: 旧URL(statistics/cash-rate/cash-rate-data.csv)が404になり、
    少なくとも 2026-09 以降 AUD は 4.35% のまま更新が止まっていた
    （実際は 2026-09-29 に 4.60% へ利上げ）。失敗はログに [WARN] が出るだけで
    誰も気づかなかった。
      1) RBA F1.1 統計表 CSV（一次ソース・最速）
      2) BIS 中銀政策金利 API（RBA がbot遮断した場合のフォールバック。
         1〜3週間の公表遅延があるため、手動検証より古い値は update 側で採用しない）
    """
    try:
        res = parse_rba_f11_csv(http_get(RBA_F11_URL, timeout=15))
        if res:
            return res
        print("[WARN] RBA F1.1: 数値行を解析できず（フォーマット変更の可能性）")
    except Exception as e:
        print(f"[WARN] RBA fetch failed: {e} → BISへフォールバック")
    return fetch_bis_policy_rate("AUD")


# ===========================================================================
# BIS 中銀政策金利（WS_CBPOL）— 手動メンテだった通貨の自動化（2026-10-13追加）
# ===========================================================================
# 背景: JPY/NZD/CAD/CHF/NOK/SEK/EM通貨は「手動メンテ」で、実際には数ヶ月放置され
# CAD 2.75%(正2.25) / MXN 9.0%(正6.5) / TRY 46%(正37) など大きく乖離していた。
# BIS は各国中銀の政策金利を日次系列で一括公開しており、キー不要・bot遮断なし。
# ただし公表は1〜3週間遅れるため、会合直後は手動検証値の方が新しいことがある。
# → update_central_bank_rates() で「観測日 <= 手動検証日」の値では上書きしない。
# USD(FRED DFEDTARU)・EUR(ECB MRO)・GBP(BOE) は既存の一次ソースの方が速く、
# BIS の定義（US=レンジ中央値、XM=預金ファシリティ金利）とも異なるので対象外。
BIS_CBPOL_URL = "https://stats.bis.org/api/v1/data/WS_CBPOL/D.{areas}?lastNObservations=1&format=csv"
BIS_AREA = {
    "AUD": "AU", "JPY": "JP", "NZD": "NZ", "CAD": "CA", "CHF": "CH",
    "NOK": "NO", "SEK": "SE", "HKD": "HK", "CNY": "CN", "MXN": "MX",
    "TRY": "TR", "ZAR": "ZA", "INR": "IN", "BRL": "BR", "PLN": "PL", "KRW": "KR",
}
_bis_cache = {}


def parse_bis_cbpol_csv(text):
    """BIS WS_CBPOL CSV → {REF_AREA: {"rate", "date"}}（エリアごとに最新観測日）"""
    import csv
    import io
    out = {}
    for row in csv.DictReader(io.StringIO(text)):
        area, d, v = row.get("REF_AREA"), row.get("TIME_PERIOD"), row.get("OBS_VALUE")
        if not area or not d or v in (None, "", "NaN"):
            continue
        try:
            rate = float(v)
        except ValueError:
            continue
        if area not in out or d > out[area]["date"]:
            out[area] = {"rate": rate, "date": d}
    return out


def _fetch_bis_all():
    if "data" not in _bis_cache:
        url = BIS_CBPOL_URL.format(areas="+".join(sorted(set(BIS_AREA.values()))))
        try:
            _bis_cache["data"] = parse_bis_cbpol_csv(http_get(url, timeout=40))
        except Exception as e:
            print(f"[WARN] BIS policy rate fetch failed: {e}")
            _bis_cache["data"] = {}
    return _bis_cache["data"]


def fetch_bis_policy_rate(ccy):
    obs = _fetch_bis_all().get(BIS_AREA.get(ccy, ""))
    if not obs:
        return None
    return {"rate": obs["rate"], "date": obs["date"], "source": "BIS WS_CBPOL"}


# ===========================================================================
# 統合更新ロジック
# ===========================================================================

# 各通貨ごとの取得関数マッピング
FETCH_FUNCTIONS = {
    "USD": fetch_usd_fed_funds,
    "EUR": fetch_eur_ecb_rate,
    "GBP": fetch_gbp_boe_rate,
    "AUD": fetch_aud_rba_rate,
    **{ccy: (lambda c=ccy: fetch_bis_policy_rate(c))
       for ccy in BIS_AREA if ccy != "AUD"},
}


def _parse_datetime(value):
    """ISO日付または日時をUTCのdatetimeに変換する。失敗時はNone。"""
    if not value:
        return None
    try:
        text = str(value)
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        dt = datetime.fromisoformat(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except (TypeError, ValueError):
        return None


def _is_policy_meeting_event(event, known_currencies):
    """カレンダーイベントが通貨別の政策決定会合かを安全側で判定する。"""
    ccy = event.get("currency")
    if ccy not in known_currencies:
        return False
    name = str(event.get("name", "")).lower()
    if any(term in name for term in NON_DECISION_EVENT_TERMS):
        return False
    return any(term in name for term in MEETING_EVENT_TERMS)


def reconcile_next_meetings(rates_section, events, now=None):
    """カレンダーの将来イベントを次回会合へ反映し、過去日付を無効化する。

    カレンダーに無い将来日を推測で埋めない。期限切れの予定を残して会合前後の
    ブラックアウトを誤発火させるより、未取得として明示する方が安全である。
    """
    now = now or datetime.now(timezone.utc)
    today = now.astimezone(timezone.utc).date()
    future = {}

    for event in events or []:
        if not _is_policy_meeting_event(event, rates_section):
            continue
        event_dt = _parse_datetime(event.get("date"))
        if not event_dt or event_dt.date() < today:
            continue
        ccy = event["currency"]
        current = future.get(ccy)
        if current is None or event_dt < current[0]:
            future[ccy] = (event_dt, event.get("source", "calendar"))

    updated = []
    expired = []
    for ccy, info in rates_section.items():
        scheduled = future.get(ccy)
        previous = info.get("next_meeting")
        if scheduled:
            next_date = scheduled[0].date().isoformat()
            if previous != next_date:
                updated.append({"currency": ccy, "old": previous, "new": next_date})
            info["next_meeting"] = next_date
            info["next_meeting_source"] = scheduled[1]
            info["next_meeting_status"] = "scheduled"
            continue

        previous_dt = _parse_datetime(previous)
        if previous_dt and previous_dt.date() < today:
            info["next_meeting"] = None
            info["next_meeting_source"] = "unavailable"
            info["next_meeting_status"] = "expired"
            expired.append({"currency": ccy, "old": previous})

    return {"updated": updated, "expired": expired}


def sync_next_meetings_from_calendar(dry_run=False, now=None):
    """
    中央銀行JSONの次回会合を同期する。

    2026-08-25変更: 従来はForexFactory経由の汎用カレンダー（キーワード一致頼み）
    だけを見ていたが、中央銀行会合は最重要イベントなので主要中銀の公式サイト
    （modules/cb_meeting_scraper.py: FRB/BOE/BOJ/ECB）から直接取得した日程を
    優先的に混ぜる。reconcile_next_meetings側は「同じ通貨で複数候補があれば
    最も早い未来日を採用」する仕組みなので、直接取得分と汎用カレンダー分の
    どちらか一方が欠けても他方でカバーできる（どちらも失敗した通貨だけ
    "unavailable"のまま）。
    """
    if not os.path.exists(CB_RATES_FILE):
        return {"updated": [], "expired": [], "errors": ["rates file not found"]}

    with open(CB_RATES_FILE, "r", encoding="utf-8") as f:
        current = json.load(f)

    calendar_events = []
    calendar_source = "calendar"
    if os.path.exists(CALENDAR_FILE):
        with open(CALENDAR_FILE, "r", encoding="utf-8") as f:
            calendar = json.load(f)
        calendar_events = calendar.get("events", [])
        calendar_source = calendar.get("last_auto_source", "calendar")

    direct_events = []
    errors = []
    try:
        from modules.cb_meeting_scraper import fetch_all_meeting_events
        direct_events = fetch_all_meeting_events()
    except Exception as e:
        errors.append(f"direct central bank scrape failed: {e}")

    result = reconcile_next_meetings(
        current.get("rates", {}), direct_events + calendar_events, now=now,
    )
    current["last_meeting_schedule_sync"] = (now or datetime.now(timezone.utc)).isoformat()
    current["meeting_schedule_source"] = (
        f"cb_meeting_scraper({len(direct_events)}) + {calendar_source}({len(calendar_events)})"
    )

    if not dry_run:
        with open(CB_RATES_FILE, "w", encoding="utf-8") as f:
            json.dump(current, f, ensure_ascii=False, indent=2)

    result["errors"] = errors
    return result


def update_central_bank_rates(dry_run=False):
    """
    既存JSONを読み込み、自動取得可能な通貨だけ更新する。
    stance（tighten/ease等）は自動更新しない。金利が変わった通貨は
    rate_fetcher.check_central_bank_data_staleness が stance 見直しを促す。

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

        if ccy not in rates_section:
            kept.append(ccy)
            continue
        new_rate = round(result["rate"], 4)
        old_rate = rates_section[ccy].get("rate")
        changed = old_rate is None or abs(new_rate - old_rate) >= 0.001

        # 公表遅延のあるソース（BIS等）が、人の検証より古い観測値で上書きするのを防ぐ。
        # 例: 2026-10-13 時点で BIS の AUD は 09-24 観測の 4.35%、手動検証(10-13)は 4.60%。
        verified = (rates_section[ccy].get("stance_verified")
                    or current.get("stance_last_verified") or "")[:10]
        obs_date = str(result.get("date") or "")[:10]
        if changed and verified and obs_date and obs_date <= verified:
            kept.append(ccy)
            print(f"  - {ccy}: {result.get('source')} {new_rate}% (観測{obs_date}) は"
                  f"手動検証({verified})の {old_rate}% より古いため不採用")
            continue

        rates_section[ccy]["last_auto_check"] = datetime.now(timezone.utc).isoformat()
        rates_section[ccy]["auto_source"] = result.get("source", "unknown")

        if changed:
            rates_section[ccy]["rate"] = new_rate
            rates_section[ccy]["last_auto_update"] = result.get("date") or datetime.now(timezone.utc).isoformat()[:10]
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
    # 2026-10-13: last_updated は書き換えない。自動取得はrateしか更新しないのに
    # 毎日 last_updated を当日にしていたため、stanceの陳腐化検知
    # （rate_fetcher.check_central_bank_data_staleness）が永久に発火しなかった。
    # 自動実行の記録は last_auto_run、人によるstance検証は stance_last_verified。
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
