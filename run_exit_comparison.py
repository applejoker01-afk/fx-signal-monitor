#!/usr/bin/env python3
"""
run_exit_comparison.py
OCO vs トレール vs ハイブリッド の決済方式比較を実行し、
結果をDiscordに報告する。

GitHub Actions（手動）から実行。
"""

import os
import json
import urllib.request
from datetime import datetime, timedelta, timezone

from signal_scanner import compute_ta_score, fetch_history, PAIR_API
from modules.rate_fetcher import load_central_bank_rates, compute_fa_score
from modules.exit_strategy_backtest import compare_exit_strategies, pick_best_method

PAGES_URL = "https://applejoker01-afk.github.io/fx-signal-monitor/"

METHOD_LABEL = {
    "OCO": "OCO固定（TP/SL両端固定）",
    "TRAIL": "トレーリングストップ",
    "HYBRID": "ハイブリッド（半利確→トレール）",
}


def main():
    print("=" * 60)
    print("決済方式比較バックテスト")
    print("=" * 60)

    lookback = int(os.environ.get("BACKTEST_DAYS", "200"))

    cb_rates = load_central_bank_rates()

    print(f"[INFO] 全{len(PAIR_API)}ペアの履歴取得中...")
    all_histories = {}
    for pair in PAIR_API:
        prices = fetch_history(pair, 280)
        if prices and len(prices) >= 60:
            all_histories[pair] = prices
            print(f"  {pair}: {len(prices)}日")

    print(f"\n[INFO] 3方式を比較中（lookback={lookback}日）...")
    results = compare_exit_strategies(
        all_histories, compute_ta_score, compute_fa_score,
        cb_rates, PAIR_API, lookback_days=lookback
    )

    # コンソール出力
    print("\n" + "=" * 60)
    print(f"{'方式':<12}{'勝率':>7}{'PF':>7}{'期待値':>9}{'最大DD':>9}{'件数':>6}")
    print("-" * 60)
    for name in ["OCO", "TRAIL", "HYBRID"]:
        r = results.get(name, {})
        if r.get("total", 0) > 0:
            print(f"{name:<12}{r['win_rate']:>6}%{r['profit_factor']:>7}"
                  f"{r['expectancy']:>9}{r['max_drawdown']:>9}{r['total']:>6}")

    best = pick_best_method(results)
    if best:
        print(f"\n→ 最良方式: {best['method']} "
              f"(期待値{best['stats']['expectancy']}, PF{best['stats']['profit_factor']})")

    # Discord報告
    webhook = os.environ.get("DISCORD_WEBHOOK_URL", "")
    if webhook:
        send_report(webhook, results, best, lookback)

    print("\n[OK] Done")


def send_report(webhook, results, best, lookback):
    webhook = webhook.replace("discordapp.com", "discord.com")

    # 比較テーブル（コードブロックで整形）
    lines = [f"{'方式':<8}{'勝率':>6}{'PF':>6}{'期待値':>8}{'DD':>8}"]
    lines.append("-" * 38)
    for name in ["OCO", "TRAIL", "HYBRID"]:
        r = results.get(name, {})
        if r.get("total", 0) > 0:
            lines.append(
                f"{name:<8}{r['win_rate']:>5}%{r['profit_factor']:>6}"
                f"{r['expectancy']:>8}{r['max_drawdown']:>8}"
            )

    fields = [{
        "name": "📊 3方式の比較結果",
        "value": "```\n" + "\n".join(lines) + "\n```",
        "inline": False,
    }]

    # 各方式の詳細
    for name in ["OCO", "TRAIL", "HYBRID"]:
        r = results.get(name, {})
        if r.get("total", 0) > 0:
            fields.append({
                "name": f"{'🏆 ' if best and best['method']==name else ''}{METHOD_LABEL[name]}",
                "value": (
                    f"勝率{r['win_rate']}% / {r['wins']}勝{r['losses']}敗 / 計{r['total']}件\n"
                    f"PF: {r['profit_factor']} / 期待値: {r['expectancy']}\n"
                    f"平均利益: {r['avg_win']} / 平均損失: {r['avg_loss']}\n"
                    f"最大DD: {r['max_drawdown']}"
                ),
                "inline": False,
            })

    if best:
        fields.append({
            "name": "💡 結論",
            "value": (
                f"**{METHOD_LABEL[best['method']]}** が期待値最良。\n"
                f"15分足デイトレ（daytrade.html）の決済ロジックに採用を推奨。\n"
                f"※ これは日足検証。15分足は値動きの性質が異なるため、"
                f"daytrade.htmlで実データ蓄積後の再検証も推奨。"
            ),
            "inline": False,
        })

    now_jst = datetime.now(timezone.utc) + timedelta(hours=9)
    embeds = [{
        "title": f"🔬 決済方式 比較検証（日足{lookback}日）",
        "description": "OCO・トレール・ハイブリッドのどれが期待値で優れるかを検証しました。",
        "color": 0xD4A574,
        "url": PAGES_URL,
        "timestamp": now_jst.isoformat(),
        "footer": {"text": f"Currents FX | Exit Strategy Test | {now_jst.strftime('%Y-%m-%d %H:%M JST')}"},
        "fields": fields,
    }]

    try:
        payload = json.dumps({"embeds": embeds}).encode("utf-8")
        req = urllib.request.Request(
            webhook, data=payload,
            headers={"Content-Type": "application/json",
                     "User-Agent": "DiscordBot (fx-signal-monitor, 1.0)"},
            method="POST")
        with urllib.request.urlopen(req, timeout=15) as resp:
            print(f"[OK] Discord report sent (HTTP {resp.status})")
    except Exception as e:
        print(f"[ERROR] Discord report: {e}")


if __name__ == "__main__":
    main()
