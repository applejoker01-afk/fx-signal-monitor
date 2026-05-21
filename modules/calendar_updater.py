"""
経済指標カレンダーの自動更新モジュール

データソース: Finnhub API (https://finnhub.io/docs/api/economic-calendar)
無料プランで月60リクエスト。週1回取得すれば月4リクエストで済む。

取得後、L3が解釈できるフォーマットに変換して data/economic_calendar.json を更新。
"""

import json
import os
import urllib.request
import urllib.error
from datetime import datetime, timezone, timedelta

CALENDAR_FILE = "data/economic_calendar.json"
USER_AGENT = "fx-signal-monitor/1.0"

# Finnhubのeventタイプ名を、L3 importanceにマッピング
# 重要度3=critical, 2=high, 1=medium
IMPORTANCE_MAP = {3: "critical", 2: "high", 1: "medium", 0: "medium"}

# Finnhubが返すevent名から、importanceを推定するためのキーワード
CRITICAL_KEYWORDS = [
    "fomc", "ecb", "boe", "boc", "rba", "rbnz", "snb", "boj",
    "fed funds", "interest rate decision", "rate decision",
    "non-farm payrolls", "nfp", "cpi", "inflation rate",
    "gdp", "unemployment rate"
]

HIGH_KEYWORDS = [
    "retail sales", "industrial production", "manufacturing pmi",
    "services pmi", "ism", "ppi", "trade balance",
    "consumer confidence", "philly fed", "core cpi"
]

# 通貨コードから影響を受けるペアへのマッピング
CURRENCY_TO_PAIRS = {
    "USD": ["USDJPY", "EURUSD", "GBPUSD", "AUDUSD", "NZDUSD", "USDCAD", "USDCHF"],
    "EUR": ["EURUSD", "EURJPY", "EURGBP", "EURAUD"],
    "JPY": ["USDJPY", "EURJPY", "GBPJPY", "AUDJPY", "NZDJPY", "CADJPY", "CHFJPY",
            "MXNJPY", "TRYJPY", "ZARJPY", "INRJPY", "SGDJPY", "HKDJPY", "CNYJPY"],
    "GBP": ["GBPJPY", "GBPUSD", "EURGBP"],
    "AUD": ["AUDJPY", "AUDUSD", "EURAUD"],
    "NZD": ["NZDJPY", "NZDUSD"],
    "CAD": ["USDCAD", "CADJPY"],
    "CHF": ["USDCHF", "CHFJPY"],
    "MXN": ["MXNJPY"],
    "TRY": ["TRYJPY"],
    "ZAR": ["ZARJPY"],
    "INR": ["INRJPY"],
    "SGD": ["SGDJPY"],
    "HKD": ["HKDJPY"],
    "CNY": ["CNYJPY"],
}


def http_get(url, timeout=20):
    req = urllib.request.Request(
        url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", errors="ignore")


def estimate_importance(event_name, finnhub_impact=None):
    """イベント名と Finnhub impact から importance を決定"""
    name_lower = (event_name or "").lower()

    # Finnhub の impact が信頼できる場合はそれを優先
    if finnhub_impact is not None:
        try:
            imp = int(finnhub_impact)
            if imp >= 3:
                return "critical"
            elif imp == 2:
                # 名前ベースで critical か high か判定
                for kw in CRITICAL_KEYWORDS:
                    if kw in name_lower:
                        return "critical"
                return "high"
            elif imp == 1:
                return "medium"
        except (ValueError, TypeError):
            pass

    # 名前ベース判定
    for kw in CRITICAL_KEYWORDS:
        if kw in name_lower:
            return "critical"
    for kw in HIGH_KEYWORDS:
        if kw in name_lower:
            return "high"
    return "medium"


def map_country_to_currency(country_code):
    """ISO国コード → 通貨コード"""
    mapping = {
        "US": "USD", "EU": "EUR", "DE": "EUR", "FR": "EUR", "IT": "EUR", "ES": "EUR",
        "JP": "JPY", "GB": "GBP", "UK": "GBP", "AU": "AUD", "NZ": "NZD",
        "CA": "CAD", "CH": "CHF", "MX": "MXN", "TR": "TRY", "ZA": "ZAR",
        "IN": "INR", "SG": "SGD", "HK": "HKD", "CN": "CNY",
    }
    return mapping.get(country_code.upper() if country_code else "", None)


def fetch_finnhub_calendar(days_ahead=21):
    """
    Finnhub Economic Calendar APIから経済指標を取得。
    https://finnhub.io/docs/api/economic-calendar
    無料枠: 月60リクエスト
    """
    api_key = os.environ.get("FINNHUB_API_KEY")
    if not api_key:
        print("[INFO] FINNHUB_API_KEY not set, skipping calendar auto-fetch")
        return None

    today = datetime.now(timezone.utc).date()
    end = today + timedelta(days=days_ahead)

    url = (
        "https://finnhub.io/api/v1/calendar/economic"
        f"?from={today}&to={end}&token={api_key}"
    )

    try:
        text = http_get(url, timeout=30)
        data = json.loads(text)
    except urllib.error.HTTPError as e:
        if e.code == 429:
            print("[ERROR] Finnhub rate limit exceeded - try again later")
        else:
            print(f"[ERROR] Finnhub HTTP error: {e.code} {e.reason}")
        return None
    except Exception as e:
        print(f"[ERROR] Finnhub fetch failed: {e}")
        return None

    events = data.get("economicCalendar", [])
    if not events:
        print("[WARN] Finnhub returned empty calendar")
        return None

    converted = []
    for ev in events:
        country = ev.get("country", "")
        currency = map_country_to_currency(country)
        if not currency:
            continue

        affects = CURRENCY_TO_PAIRS.get(currency, [])
        if not affects:
            continue

        event_name = ev.get("event", "").strip()
        if not event_name:
            continue

        # 時刻フォーマット
        date_str = ev.get("time", "")
        if not date_str:
            continue
        # Finnhubは "YYYY-MM-DD HH:MM:SS" 形式（UTC）
        try:
            dt = datetime.strptime(date_str, "%Y-%m-%d %H:%M:%S")
            dt = dt.replace(tzinfo=timezone.utc)
            iso_str = dt.strftime("%Y-%m-%dT%H:%M:%SZ")
        except ValueError:
            # 日付だけの場合
            try:
                dt = datetime.strptime(date_str, "%Y-%m-%d")
                dt = dt.replace(tzinfo=timezone.utc)
                iso_str = dt.strftime("%Y-%m-%dT12:00:00Z")  # 正午UTCで埋める
            except ValueError:
                continue

        importance = estimate_importance(event_name, ev.get("impact"))

        # medium の重要度はスキップ（カレンダーが膨大になるため）
        if importance == "medium":
            continue

        converted.append({
            "date": iso_str,
            "country": country,
            "currency": currency,
            "name": event_name,
            "importance": importance,
            "affects_pairs": affects,
            "source": "finnhub",
            "actual": ev.get("actual"),
            "estimate": ev.get("estimate"),
            "previous": ev.get("prev"),
        })

    print(f"[OK] Finnhub: {len(converted)} relevant events fetched (from {len(events)} total)")
    return converted


def merge_with_manual_events(auto_events, current_events):
    """
    手動で追加されたイベント（source != 'finnhub'）と統合。
    同じ日付・通貨・名前のものは自動取得側を優先。
    """
    seen_keys = set()
    merged = []

    # 自動取得分を先に
    for ev in auto_events:
        key = f"{ev['date']}|{ev['currency']}|{ev['name'].lower()}"
        seen_keys.add(key)
        merged.append(ev)

    # 既存の手動分で重複していないものを追加
    for ev in current_events:
        if ev.get("source") == "finnhub":
            continue  # 自動取得分は捨てる（古いため）
        key = f"{ev.get('date', '')}|{ev.get('currency', '')}|{ev.get('name', '').lower()}"
        if key not in seen_keys:
            merged.append(ev)
            seen_keys.add(key)

    # 日付でソート
    merged.sort(key=lambda x: x.get("date", ""))
    return merged


def update_economic_calendar(dry_run=False):
    """
    Finnhubから取得して、既存JSONを更新する。
    既存の手動イベントは保持。

    Returns:
        {"fetched_count": int, "merged_count": int, "errors": [...]}
    """
    # 既存ファイルを読込
    current_data = {"events": [], "notes": ""}
    if os.path.exists(CALENDAR_FILE):
        try:
            with open(CALENDAR_FILE, "r", encoding="utf-8") as f:
                current_data = json.load(f)
        except Exception as e:
            print(f"[WARN] Could not load existing calendar: {e}")

    # 自動取得
    auto_events = fetch_finnhub_calendar(days_ahead=21)

    if auto_events is None:
        return {"fetched_count": 0, "merged_count": 0,
                "errors": ["Finnhub fetch failed or skipped"]}

    # マージ
    merged = merge_with_manual_events(auto_events, current_data.get("events", []))

    # 過去のイベント（48h以上前）を削除して肥大化を防ぐ
    cutoff = datetime.now(timezone.utc) - timedelta(hours=48)
    fresh = []
    for ev in merged:
        try:
            dt_str = ev["date"]
            if dt_str.endswith("Z"):
                dt_str = dt_str[:-1] + "+00:00"
            ev_dt = datetime.fromisoformat(dt_str)
            if ev_dt >= cutoff:
                fresh.append(ev)
        except Exception:
            fresh.append(ev)  # parseに失敗したものは安全側で残す

    new_data = {
        "last_updated": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "last_auto_run": datetime.now(timezone.utc).isoformat(),
        "notes": current_data.get("notes", "自動取得+手動編集の統合カレンダー"),
        "importance_guide": {
            "critical": "中央銀行会合・雇用統計・CPI（48h前から取引控え）",
            "high": "GDP・PMI・小売売上・要人発言（24h前から警戒）",
            "medium": "二次指標（自動取得では除外）",
        },
        "data_source": "Finnhub API (auto) + 手動編集",
        "events": fresh,
    }

    if not dry_run:
        os.makedirs(os.path.dirname(CALENDAR_FILE), exist_ok=True)
        with open(CALENDAR_FILE, "w", encoding="utf-8") as f:
            json.dump(new_data, f, ensure_ascii=False, indent=2)
        print(f"[OK] {CALENDAR_FILE} updated. {len(fresh)} events stored.")

    return {
        "fetched_count": len(auto_events),
        "merged_count": len(fresh),
        "errors": [],
    }


if __name__ == "__main__":
    result = update_economic_calendar()
    print(f"\n=== Summary ===")
    print(f"Fetched: {result['fetched_count']} events from Finnhub")
    print(f"Total in calendar: {result['merged_count']}")
    print(f"Errors: {len(result['errors'])}")
