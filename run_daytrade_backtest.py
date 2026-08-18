"""Run the v2 cost-aware M15 paper-monitor evaluation."""

import json
from datetime import datetime, timezone

from generate_strategy_map import PAIRS, fetch_15m
from modules.daytrade_v2 import evaluate_pair


def main():
    results = {}
    for pair in PAIRS:
        print(f"--- {pair} ---")
        data = fetch_15m(pair)
        if not data or len(data["closes"]) < 300:
            results[pair] = {"error": "insufficient data"}
            continue
        results[pair] = evaluate_pair(data, pair)
        for key, item in results[pair]["strategies"].items():
            metric = item["metrics"]
            oos = item["oos_metrics"]
            print(f"  {key}: n={metric['trades']} PF={metric['net_pf']} "
                  f"exp={metric['net_expectancy_pips']}p, "
                  f"OOS exp={oos['net_expectancy_pips']}p, eligible={item['eligible']}")
    output = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "engine_version": "daytrade-v2",
        "execution_mode": "paper_only",
        "results": results,
    }
    with open("docs/daytrade_backtest.json", "w", encoding="utf-8") as handle:
        json.dump(output, handle, ensure_ascii=False, indent=2)


if __name__ == "__main__":
    main()
