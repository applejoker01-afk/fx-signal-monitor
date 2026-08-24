"""
経済指標カレンダーの自動更新モジュール

データソース: Finnhub API (https://finnhub.io/docs/api/economic-calendar)
無料プランで月60リクエスト。週1回取得すれば月4リクエストで済む。

取得後、L3が解釈できるフォーマットに変換して data/economic_calendar.json を更新。
"""

import json
import os
import time
import urllib.request
import urllib.error
import xml.etree.ElementTree as ET
from datetime import datetime, timezone, timedelta

CALENDAR_FILE = "data/economic_calendar.json"
CALENDAR_HISTORY_FILE = "data/economic_calendar_history.jsonl"
USER_AGENT = "fx-signal-monitor/1.0"

# 2026-08-18追加: FinnhubのEconomic Calendar APIが無料枠で403 Forbiddenを返す
# ようになり、2026-07-12週以降5週間サイレントに更新が止まっていたことが判明
# （ユーザー報告→L3ダッシュボードに鮮度バッジを追加して発覚）。APIキー不要の
# ForexFactory公開フィードをフォールバック先として追加する。
# フィードの時刻はUTC基準（Unemployment Claimsの発表時刻を米東部標準の
# 8:30am(夏時間)と突き合わせてUTC 12:30pm一致を確認済み）。
FOREXFACTORY_CALENDAR_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.xml"

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

# 2026-08-25判明: 通貨→影響ペアの対応表を手動列挙していたため、7/20のSBI取扱拡張
# (GBPAUD/GBPCHF/AUDCHF/EURCHF/AUDNZD/EURNZD/USDCNY等)の追加がここに反映されず、
# それらのペアはイベント前後の自動見送りフィルタが一切効かない状態になっていた。
# signal_scanner.PAIR_API と同一の全ペア一覧をここでも保持し（クロスインポートは
# calendar_updater→signal_scannerの重い依存を避けるため見送り、他モジュール同様に
# 独立コピーを持つ既存の設計方針に合わせる）、対応表はそこから機械的に生成することで
# 「新ペア追加時にここを更新し忘れる」というクラスのバグを構造的に防ぐ。
# 新ペアを追加する場合は signal_scanner.PAIR_API とここの両方を更新すること。
_ALL_PAIRS = [
    ("USD", "JPY"), ("EUR", "JPY"), ("GBP", "JPY"), ("AUD", "JPY"),
    ("NZD", "JPY"), ("CAD", "JPY"), ("CHF", "JPY"), ("SGD", "JPY"),
    ("HKD", "JPY"), ("CNY", "JPY"), ("MXN", "JPY"), ("TRY", "JPY"),
    ("ZAR", "JPY"), ("INR", "JPY"),
    ("EUR", "USD"), ("GBP", "USD"), ("AUD", "USD"), ("NZD", "USD"),
    ("USD", "CAD"), ("USD", "CHF"), ("EUR", "GBP"), ("EUR", "AUD"),
    ("SEK", "JPY"), ("NOK", "JPY"), ("BRL", "JPY"), ("PLN", "JPY"),
    ("KRW", "JPY"),
    ("GBP", "AUD"), ("GBP", "CHF"), ("AUD", "CHF"), ("EUR", "CHF"),
    ("AUD", "NZD"), ("EUR", "NZD"), ("USD", "CNY"),
]


def _build_currency_to_pairs(pairs):
    mapping = {}
    for frm, to in pairs:
        pair_name = frm + to
        mapping.setdefault(frm, []).append(pair_name)
        mapping.setdefault(to, []).append(pair_name)
    return mapping


# 通貨コードから影響を受けるペアへのマッピング（_ALL_PAIRSから機械的に生成）
CURRENCY_TO_PAIRS = _build_currency_to_pairs(_ALL_PAIRS)


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


def _parse_ff_datetime(date_str, time_str):
    """ForexFactoryの 'MM-DD-YYYY' + '10:30pm'/'All Day'/'Tentative' をUTC datetimeへ。"""
    try:
        base = datetime.strptime(date_str.strip(), "%m-%d-%Y")
    except (ValueError, AttributeError):
        return None
    t = (time_str or "").strip()
    if not t or t.lower() in ("all day", "tentative"):
        dt = base.replace(hour=12, minute=0)  # 精度なし・正午UTCで埋める
    else:
        try:
            parsed_time = datetime.strptime(t, "%I:%M%p")
            dt = base.replace(hour=parsed_time.hour, minute=parsed_time.minute)
        except ValueError:
            dt = base.replace(hour=12, minute=0)
    return dt.replace(tzinfo=timezone.utc)


def fetch_forexfactory_calendar():
    """
    ForexFactory公開カレンダーフィード（APIキー不要）から今週の高重要度イベントを取得。
    Finnhub Economic Calendar APIが使えない場合のフォールバック。
    """
    try:
        text = http_get(FOREXFACTORY_CALENDAR_URL, timeout=20)
    except Exception as e:
        print(f"[ERROR] ForexFactory fetch failed: {e}")
        return None

    try:
        root = ET.fromstring(text)
    except ET.ParseError as e:
        print(f"[ERROR] ForexFactory XML parse failed: {e}")
        return None

    converted = []
    for ev in root.findall(".//event"):
        impact = (ev.findtext("impact") or "").strip()
        if impact != "High":
            continue  # Medium/Lowはカレンダー肥大化防止のため除外(Finnhub側と同方針)

        currency = (ev.findtext("country") or "").strip()  # FFのcountryは実質通貨コード
        affects = CURRENCY_TO_PAIRS.get(currency, [])
        if not affects:
            continue

        name = (ev.findtext("title") or "").strip()
        if not name:
            continue

        dt = _parse_ff_datetime(ev.findtext("date") or "", ev.findtext("time") or "")
        if dt is None:
            continue

        # FFは3段階(High/Medium/Low)しかなくFinnhubのcritical相当がないため、
        # 名前ベースのキーワード判定でcritical/highを振り分ける（finnhub_impact=2扱い）。
        importance = estimate_importance(name, finnhub_impact=2)

        converted.append({
            "date": dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "country": currency,
            "currency": currency,
            "name": name,
            "importance": importance,
            "affects_pairs": affects,
            "source": "forexfactory",
            "actual": None,
            "estimate": (ev.findtext("forecast") or "").strip() or None,
            "previous": (ev.findtext("previous") or "").strip() or None,
        })

    print(f"[OK] ForexFactory: {len(converted)} high-impact events fetched")
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
        if ev.get("source") in ("finnhub", "forexfactory"):
            continue  # 自動取得分は捨てる（古いため）
        key = f"{ev.get('date', '')}|{ev.get('currency', '')}|{ev.get('name', '').lower()}"
        if key not in seen_keys:
            merged.append(ev)
            seen_keys.add(key)

    # 日付でソート
    merged.sort(key=lambda x: x.get("date", ""))
    return merged


def _load_archived_event_keys(sample_lines=500):
    """直近の履歴ファイル末尾を読んで重複キーを把握する（全件読み込みは避ける）。"""
    if not os.path.exists(CALENDAR_HISTORY_FILE):
        return set()
    keys = set()
    try:
        with open(CALENDAR_HISTORY_FILE, "r", encoding="utf-8") as f:
            lines = f.readlines()[-sample_lines:]
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
                keys.add(f"{ev.get('date','')}|{ev.get('currency','')}|{ev.get('name','').lower()}")
            except Exception:
                continue
    except Exception as e:
        print(f"[WARN] Could not read {CALENDAR_HISTORY_FILE}: {e}")
    return keys


def _archive_expiring_events(events):
    """48h経過で消える前のイベントを恒久履歴ファイルへ追記する（重複はスキップ）。"""
    seen = _load_archived_event_keys()
    new_lines = []
    for ev in events:
        key = f"{ev.get('date','')}|{ev.get('currency','')}|{ev.get('name','').lower()}"
        if key in seen:
            continue
        seen.add(key)
        new_lines.append(json.dumps(ev, ensure_ascii=False))
    if not new_lines:
        return
    os.makedirs(os.path.dirname(CALENDAR_HISTORY_FILE), exist_ok=True)
    with open(CALENDAR_HISTORY_FILE, "a", encoding="utf-8") as f:
        f.write("\n".join(new_lines) + "\n")
    print(f"[OK] Archived {len(new_lines)} expiring events -> {CALENDAR_HISTORY_FILE}")


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

    # 自動取得（Finnhub優先、失敗時はForexFactoryへフォールバック）
    errors = []
    auto_events = fetch_finnhub_calendar(days_ahead=21)
    source_used = "finnhub"
    if auto_events is None:
        errors.append("Finnhub fetch failed or skipped")
        print("[INFO] Finnhub unavailable, falling back to ForexFactory")
        auto_events = fetch_forexfactory_calendar()
        source_used = "forexfactory"

    if auto_events is None:
        errors.append("ForexFactory fetch also failed")
        return {"fetched_count": 0, "merged_count": 0, "errors": errors}

    # 2026-08-25判明: ForexFactoryの"thisweek"フィードは週替わり直後の瞬間に
    # まだその週のイベントが公開されておらず0件で返ることがあり、これを
    # 「正常に取得できたが今週は0件」と区別できなかったため、有効な既存データ
    # （未来のイベント）を空リストで上書きしてしまうバグがあった
    # （2026-08-23の週次実行がまさにこれで発生し、2日間イベントフィルタが
    # 実質機能停止していた）。0件で返ってきた場合は少し待って1回だけ再取得を
    # 試み、それでも0件なら「取得失敗」と同様に既存データを保持して上書きしない。
    if source_used == "forexfactory" and len(auto_events) == 0:
        print("[WARN] ForexFactory returned 0 events, retrying once after a short delay "
              "(likely mid-rollover of the weekly feed)")
        time.sleep(5)
        retry_events = fetch_forexfactory_calendar()
        if retry_events:
            auto_events = retry_events
        else:
            existing_future = [
                ev for ev in current_data.get("events", [])
                if ev.get("source") in ("finnhub", "forexfactory")
            ]
            errors.append(
                f"ForexFactory returned 0 events twice; keeping {len(existing_future)} "
                "existing auto-fetched events instead of overwriting with empty data"
            )
            print(f"[WARN] {errors[-1]}")
            return {
                "fetched_count": 0,
                "merged_count": len(current_data.get("events", [])),
                "source": source_used,
                "errors": errors,
                "skipped_overwrite": True,
            }

    # マージ
    merged = merge_with_manual_events(auto_events, current_data.get("events", []))

    # 過去のイベント（48h以上前）を削除して肥大化を防ぐ
    # 2026-08-25追加: 削除する前に data/economic_calendar_history.jsonl へ
    # 恒久保存する。従来は48h経過で消えるだけで、run_pair_reverification.py等の
    # バックテストでイベント回避の効果を検証しようにも過去のイベント日程が
    # 一切残っていなかった（weekly_stats_history.jsonlと同じ理由の欠落）。
    cutoff = datetime.now(timezone.utc) - timedelta(hours=48)
    fresh = []
    expiring = []
    for ev in merged:
        try:
            dt_str = ev["date"]
            if dt_str.endswith("Z"):
                dt_str = dt_str[:-1] + "+00:00"
            ev_dt = datetime.fromisoformat(dt_str)
            if ev_dt >= cutoff:
                fresh.append(ev)
            else:
                expiring.append(ev)
        except Exception:
            fresh.append(ev)  # parseに失敗したものは安全側で残す

    if expiring and not dry_run:
        _archive_expiring_events(expiring)

    new_data = {
        "last_updated": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "last_auto_run": datetime.now(timezone.utc).isoformat(),
        "last_auto_source": source_used,
        "notes": current_data.get("notes", "自動取得+手動編集の統合カレンダー"),
        "importance_guide": {
            "critical": "中央銀行会合・雇用統計・CPI（48h前から取引控え）",
            "high": "GDP・PMI・小売売上・要人発言（24h前から警戒）",
            "medium": "二次指標（自動取得では除外）",
        },
        "data_source": ("Finnhub API (auto) + 手動編集" if source_used == "finnhub"
                         else "ForexFactory (auto, Finnhub 403のためフォールバック中) + 手動編集"),
        "events": fresh,
    }

    if not dry_run:
        os.makedirs(os.path.dirname(CALENDAR_FILE), exist_ok=True)
        with open(CALENDAR_FILE, "w", encoding="utf-8") as f:
            json.dump(new_data, f, ensure_ascii=False, indent=2)
        print(f"[OK] {CALENDAR_FILE} updated via {source_used}. {len(fresh)} events stored.")

    return {
        "fetched_count": len(auto_events),
        "merged_count": len(fresh),
        "source": source_used,
        "errors": errors,
    }


if __name__ == "__main__":
    result = update_economic_calendar()
    print(f"\n=== Summary ===")
    print(f"Fetched: {result['fetched_count']} events from Finnhub")
    print(f"Total in calendar: {result['merged_count']}")
    print(f"Errors: {len(result['errors'])}")
