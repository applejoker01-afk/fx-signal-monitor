"""
weekly_stats_history.py
週次成績の恒久蓄積レイヤー

背景: data/closed_trades.jsonl は個別トレードの生ログだが MAX_CLOSED_DAYS(120日)で
古い順に削除される。weekly_report.py は毎週その週の集計をDiscordに送るだけで
どこにも保存していないため、週をまたいだ勝率・TP到達率・ペア別成績の推移を
後から追えなかった。本モジュールは週次スナップショット（集計値のみ、生トレード
は含まない）を data/weekly_stats_history.jsonl に追記し、120日プルーンの影響を
受けない長期トレンド分析を可能にする。

record_current_week()  : weekly_report.py から呼ばれ、直近7日分のスナップショットを1行追記
backfill_from_closed_trades() : 既存の closed_trades.jsonl から遡って週単位に
                                 バケット分けし、欠けている週のスナップショットを埋める
"""

import json
import os
from datetime import datetime, timedelta, timezone

from modules.trade_tracker import load_closed_trades, calc_stats_from_trades


HISTORY_FILE = "data/weekly_stats_history.jsonl"


def _slim(stats: dict) -> dict:
    """best_trade/worst_trade の全フィールドは持たず、傾向分析に要る値だけ残す"""
    if not stats:
        return {}
    slim_pair_stats = {
        pair: {"total": ps["total"], "wins": ps["wins"], "total_pips": round(ps["total_pips"], 3)}
        for pair, ps in stats.get("pair_stats", {}).items()
    }
    best = stats.get("best_trade") or {}
    worst = stats.get("worst_trade") or {}
    return {
        "total_trades": stats.get("total_trades", 0),
        "wins": stats.get("wins", 0),
        "losses": stats.get("losses", 0),
        "evens": stats.get("evens", 0),
        "win_rate": stats.get("win_rate", 0),
        "avg_hold_hours": stats.get("avg_hold_hours", 0),
        "reason_counts": stats.get("reason_counts", {}),
        "pair_stats": slim_pair_stats,
        "best_pips": best.get("pips"),
        "worst_pips": worst.get("pips"),
    }


def _load_existing_weeks() -> set:
    if not os.path.exists(HISTORY_FILE):
        return set()
    weeks = set()
    with open(HISTORY_FILE, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                weeks.add(json.loads(line)["week_start"])
            except Exception:
                continue
    return weeks


def _append(record: dict):
    os.makedirs(os.path.dirname(HISTORY_FILE), exist_ok=True)
    with open(HISTORY_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def record_current_week(stats: dict, week_start: str, week_end: str):
    """
    weekly_report.send_weekly_report() から呼ぶ。同じ week_start が既に
    記録済みならスキップ（手動再実行での二重記録を防ぐ）。
    """
    if not stats:
        return
    if week_start in _load_existing_weeks():
        return
    record = {
        "week_start": week_start,
        "week_end": week_end,
        "recorded_at": datetime.now(timezone.utc).isoformat(),
        **_slim(stats),
    }
    _append(record)


def backfill_from_closed_trades():
    """
    closed_trades.jsonl 全件を exit_date で週バケット（月曜始まり）に分け、
    まだ history に無い週だけ計算して追記する。1回実行すれば十分な冪等処理。
    """
    all_trades = load_closed_trades(days_back=None)
    if not all_trades:
        print("[INFO] closed_trades.jsonl が空、backfillするデータなし")
        return

    def _monday_of(date_str: str) -> datetime:
        d = datetime.fromisoformat(date_str.replace("Z", "+00:00")) if "T" in date_str else datetime.fromisoformat(date_str)
        d = d.replace(tzinfo=None)
        return d - timedelta(days=d.weekday())

    buckets = {}
    for t in all_trades:
        exit_date = t.get("exit_date") or (t.get("exit_time", "")[:10])
        if not exit_date:
            continue
        try:
            monday = _monday_of(exit_date)
        except Exception:
            continue
        key = monday.strftime("%Y-%m-%d")
        buckets.setdefault(key, []).append(t)

    existing = _load_existing_weeks()
    added = 0
    for week_start, trades in sorted(buckets.items()):
        if week_start in existing:
            continue
        week_end_dt = datetime.strptime(week_start, "%Y-%m-%d") + timedelta(days=6)
        stats = calc_stats_from_trades(trades)
        record = {
            "week_start": week_start,
            "week_end": week_end_dt.strftime("%Y-%m-%d"),
            "recorded_at": datetime.now(timezone.utc).isoformat(),
            "backfilled": True,
            **_slim(stats),
        }
        _append(record)
        added += 1

    print(f"[OK] backfill完了: {added}週分を追記（対象トレード{len(all_trades)}件、{len(buckets)}週分バケット）")


if __name__ == "__main__":
    backfill_from_closed_trades()
