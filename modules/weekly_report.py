"""
weekly_report.py
週次パフォーマンスレポート（機能⑦・トレードライフサイクル版）

毎週月曜 JST 9:00 に前週の「決済済みトレード」の成績をDiscordに送信。
trade_tracker.py が管理する closed_trades.jsonl から集計するため、
同一通貨ペアの重複カウント問題が解消されている。

1シグナル = 1トレード として正確に集計。
"""

import json
import os
import urllib.request
from datetime import datetime, timedelta, timezone

from modules.trade_tracker import calc_trade_stats, load_open_trades


PAGES_URL = "https://applejoker01-afk.github.io/fx-signal-monitor/"

REASON_LABELS = {
    "TP3_HIT": "TP3到達（大勝ち）",
    "TP2_HIT": "TP2到達（勝ち）",
    "TP1_HIT": "TP1到達（小勝ち）",
    "SL_HIT": "SL到達（負け）",
    "SIGNAL_LOST": "シグナル消滅",
    "REVERSED": "方向反転",
}


def send_weekly_report(webhook_url: str):
    if not webhook_url:
        print("[INFO] Discord webhook not configured, skipping weekly report")
        return False

    stats = calc_trade_stats(days_back=7)
    open_trades = load_open_trades()

    if not stats or stats.get("total_trades", 0) == 0:
        return _send_no_data_report(webhook_url, len(open_trades))

    now_jst = datetime.now(timezone.utc) + timedelta(hours=9)
    week_start = (now_jst - timedelta(days=7)).strftime("%m/%d")
    week_end = (now_jst - timedelta(days=1)).strftime("%m/%d")

    total = stats["total_trades"]
    wins = stats["wins"]
    losses = stats["losses"]
    win_rate = stats["win_rate"]
    avg_hold = stats["avg_hold_hours"]

    if win_rate >= 60:
        color = 0x4ADE80
    elif win_rate >= 45:
        color = 0xD4A574
    else:
        color = 0xF87171

    reason_counts = stats.get("reason_counts", {})
    reason_lines = []
    for reason in ["TP3_HIT", "TP2_HIT", "TP1_HIT", "SL_HIT", "SIGNAL_LOST", "REVERSED"]:
        cnt = reason_counts.get(reason, 0)
        if cnt > 0:
            label = REASON_LABELS.get(reason, reason)
            reason_lines.append(f"{label}: {cnt}件")

    pair_stats = stats.get("pair_stats", {})
    pair_lines = []
    for pair, ps in sorted(pair_stats.items(),
                           key=lambda x: -(x[1]["wins"] / x[1]["total"] if x[1]["total"] else 0)):
        rate = round(ps["wins"] / ps["total"] * 100) if ps["total"] else 0
        mark = "✅" if rate >= 60 else ("△" if rate >= 40 else "❌")
        pips = ps["total_pips"]
        pair_lines.append(f"{mark} {pair}: {ps['wins']}/{ps['total']}勝 ({rate}%) 累計{pips:+.3f}")
        if len(pair_lines) >= 6:
            break

    best = stats.get("best_trade")
    worst = stats.get("worst_trade")
    highlight_lines = []
    if best and best.get("pips", 0) > 0:
        highlight_lines.append(
            f"🏆 最大利益: {best['pair']} {best['direction']} "
            f"{best.get('pips',0):+.3f} ({REASON_LABELS.get(best.get('exit_reason'),'')})"
        )
    if worst and worst.get("pips", 0) < 0:
        highlight_lines.append(
            f"📉 最大損失: {worst['pair']} {worst['direction']} "
            f"{worst.get('pips',0):+.3f} ({REASON_LABELS.get(worst.get('exit_reason'),'')})"
        )

    fields = [
        {
            "name": "📊 トレード成績（決済済み）",
            "value": (
                f"```\n"
                f"対象期間: {week_start}〜{week_end}\n"
                f"決済トレード数: {total}件\n"
                f"勝ち: {wins}件 / 負け: {losses}件\n"
                f"勝率: {win_rate}%\n"
                f"平均保有時間: {avg_hold}時間\n"
                f"```"
            ),
            "inline": False,
        },
    ]

    if reason_lines:
        fields.append({
            "name": "🎯 決済理由の内訳",
            "value": "```\n" + "\n".join(reason_lines) + "\n```",
            "inline": False,
        })

    if pair_lines:
        fields.append({
            "name": "🏅 通貨ペア別成績",
            "value": "```\n" + "\n".join(pair_lines) + "\n```",
            "inline": False,
        })

    if highlight_lines:
        fields.append({
            "name": "⚡ ハイライト",
            "value": "\n".join(highlight_lines),
            "inline": False,
        })

    # 2026-07-24: {pair: [trade,...]}のピラミッディング対応
    flat_open = [
        (pair, t) for pair, trades in open_trades.items()
        for t in (trades if isinstance(trades, list) else [trades])
    ]
    if flat_open:
        open_lines = []
        for pair, t in flat_open[:8]:
            seq = t.get("pyramid_seq")
            pair_label = f"{pair}#{seq}" if seq and seq > 1 else pair
            open_lines.append(f"{pair_label} {t['direction']} @ {t['entry_price']}")
        fields.append({
            "name": f"📌 現在保有中（{len(flat_open)}件）",
            "value": "```\n" + "\n".join(open_lines) + "\n```",
            "inline": False,
        })

    fields.append({
        "name": "📝 注記",
        "value": (
            "1シグナル=1トレードとして集計。★4到達でエントリー、"
            "TP/SL到達・シグナル消滅・方向反転で決済。同一ペアは"
            "最大2ポジションまでピラミッディング可能（2026-07-24〜、"
            "既存ポジションがTP到達済み＝トレーリング中の時のみ追加）。"
        ),
        "inline": False,
    })

    # ⑰ AI週次総括（ANTHROPIC_API_KEY設定時のみ）
    try:
        from modules.ai_commentary import generate_weekly_summary
        ai_summary = generate_weekly_summary(stats)
        if ai_summary:
            fields.insert(0, {
                "name": "🤖 AI週次総括",
                "value": ai_summary[:1024],
                "inline": False,
            })
    except Exception as e:
        print(f"[WARN] AI weekly summary skipped: {e}")

    embeds = [{
        "title": f"📊 週次トレードレポート {week_start}〜{week_end}",
        "description": f"先週の決済トレード勝率: **{win_rate}%** ({wins}勝{losses}敗 / 計{total}件)",
        "color": color,
        "url": PAGES_URL,
        "timestamp": now_jst.isoformat(),
        "footer": {"text": f"Currents FX Terminal L3 | Weekly Report | {now_jst.strftime('%Y-%m-%d %H:%M JST')}"},
        "fields": fields,
    }]

    return _post_discord(webhook_url, embeds)


def _send_no_data_report(webhook_url: str, open_count: int) -> bool:
    now_jst = datetime.now(timezone.utc) + timedelta(hours=9)
    embeds = [{
        "title": "📊 週次トレードレポート",
        "description": (
            f"先週の決済済みトレードはまだありません。\n"
            f"現在保有中のトレード: {open_count}件\n\n"
            f"★4シグナルがTP/SLに到達するか、シグナルが消滅すると"
            f"決済としてカウントされ、来週以降のレポートに反映されます。"
        ),
        "color": 0x94A3B8,
        "url": PAGES_URL,
        "timestamp": now_jst.isoformat(),
        "footer": {"text": "Currents FX Terminal L3 | Weekly Report"},
    }]
    return _post_discord(webhook_url, embeds)


def _post_discord(webhook_url: str, embeds: list) -> bool:
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


if __name__ == "__main__":
    import sys
    wh_url = os.environ.get("DISCORD_WEBHOOK_URL", "")
    if not wh_url:
        print("[ERROR] DISCORD_WEBHOOK_URL not set")
        sys.exit(1)
    send_weekly_report(wh_url)
    print("[OK] Done")
