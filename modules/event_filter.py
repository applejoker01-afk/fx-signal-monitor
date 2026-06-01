"""
経済指標カレンダーに基づくイベント近接判定。
重要イベント前後は新規シグナルを抑制する。

【緩和版】
  従来は critical を前48h〜後12h 見送りにしていたため、
  重要指標が1つあるだけで全通貨が長時間ブロックされていた。
  理論文書の「発表前後30分は取引停止」に準拠し、
  発表直前の短時間のみ見送る方式に変更。
"""

import json
import os
from datetime import datetime, timezone

CALENDAR_FILE = "data/economic_calendar.json"

# ─── 見送り・警告の時間設定（時間単位）───
# critical: 発表直前2時間〜発表後30分のみブロック
CRITICAL_BLOCK_BEFORE = 2.0    # 発表前 何時間からブロックするか
CRITICAL_BLOCK_AFTER = 0.5     # 発表後 何時間までブロックするか
# high: 発表直前2時間〜発表後30分のみ警告（ブロックはしない）
HIGH_WARN_BEFORE = 2.0
HIGH_WARN_AFTER = 0.5


def load_economic_calendar():
    """経済指標カレンダーJSON読込"""
    if not os.path.exists(CALENDAR_FILE):
        print(f"[WARN] {CALENDAR_FILE} not found")
        return []
    try:
        with open(CALENDAR_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data.get("events", [])
    except Exception as e:
        print(f"[WARN] Calendar load failed: {e}")
        return []


def parse_event_time(date_str):
    """ISO 8601文字列をtimezone-aware datetimeに変換"""
    try:
        if date_str.endswith("Z"):
            date_str = date_str[:-1] + "+00:00"
        dt = datetime.fromisoformat(date_str)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception as e:
        print(f"[WARN] Failed to parse date {date_str}: {e}")
        return None


def check_event_proximity(pair, now=None):
    """
    指定ペアの直近イベントを判定。
    返り値:
      {"status": "block"/"warn"/"ok", "reason": str, "event": dict, "hours_until": float}

    【緩和版の挙動】
      critical: 発表前2h〜発表後0.5h のみ block（取引控え）
      high:     発表前2h〜発表後0.5h のみ warn（警告のみ・降格しない）
      それ以外の時間帯・重要度は ok（通常通り）
    """
    if now is None:
        now = datetime.now(timezone.utc)

    events = load_economic_calendar()
    relevant = []

    for event in events:
        if pair not in event.get("affects_pairs", []):
            continue
        event_time = parse_event_time(event["date"])
        if event_time is None:
            continue
        hours_until = (event_time - now).total_seconds() / 3600
        relevant.append((hours_until, event))

    # 最も近接するイベントから判定
    relevant.sort(key=lambda x: abs(x[0]))

    for hours_until, event in relevant:
        imp = event.get("importance", "medium")

        if imp == "critical":
            # 発表前2時間〜発表後30分のみブロック
            if 0 < hours_until <= CRITICAL_BLOCK_BEFORE:
                return {
                    "status": "block",
                    "reason": f"重要イベント直前 ({event['name']} まで {hours_until:.1f}h)",
                    "event": event,
                    "hours_until": hours_until,
                }
            elif -CRITICAL_BLOCK_AFTER < hours_until <= 0:
                return {
                    "status": "block",
                    "reason": f"重要イベント直後 ({event['name']} から {-hours_until:.1f}h経過)",
                    "event": event,
                    "hours_until": hours_until,
                }
        elif imp == "high":
            # high は警告のみ（ブロックせず・短時間）
            if 0 < hours_until <= HIGH_WARN_BEFORE:
                return {
                    "status": "warn",
                    "reason": f"指標発表前警戒 ({event['name']} まで {hours_until:.1f}h)",
                    "event": event,
                    "hours_until": hours_until,
                }
            elif -HIGH_WARN_AFTER < hours_until <= 0:
                return {
                    "status": "warn",
                    "reason": f"指標発表直後 ({event['name']} から {-hours_until:.1f}h経過)",
                    "event": event,
                    "hours_until": hours_until,
                }

    return {"status": "ok", "reason": None, "event": None, "hours_until": None}


def upcoming_events_for(pair, hours_ahead=168):
    """指定ペアの今後168時間（7日）のイベント一覧"""
    now = datetime.now(timezone.utc)
    events = load_economic_calendar()
    result = []
    for event in events:
        if pair not in event.get("affects_pairs", []):
            continue
        event_time = parse_event_time(event["date"])
        if event_time is None:
            continue
        hours_until = (event_time - now).total_seconds() / 3600
        if 0 < hours_until <= hours_ahead:
            result.append({
                "name": event["name"],
                "currency": event["currency"],
                "importance": event["importance"],
                "hours_until": round(hours_until, 1),
                "date": event["date"],
            })
    result.sort(key=lambda x: x["hours_until"])
    return result
