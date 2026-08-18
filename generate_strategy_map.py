"""Generate the deterministic, cost-aware M15 paper-monitor map.

Yahoo's 15-minute endpoint is limited to a short history.  The output therefore
never enables live-trade alerts by itself; it only decides which of the two
predefined research setups is worth displaying as a paper candidate.
"""

import json
import time
import urllib.request
from datetime import datetime, timezone

from modules.daytrade_v2 import evaluate_pair


PAIRS = ["USDJPY", "EURUSD", "GBPJPY", "AUDJPY", "EURJPY", "AUDUSD",
         "GBPUSD", "USDCAD", "USDCHF", "NZDJPY"]
LABELS = {
    "trend_breakout": "トレンド継続ブレイク",
    "range_reversion": "レンジ反転",
}


def fetch_15m(pair: str, days: int = 60):
    url = (f"https://query1.finance.yahoo.com/v8/finance/chart/{pair}=X"
           f"?interval=15m&range={days}d")
    request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        with urllib.request.urlopen(request, timeout=8) as response:
            payload = json.loads(response.read().decode("utf-8"))
        result = payload["chart"]["result"][0]
        quote = result["indicators"]["quote"][0]
        timestamps = result.get("timestamp", [])
        data = {"opens": [], "highs": [], "lows": [], "closes": [], "timestamps": []}
        for index, close in enumerate(quote["close"]):
            high, low = quote["high"][index], quote["low"][index]
            if close is None or high is None or low is None:
                continue
            data["opens"].append(quote["open"][index] if quote["open"][index] is not None else close)
            data["highs"].append(high)
            data["lows"].append(low)
            data["closes"].append(close)
            data["timestamps"].append(timestamps[index] if index < len(timestamps) else None)
        return data
    except Exception as error:
        print(f"[WARN] {pair}: fetch failed: {error}")
        return None


def choose_candidate(report: dict):
    candidates = [
        (key, value) for key, value in report["strategies"].items()
        if value["eligible"]
    ]
    if not candidates:
        return None, None
    return max(candidates, key=lambda item: (
        item[1]["oos_metrics"]["net_expectancy_pips"],
        item[1]["metrics"]["net_pf"],
    ))


def main():
    strategy_map = {}
    for pair in PAIRS:
        print(f"--- {pair} ---")
        data = fetch_15m(pair)
        if not data or len(data["closes"]) < 300:
            strategy_map[pair] = {
                "status": "hold", "strategy_key": None, "strategy": "検証データ不足",
                "reason": "15分足の有効データが不足しています", "alert_eligible": False,
            }
            continue
        report = evaluate_pair(data, pair)
        key, candidate = choose_candidate(report)
        if candidate is None:
            strategy_map[pair] = {
                "status": "hold", "strategy_key": None, "strategy": "条件未達（監視停止）",
                "reason": "コスト控除後・最終1/3の検証条件を満たす手法がありません",
                "validation": report, "alert_eligible": False,
            }
        else:
            strategy_map[pair] = {
                "status": "paper", "strategy_key": key, "strategy": LABELS[key],
                "reason": "コスト控除後の全期間・最終1/3で暫定条件を通過。長期データ検証前のペーパー監視です",
                "validation": report, "metrics": candidate["metrics"],
                "oos_metrics": candidate["oos_metrics"], "alert_eligible": False,
            }
        time.sleep(0.5)

    output = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "engine_version": "daytrade-v2",
        "execution_mode": "paper_only",
        "lookback_days": 60,
        "methodology": [
            "Two mutually exclusive strategies: trend continuation breakout or range reversion.",
            "Entry is next-bar open, with configured round-trip costs and pessimistic intrabar stop priority.",
            "The final one-third is held out for a basic out-of-sample check.",
            "This short sample can reject weak rules but cannot validate a durable live-trading edge.",
        ],
        "strategy_map": strategy_map,
    }
    with open("docs/strategy_map.json", "w", encoding="utf-8") as handle:
        json.dump(output, handle, ensure_ascii=False, indent=2)
    print("Wrote docs/strategy_map.json")


if __name__ == "__main__":
    main()
