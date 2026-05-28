"""
weekly_report.py
週次パフォーマンスレポート（機能⑦）

毎週月曜 JST 9:00 に前週のシグナル精度をDiscordに送信。
last_signals.json の履歴（data/signal_history.jsonl）を蓄積して集計。
"""

import json
import os
import urllib.request
from datetime import datetime, timedelta, timezone


HISTORY_FILE = "data/signal_history.jsonl"
MAX_HISTORY_DAYS = 90  # 90日分を保持


# ============================================================
# 履歴の保存
# ============================================================

def save_signal_snapshot(results: list, sentiment: dict, now: datetime):
    """
    現在のシグナル状態をJSONL形式で履歴ファイルに追記。
    signal_scanner.pyのメイン処理の最後に毎回呼ぶ。
    """
    os.makedirs("data", exist_ok=True)
    snapshot = {
        "timestamp": now.isoformat(),
        "date": now.strftime("%Y-%m-%d"),
        "hour": now.hour,
        "signals": {
            r["pair"]: {
                "stars": r["stars"],
                "direction": r.get("direction", ""),
                "price": r.get("price", 0),
                "ta_score": r.get("ta_score", 0),
                "fa_score": r.get("fa_score", 0),
            }
            for r in results
        },
        "risk_mode": sentiment.get("risk_mode", "unknown") if sentiment else "unknown",
        "vix": sentiment.get("vix") if sentiment else None,
    }

    with open(HISTORY_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps(snapshot, ensure_ascii=False) + "\n")

    # 古い履歴を削除（90日超）
    _prune_history()


def _prune_history():
    """90日より古いスナップショットを削除"""
    if not os.path.exists(HISTORY_FILE):
        return
    cutoff = (datetime.now(timezone.utc) - timedelta(days=MAX_HISTORY_DAYS)).strftime("%Y-%m-%d")
    kept = []
    with open(HISTORY_FILE, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                snap = json.loads(line)
                if snap.get("date", "") >= cutoff:
                    kept.append(line)
            except Exception:
                continue

    with open(HISTORY_FILE, "w", encoding="utf-8") as f:
        f.write("\n".join(kept) + ("\n" if kept else ""))


# ============================================================
# 週次集計
# ============================================================

def load_weekly_snapshots(days_back: int = 7) -> list:
    """過去N日分のスナップショットを読み込む"""
    if not os.path.exists(HISTORY_FILE):
        return []
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days_back)).strftime("%Y-%m-%d")
    snapshots = []
    with open(HISTORY_FILE, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                snap = json.loads(line)
                if snap.get("date", "") >= cutoff:
                    snapshots.append(snap)
            except Exception:
                continue
    return snapshots


def calc_weekly_stats(snapshots: list) -> dict:
    """
    シグナル精度を計算。
    「★4以上のシグナルが出た後、翌日の方向に動いたか」を検証。
    """
    if len(snapshots) < 2:
        return {}

    signal_events = []  # (pair, direction, entry_price, next_price, correct)

    # 各スナップショットで★4以上のシグナルを取得し、次のスナップショットで検証
    for i in range(len(snapshots) - 1):
        snap = snapshots[i]
        next_snap = snapshots[i + 1]

        for pair, sig in snap.get("signals", {}).items():
            if sig.get("stars", 0) < 4:
                continue
            direction = sig.get("direction", "")
            if not direction.endswith(("LONG", "SHORT")):
                continue

            entry_price = sig.get("price", 0)
            next_sig = next_snap.get("signals", {}).get(pair, {})
            next_price = next_sig.get("price", entry_price)

            if entry_price == 0:
                continue

            price_change = next_price - entry_price
            if direction.endswith("LONG"):
                correct = price_change > 0
            else:
                correct = price_change < 0

            pips = abs(price_change)

            signal_events.append({
                "pair": pair,
                "direction": direction,
                "stars": sig.get("stars", 0),
                "entry_price": entry_price,
                "next_price": next_price,
                "pips_change": round(pips, 4),
                "correct": correct,
                "date": snap.get("date", ""),
            })

    if not signal_events:
        return {}

    total = len(signal_events)
    correct_count = sum(1 for e in signal_events if e["correct"])
    win_rate = round(correct_count / total * 100, 1)

    # ペア別集計
    pair_stats = {}
    for ev in signal_events:
        p = ev["pair"]
        if p not in pair_stats:
            pair_stats[p] = {"total": 0, "correct": 0}
        pair_stats[p]["total"] += 1
        if ev["correct"]:
            pair_stats[p]["correct"] += 1

    # 最もシグナルが多かった通貨
    most_active = max(pair_stats.items(), key=lambda x: x[1]["total"]) if pair_stats else None

    # 最大ピップス変動
    max_event = max(signal_events, key=lambda e: e["pips_change"]) if signal_events else None

    return {
        "total_signals": total,
        "correct_signals": correct_count,
        "win_rate": win_rate,
        "pair_stats": pair_stats,
        "most_active_pair": most_active[0] if most_active else "N/A",
        "most_active_count": most_active[1]["total"] if most_active else 0,
        "best_event": max_event,
        "signal_events": signal_events,
    }


# ============================================================
# Discord 週次レポート送信
# ============================================================

def send_weekly_report(webhook_url: str):
    """
    先週のシグナル精度レポートをDiscordに送信。
    毎週月曜に呼ぶ。
    """
    if not webhook_url:
        print("[INFO] Discord webhook not configured, skipping weekly report")
        return False

    snapshots = load_weekly_snapshots(days_back=7)
    if len(snapshots) < 5:
        print(f"[WARN] Insufficient history for weekly report ({len(snapshots)} snapshots)")
        # 履歴不足でも最低限のレポートを送信
        return _send_no_data_report(webhook_url, len(snapshots))

    stats = calc_weekly_stats(snapshots)
    if not stats:
        return _send_no_data_report(webhook_url, len(snapshots))

    now_jst = datetime.now(timezone.utc) + timedelta(hours=9)
    week_start = (now_jst - timedelta(days=7)).strftime("%m/%d")
    week_end = (now_jst - timedelta(days=1)).strftime("%m/%d")

    win_rate = stats.get("win_rate", 0)
    total = stats.get("total_signals", 0)
    correct = stats.get("correct_signals", 0)

    # 勝率に応じた色
    if win_rate >= 65:
        color = 0x4ADE80  # 緑
    elif win_rate >= 50:
        color = 0xD4A574  # ゴールド
    else:
        color = 0xF87171  # 赤

    # ペア別成績（上位5件）
    pair_stats = stats.get("pair_stats", {})
    pair_lines = []
    for pair, ps in sorted(pair_stats.items(),
                           key=lambda x: -(x[1]["correct"] / x[1]["total"] if x[1]["total"] > 0 else 0)):
        rate = round(ps["correct"] / ps["total"] * 100) if ps["total"] > 0 else 0
        mark = "✅" if rate >= 60 else ("△" if rate >= 40 else "❌")
        pair_lines.append(f"{mark} {pair}: {ps['correct']}/{ps['total']}件 ({rate}%)")
        if len(pair_lines) >= 6:
            break

    # 最大変動イベント
    best = stats.get("best_event")
    best_line = ""
    if best:
        mark = "✅" if best["correct"] else "❌"
        best_line = (
            f"{mark} {best['pair']} {best['direction']}\n"
            f"   {best['date']} | 変動: {best['pips_change']:.4f}"
        )

    fields = [
        {
            "name": "📈 シグナル精度",
            "value": (
                f"```\n"
                f"対象期間: {week_start}〜{week_end}\n"
                f"★4以上シグナル: {total}件\n"
                f"方向一致（翌時間足）: {correct}件\n"
                f"勝率: {win_rate}%\n"
                f"```"
            ),
            "inline": False,
        },
        {
            "name": "🏆 通貨ペア別成績",
            "value": "```\n" + "\n".join(pair_lines) + "\n```" if pair_lines else "データなし",
            "inline": False,
        },
    ]

    if best_line:
        fields.append({
            "name": "⚡ 最大変動イベント",
            "value": best_line,
            "inline": False,
        })

    fields.append({
        "name": "📝 注記",
        "value": (
            "勝率は「シグナル発生後の次回スキャン時（〜1時間後）に"
            "方向一致したか」で判定。中長期的な成績とは異なります。"
        ),
        "inline": False,
    })

    embeds = [{
        "title": f"📊 週次シグナルレポート {week_start}〜{week_end}",
        "description": f"先週の★4以上シグナルの方向一致率: **{win_rate}%** ({correct}/{total}件)",
        "color": color,
        "url": "https://applejoker01-afk.github.io/fx-signal-monitor/",
        "timestamp": now_jst.isoformat(),
        "footer": {"text": f"Currents FX Terminal L3 | Weekly Report | {now_jst.strftime('%Y-%m-%d %H:%M JST')}"},
        "fields": fields,
    }]

    return _post_discord(webhook_url, embeds)


def _send_no_data_report(webhook_url: str, snapshot_count: int) -> bool:
    """履歴不足時の簡易レポート"""
    now_jst = datetime.now(timezone.utc) + timedelta(hours=9)
    embeds = [{
        "title": "📊 週次シグナルレポート",
        "description": (
            f"履歴データが蓄積中です。\n"
            f"現在 {snapshot_count} スナップショット保存済み。\n"
            f"来週以降にレポートが生成されます。"
        ),
        "color": 0x94A3B8,
        "timestamp": now_jst.isoformat(),
        "footer": {"text": f"Currents FX Terminal L3 | Weekly Report"},
    }]
    return _post_discord(webhook_url, embeds)


def _post_discord(webhook_url: str, embeds: list) -> bool:
    """Discord Webhook にPOST"""
    webhook_url = webhook_url.replace("discordapp.com", "discord.com")
    try:
        payload = json.dumps({"embeds": embeds}).encode("utf-8")
        req = urllib.request.Request(
            webhook_url, data=payload,
            headers={
                "Content-Type": "application/json",
                "User-Agent": "DiscordBot (fx-signal-monitor, 1.0)",
            },
            method="POST"
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            print(f"[OK] Weekly report sent (HTTP {resp.status})")
            return True
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="ignore")[:200]
        print(f"[ERROR] Weekly report failed: HTTP {e.code} | {body}")
        return False
    except Exception as e:
        print(f"[ERROR] Weekly report failed: {e}")
        return False


# ============================================================
# スタンドアロン実行（GitHub Actions から直接呼べる）
# ============================================================

if __name__ == "__main__":
    import sys
    wh_url = os.environ.get("DISCORD_WEBHOOK_URL", "")
    if not wh_url:
        print("[ERROR] DISCORD_WEBHOOK_URL not set")
        sys.exit(1)
    send_weekly_report(wh_url)
    print("[OK] Done")
