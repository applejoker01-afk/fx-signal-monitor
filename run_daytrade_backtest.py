"""
デイトレ反発ロジック検証ランナー

Yahoo Financeから15分足を取得し、複数ペアで4方式を比較。
GitHub Actionsから実行する。結果はdocs/daytrade_backtest.jsonに保存。
"""

import json
import time
import urllib.request
from datetime import datetime, timezone

from modules.daytrade_backtest import run_strategy_comparison

# 検証対象（デイトレ監視ペア）
PAIRS = ["USDJPY", "EURUSD", "GBPJPY", "AUDJPY", "EURJPY", "AUDUSD"]


def fetch_15m(pair, days=60):
    """Yahoo Financeから15分足を取得（最大60日）"""
    symbol = pair + "=X"
    # Yahooは15分足を最大60日提供
    url = (f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
           f"?interval=15m&range={days}d")
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode())
        result = data["chart"]["result"][0]
        q = result["indicators"]["quote"][0]
        highs, lows, closes, opens = [], [], [], []
        for i in range(len(q["close"])):
            if (q["close"][i] is None or q["high"][i] is None
                    or q["low"][i] is None):
                continue
            highs.append(q["high"][i])
            lows.append(q["low"][i])
            closes.append(q["close"][i])
            opens.append(q["open"][i] if q["open"][i] is not None else q["close"][i])
        return {"highs": highs, "lows": lows, "closes": closes, "opens": opens}
    except Exception as e:
        print(f"[WARN] {pair} 取得失敗: {e}")
        return None


def main():
    print("=" * 64)
    print("デイトレ戦略比較: 反発(逆張り) vs 三次元共鳴(順張り) RR1:2")
    print("=" * 64)

    all_results = {}
    strategies = ["反発(improveB)", "反発の逆(reverse)", "三次元共鳴(順張り)"]
    aggregate = {s: {"trades": 0, "wins": 0, "losses": 0, "total_pips": 0.0}
                 for s in strategies}

    for pair in PAIRS:
        print(f"\n--- {pair} ---")
        data = fetch_15m(pair)
        if not data or len(data["closes"]) < 250:
            print(f"  データ不足のためスキップ（{len(data['closes']) if data else 0}本）")
            continue
        print(f"  {len(data['closes'])}本の15分足を取得")

        summaries = run_strategy_comparison(data, pair, rr=2.0)
        if not summaries:
            continue
        all_results[pair] = summaries

        for s, v in summaries.items():
            if v.get("trades", 0) == 0:
                print(f"  {s:20}: トレードなし")
                continue
            print(f"  {s:20}: {v['trades']}件 勝率{v['win_rate']}% "
                  f"PF{v['pf']} 期待値{v['expectancy']}pips/件 "
                  f"累計{v['total_pips']}pips DD{v['max_dd']}")
            if s in aggregate:
                aggregate[s]["trades"] += v["trades"]
                aggregate[s]["wins"] += v["wins"]
                aggregate[s]["losses"] += v["losses"]
                aggregate[s]["total_pips"] += v["total_pips"]

        time.sleep(1)

    print("\n" + "=" * 64)
    print("【全ペア合算】戦略比較（RR1:2）")
    print("=" * 64)
    agg_summary = {}
    for s, a in aggregate.items():
        if a["trades"] == 0:
            continue
        win_rate = a["wins"] / a["trades"] * 100
        avg_pips = a["total_pips"] / a["trades"]
        agg_summary[s] = {
            "trades": a["trades"], "win_rate": round(win_rate, 1),
            "total_pips": round(a["total_pips"], 1),
            "avg_pips_per_trade": round(avg_pips, 2),
        }
        print(f"  {s:20}: {a['trades']}件 勝率{win_rate:.1f}% "
              f"1件平均{avg_pips:+.2f}pips 累計{a['total_pips']:+.1f}pips")

    if agg_summary:
        best = max(agg_summary.items(), key=lambda kv: kv[1]["avg_pips_per_trade"])
        print(f"\n  → 期待値が高い戦略: 【{best[0]}】"
              f"（1件平均{best[1]['avg_pips_per_trade']:+.2f}pips）")
        if best[1]["avg_pips_per_trade"] > 0:
            print("  ✅ プラスの期待値が出る戦略が見つかりました")
        else:
            print("  ⚠ どちらも期待値マイナス。15分足デイトレ自体の見直しが必要")

    output = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "test_type": "strategy_comparison",
        "rr": 2.0,
        "pairs": all_results,
        "aggregate": agg_summary,
        "best_strategy": max(agg_summary.items(),
                             key=lambda kv: kv[1]["avg_pips_per_trade"])[0]
        if agg_summary else None,
    }
    with open("docs/daytrade_backtest.json", "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)
    print("\n結果を docs/daytrade_backtest.json に保存しました")


if __name__ == "__main__":
    main()
