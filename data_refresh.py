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
from modules.cb_rate_scraper import update_central_bank_rates
from modules.calendar_updater import update_economic_calendar


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

    # === 1. 中央銀行金利の更新 ===
    print("\n--- Phase 1: Central Bank Rates ---")
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

    # === 2. 経済指標カレンダーの更新 ===
    print("\n--- Phase 2: Economic Calendar ---")
    try:
        cal_result = update_economic_calendar()
        summary["calendar"] = cal_result
        print(f"  Fetched: {cal_result['fetched_count']} events")
        print(f"  Total:   {cal_result['merged_count']} in calendar")
        if cal_result.get("errors"):
            for e in cal_result["errors"]:
                print(f"  Warning: {e}")
    except Exception as e:
        print(f"[ERROR] Calendar update failed: {e}")
        traceback.print_exc()
        summary["errors"].append(f"calendar: {e}")

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
