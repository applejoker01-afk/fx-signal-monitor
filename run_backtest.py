#!/usr/bin/env python3
"""
run_backtest.py
バックテストを実行してDiscordにレポート送信するスタンドアロンスクリプト。

GitHub Actions（手動 or 月次）から実行。
signal_scanner.py のシグナル判定ロジックを再利用する。
"""

import os
import sys
import json
import urllib.request
from datetime import datetime, timedelta, timezone

# signal_scanner.py から判定ロジックと取得関数を借用
from signal_scanner import (
    compute_ta_score, fetch_history, PAIR_API,
    fetch_latest_rates,
)
from modules.rate_fetcher import load_central_bank_rates, compute_fa_score
from modules.backtest import run_full_backtest

PAGES_URL = "https://applejoker01-afk.github.io/fx-signal-monitor/"


def main():
    print("=" * 60)
    print(f"FX Backtest - {datetime.now(timezone.utc).isoformat()}")
    print("=" * 60)

    lookback = int(os.environ.get("BACKTEST_DAYS", "180"))

    cb_rates = load_central_bank_rates()

    # 全ペアの履歴取得
    print(f"[INFO] Fetching {len(PAIR_API)} pairs history...")
    all_histories = {}
    for pair in PAIR_API:
        prices = fetch_history(pair, 280)
        if prices and len(prices) >= 60:
            all_histories[pair] = prices
            print(f"  {pair}: {len(prices)} days")

    # バックテスト実行
    print(f"\n[INFO] Running backtest (lookback={lookback}days)...")
    bt = run_full_backtest(
        all_histories, compute_ta_score, compute_fa_score,
        cb_rates, PAIR_API, lookback_days=lookback
    )

    overall = bt["overall"]
    if not overall:
        print("[WARN] No backtest results")
        return

    print(f"\n=== バックテスト結果 ===")
    print(f"総トレード: {overall['total']}件")
    print(f"勝率: {overall['win_rate']}%")
    print(f"プロフィットファクター: {overall['profit_factor']}")
    print(f"TP1/TP2/TP3/SL: {overall['tp1_hits']}/{overall['tp2_hits']}/{overall['tp3_hits']}/{overall['sl_hits']}")

    # Discord送信
    webhook = os.environ.get("DISCORD_WEBHOOK_URL", "")
    if webhook:
        send_backtest_report(webhook, bt, lookback)

    # AI総括（オプション）
    if os.environ.get("ANTHROPIC_API_KEY"):
        try:
            from modules.ai_commentary import generate_weekly_summary
            # バックテストをstats形式に変換してAI総括
            pseudo_stats = {
                "total_trades": overall["total"],
                "wins": overall["wins"],
                "losses": overall["losses"],
                "win_rate": overall["win_rate"],
                "avg_hold_hours": 0,
                "reason_counts": {
                    "TP1_HIT": overall["tp1_hits"],
                    "TP2_HIT": overall["tp2_hits"],
                    "TP3_HIT": overall["tp3_hits"],
                    "SL_HIT": overall["sl_hits"],
                },
                "pair_stats": {
                    r["pair"]: {
                        "wins": r["wins"], "total": r["total"],
                        "total_pips": r["total_pips"]
                    }
                    for r in bt["by_pair"].values()
                },
            }
            print("[AI] バックテストAI総括生成中...")
            # （送信は省略、ログのみ）
        except Exception as e:
            print(f"[WARN] AI summary skipped: {e}")

    print("\n[OK] Done")


def send_backtest_report(webhook_url, bt, lookback):
    webhook_url = webhook_url.replace("discordapp.com", "discord.com")
    overall = bt["overall"]

    win_rate = overall["win_rate"]
    color = 0x4ADE80 if win_rate >= 55 else (0xD4A574 if win_rate >= 45 else 0xF87171)

    best_lines = "\n".join(
        f"✅ {b['pair']}: {b['win_rate']}% ({b['total']}件)"
        for b in bt["best_pairs"]
    )
    worst_lines = "\n".join(
        f"❌ {w['pair']}: {w['win_rate']}% ({w['total']}件)"
        for w in bt["worst_pairs"]
    )

    fields = [
        {
            "name": "📊 全体成績",
            "value": (
                f"```\n"
                f"総トレード: {overall['total']}件\n"
                f"勝率: {win_rate}%\n"
                f"勝ち: {overall['wins']} / 負け: {overall['losses']}\n"
                f"プロフィットファクター: {overall['profit_factor']}\n"
                f"累計pips: {overall['total_pips']:+.2f}\n"
                f"```"
            ),
            "inline": False,
        },
        {
            "name": "🎯 決済内訳",
            "value": (
                f"```\n"
                f"TP1到達: {overall['tp1_hits']}件\n"
                f"TP2到達: {overall['tp2_hits']}件\n"
                f"TP3到達: {overall['tp3_hits']}件\n"
                f"SL到達: {overall['sl_hits']}件\n"
                f"```"
            ),
            "inline": False,
        },
        {
            "name": "🏆 好成績ペア",
            "value": "```\n" + best_lines + "\n```" if best_lines else "なし",
            "inline": True,
        },
        {
            "name": "⚠ 不振ペア",
            "value": "```\n" + worst_lines + "\n```" if worst_lines else "なし",
            "inline": True,
        },
        {
            "name": "🔗 ダッシュボード",
            "value": f"**[📊 L3 ダッシュボードを開く]({PAGES_URL})**",
            "inline": False,
        },
    ]

    now_jst = datetime.now(timezone.utc) + timedelta(hours=9)
    embeds = [{
        "title": f"🔬 バックテスト結果（過去{lookback}日）",
        "description": f"テクノファンダメンタル戦略の過去検証: 勝率**{win_rate}%** / PF**{overall['profit_factor']}**",
        "color": color,
        "url": PAGES_URL,
        "timestamp": now_jst.isoformat(),
        "footer": {"text": f"Currents FX Terminal L3 | Backtest | {now_jst.strftime('%Y-%m-%d %H:%M JST')}"},
        "fields": fields,
    }]

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
            print(f"[OK] Backtest report sent (HTTP {resp.status})")
    except Exception as e:
        print(f"[ERROR] Backtest report failed: {e}")


if __name__ == "__main__":
    main()
