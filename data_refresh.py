#!/usr/bin/env python3
"""
週次データ自動更新スクリプト
GitHub Actions から呼び出される。

実行内容:
  1. 中央銀行金利を自動取得（USD/EUR/GBP/AUD）
  2. 経済指標カレンダーをFinnhubから取得
  3. 結果を git commit & push（変更があれば）

未取得通貨の金利、手動追加されたイベントはそのまま保持される。
"""

import sys
import json
import os
import traceback
from datetime import datetime, timezone

# モジュール読込
from modules.cb_rate_scraper import update_central_bank_rates, sync_next_meetings_from_calendar
from modules.calendar_updater import update_economic_calendar


def export_daytrade_calendar():
    """Publish only actionable event data to GitHub Pages for the M15 monitor."""
    source_path = "data/economic_calendar.json"
    target_path = "docs/daytrade_calendar.json"
    if not os.path.exists(source_path):
        return {"exported": 0, "reason": "calendar missing"}
    with open(source_path, "r", encoding="utf-8") as handle:
        source = json.load(handle)
    now = datetime.now(timezone.utc)
    events = []
    for event in source.get("events", []):
        if event.get("importance") not in {"critical", "high"}:
            continue
        try:
            event_time = datetime.fromisoformat(event["date"].replace("Z", "+00:00"))
        except (KeyError, ValueError):
            continue
        # Keep a short past window so the browser can enforce post-news blocks.
        if -1 <= (event_time - now).total_seconds() / 3600 <= 168:
            events.append(event)
    os.makedirs("docs", exist_ok=True)
    with open(target_path, "w", encoding="utf-8") as handle:
        json.dump({
            "generated_at": now.isoformat(),
            "source_last_updated": source.get("last_updated"),
            "events": events,
        }, handle, ensure_ascii=False, indent=2)
    return {"exported": len(events)}


def main():
    print("=" * 64)
    print(f"Data Auto-Refresh - {datetime.now(timezone.utc).isoformat()}")
    print("=" * 64)

    summary = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "cb_rates": None,
        "calendar": None,
        "errors": [],
    }

    # === 1. 経済指標カレンダーの更新 ===
    # 次回の政策会合を中央銀行データへ同期するため、先にカレンダーを更新する。
    print("\n--- Phase 1: Economic Calendar ---")
    try:
        cal_result = update_economic_calendar()
        summary["calendar"] = cal_result
        print(f"  Fetched: {cal_result['fetched_count']} events")
        print(f"  Total:   {cal_result['merged_count']} in calendar")
        if cal_result.get("errors"):
            for e in cal_result["errors"]:
                print(f"    {e}")
    except Exception as e:
        print(f"[ERROR] Calendar update failed: {e}")
        traceback.print_exc()
        summary["errors"].append(f"calendar: {e}")

    # The day-trade page is static GitHub Pages, so it needs a small public
    # calendar export rather than access to the repository's data directory.
    try:
        summary["daytrade_calendar"] = export_daytrade_calendar()
        print(f"  Daytrade events: {summary['daytrade_calendar']['exported']}")
    except Exception as e:
        print(f"[ERROR] Daytrade calendar export failed: {e}")
        summary["errors"].append(f"daytrade_calendar: {e}")

    # === 2. 中央銀行金利の更新 ===
    print("\n--- Phase 2: Central Bank Rates ---")
    try:
        cb_result = update_central_bank_rates()
        summary["cb_rates"] = {
            "updated": len(cb_result["updated"]),
            "kept": len(cb_result["kept"]),
            "errors": cb_result["errors"],
            "changes": cb_result["updated"],
        }
        print(f"  Updated: {len(cb_result['updated'])} currencies")
        print(f"  Kept:    {len(cb_result['kept'])} currencies")
        if cb_result["errors"]:
            print(f"  Errors:  {len(cb_result['errors'])}")
            for e in cb_result["errors"]:
                print(f"    {e}")
    except Exception as e:
        print(f"[ERROR] CB rates update failed: {e}")
        traceback.print_exc()
        summary["errors"].append(f"cb_rates: {e}")

    # === 3. 次回の政策会合をカレンダーから同期 ===
    print("\n--- Phase 3: Central Bank Meeting Schedule ---")
    try:
        meeting_result = sync_next_meetings_from_calendar()
        summary["meeting_schedule"] = meeting_result
        print(f"  Scheduled: {len(meeting_result['updated'])} changed")
        print(f"  Expired:   {len(meeting_result['expired'])} cleared")
    except Exception as e:
        print(f"[ERROR] Meeting schedule sync failed: {e}")
        traceback.print_exc()
        summary["errors"].append(f"meeting_schedule: {e}")

    # === 3. サマリーを保存 ===
    os.makedirs("data", exist_ok=True)
    with open("data/auto_refresh_log.json", "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2, default=str)

    print("\n" + "=" * 64)
    print("Done.")
    print("=" * 64)

    return 0 if not summary["errors"] else 1


if __name__ == "__main__":
    sys.exit(main())
