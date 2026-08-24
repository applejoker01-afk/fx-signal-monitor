#!/usr/bin/env python3
"""
run_cost_adjusted_backtest.py
2026-08-25追加（最優先タスク）:
  全バックテストを「正しいpip換算 → JPY損益 or R倍数」に統一し、
  約定コスト（スプレッド+スリッページ）を保守/標準/悪化の3シナリオで反映する。
  さらに時系列ウォークフォワード（IS=検証用70% / OOS=確証用30%）に分割し、
  OOSの成績だけを採用判定に使う。

範囲・限界（重要、必ず読むこと）:
  - ここで使う run_backtest() は signal_scanner.py と同じTA/FAスコアリングを
    使うが、本番の全フィルタ（季節性・BOJサイクル・過熱降格・実績調整・
    イベント回避）は含まない簡略エンジン。ここで「OOS期待値プラス」が出ても
    本番の複雑なフィルタ込みの成績を保証するものではない——あくまで
    「素のTA/FAシグナル + コスト」だけを見た一次スクリーニング。
  - 複数ペア・複数シナリオを見る時点で複数比較（multiple comparisons）が
    発生している。White's Reality Check / Hansen's SPA等の多重検定補正は
    未実装（Sullivan et al.の指摘通り、これを経ないと見かけの優位性が
    出やすい）。今回は「採用の可否を機械的に判定する」目的ではなく、
    「コスト後もOOSでプラスかどうかの一次スクリーニング」として使うこと。

採用条件（ユーザー指定）: コスト後のOOSで期待値が正、最大DDが許容内、
既存戦略を上回ること。「許容内」の具体的な数値基準は口座リスク方針に
依存するため本スクリプトでは判定せず、実測値を出すところまでに留める。
"""

import json
from datetime import datetime, timezone

from signal_scanner import compute_ta_score, fetch_history, PAIR_API
from modules.rate_fetcher import fetch_live_central_bank_rates, compute_fa_score
from modules.backtest import run_backtest
from modules.backtest_costs import net_pips_and_r, summarize_r, jpy_estimate, COST_SCENARIOS

DEFAULT_TH = (60, 55)
LOOKBACK_DAYS = 180
OOS_FRACTION = 0.3  # 直近30%をOOS(確証用)、残りをIS(検証用)として扱う

# 既存戦略（現行本番ロジック）の参考ベースライン——2026-08-25会話で算出した
# 実クローズドトレードのR倍数実績。ここでは「これを上回るか」の比較対象として使う。
EXISTING_STRATEGY_BASELINE_R = {
    "label": "現行本番(2026-06-22ルール以降、実クローズド11件)",
    "note": "サンプル極小につき参考値。SL_HIT5/SIGNAL_LOST2/TRAIL_HIT2/BE_HIT2の内訳",
}


def split_is_oos(trades: list, price_len: int):
    """entry_idxを基準にIS(前半70%)/OOS(直近30%)へ分割する。"""
    if not trades:
        return [], []
    split_idx = int(price_len * (1 - OOS_FRACTION))
    is_trades = [t for t in trades if t["entry_idx"] < split_idx]
    oos_trades = [t for t in trades if t["entry_idx"] >= split_idx]
    return is_trades, oos_trades


def report_pair(pair, prices, cb_rates):
    r = run_backtest(pair, prices, compute_ta_score, compute_fa_score,
                      cb_rates, PAIR_API, lookback_days=LOOKBACK_DAYS, ta_thresholds=DEFAULT_TH)
    trades = r.get("trades", [])
    if not trades:
        print(f"  {pair:<8} トレードなし")
        return None

    is_trades, oos_trades = split_is_oos(trades, len(prices))
    if not oos_trades:
        print(f"  {pair:<8} OOS区間にトレードなし(IS={len(is_trades)}件)")
        return None

    print(f"  {pair} (IS={len(is_trades)}件 / OOS={len(oos_trades)}件)")

    result = {"pair": pair, "is_count": len(is_trades), "oos_count": len(oos_trades),
              "scenarios": {}, "raw_r_by_scenario": {}}
    for scenario in COST_SCENARIOS:
        r_values = []
        for t in oos_trades:
            calc = net_pips_and_r(t, pair, scenario)
            if calc:
                r_values.append(calc["net_r"])
        if not r_values:
            continue
        result["raw_r_by_scenario"][scenario] = r_values
        summary = summarize_r(r_values)
        summary["jpy_estimate_30k_risk"] = jpy_estimate(summary["expectancy_r"], summary["total"])
        result["scenarios"][scenario] = summary
        verdict = "OK(期待値プラス)" if summary["expectancy_r"] > 0 else "NG(期待値マイナス)"
        print(f"    [{scenario:<12}] 期待値={summary['expectancy_r']:+.3f}R "
              f"合計={summary['total_r']:+.2f}R 勝率{summary['win_rate']}% "
              f"PF{summary['profit_factor']} maxDD={summary['max_drawdown_r']}R "
              f"目安JPY(risk3万円/件想定)={summary['jpy_estimate_30k_risk']:+,.0f}円 -> {verdict}")

    return result


def main():
    print("=" * 78)
    print("コスト調整済みバックテスト（IS/OOSウォークフォワード・3シナリオ）")
    print("=" * 78)
    print(f"※範囲・限界はスクリプト冒頭のdocstring参照。IS/OOS分割={1-OOS_FRACTION:.0%}/{OOS_FRACTION:.0%}\n")

    cb_rates = fetch_live_central_bank_rates()

    # 主要ペア中心に検証（全34ペアだと低頻度ペアでOOSサンプルが枯渇するため、
    # まず流動性の高い主要ペアで機構を検証する）
    target_pairs = ["USDJPY", "EURJPY", "GBPJPY", "AUDJPY", "EURUSD", "GBPUSD", "AUDUSD"]

    print("【ペア別 OOS成績（3シナリオ）】")
    results = []
    for pair in target_pairs:
        prices = fetch_history(pair, 280)
        if not prices or len(prices) < 60:
            print(f"  {pair:<8} 履歴不足")
            continue
        res = report_pair(pair, prices, cb_rates)
        if res:
            results.append(res)

    # シナリオ別の全ペア合算（生R値を集約して再集計）
    print("\n【全ペア合算(OOS) シナリオ別】")
    for scenario in COST_SCENARIOS:
        all_r = []
        for res in results:
            all_r.extend(res["raw_r_by_scenario"].get(scenario, []))
        if not all_r:
            continue
        summary = summarize_r(all_r)
        summary["jpy_estimate_30k_risk"] = jpy_estimate(summary["expectancy_r"], summary["total"])
        verdict = "OK(期待値プラス)" if summary["expectancy_r"] > 0 else "NG(期待値マイナス)"
        print(f"  [{scenario:<12}] n={summary['total']:>3} 期待値={summary['expectancy_r']:+.3f}R "
              f"合計={summary['total_r']:+.2f}R 勝率{summary['win_rate']}% "
              f"PF{summary['profit_factor']} maxDD={summary['max_drawdown_r']}R "
              f"目安JPY(risk3万円/件想定)={summary['jpy_estimate_30k_risk']:+,.0f}円 -> {verdict}")

    print(f"\n[NOTE] 既存戦略ベースライン: {EXISTING_STRATEGY_BASELINE_R['label']} — {EXISTING_STRATEGY_BASELINE_R['note']}")
    print("[NOTE] 採用可否の最終判断は、上記OOS期待値・PF・maxDDを口座のリスク許容値と")
    print("       照らし合わせて人間が行うこと（本スクリプトは数値算出まで）。")
    print("[NOTE] 複数ペア×複数シナリオの多重比較補正(Reality Check/SPA)は未実装。")

    out = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "lookback_days": LOOKBACK_DAYS,
        "oos_fraction": OOS_FRACTION,
        "results": results,
    }
    with open("data/cost_adjusted_backtest_latest.json", "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print("\n[OK] data/cost_adjusted_backtest_latest.json に結果を保存")


if __name__ == "__main__":
    main()
