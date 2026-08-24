#!/usr/bin/env python3
"""
run_cost_adjusted_backtest.py
2026-08-25追加（最優先タスク）:
  全バックテストを「正しいpip換算 → JPY損益 or R倍数」に統一し、
  約定コスト（スプレッド+スリッページ）を保守/標準/悪化の3シナリオで反映する。
  さらに時系列ウォークフォワード（IS=検証用70% / OOS=確証用30%）に分割し、
  OOSの成績だけを採用判定に使う。全34ペアを対象に、White's Reality Check
  （modules/reality_check.py）で「一番良さそうなペアを選んだだけ」による
  見かけの優位性を補正したp値を算出する。

範囲・限界（重要、必ず読むこと）:
  - ここで使う run_backtest() は signal_scanner.py と同じTA/FAスコアリングを
    使うが、本番の全フィルタ（季節性・BOJサイクル・過熱降格・実績調整・
    イベント回避）は含まない簡略エンジン。ここで「OOS期待値プラス」が出ても
    本番の複雑なフィルタ込みの成績を保証するものではない——あくまで
    「素のTA/FAシグナル + コスト」だけを見た一次スクリーニング。
  - Reality CheckはWhite(2000)の簡略実装（stationary bootstrap、ブロック確率
    固定）。Hansen(2005)のSPA（studentized・劣後候補の除外）は未実装——
    SPAの方が検定力が高いが、RCより保守的な分だけ「p値が有意に出ない」方向の
    誤りは少ない。

採用条件（ユーザー指定）: コスト後のOOSで期待値が正、最大DDが許容内、
既存戦略を上回ること、かつReality Check p値で「たまたま良く見えているだけ」
ではないことを確認する。「許容内」の具体的な数値基準は口座リスク方針に
依存するため本スクリプトでは判定せず、実測値を出すところまでに留める。
"""

import json
from datetime import datetime, timezone

from signal_scanner import compute_ta_score, fetch_history_with_dates, PAIR_API
from modules.rate_fetcher import fetch_live_central_bank_rates, compute_fa_score
from modules.backtest import run_backtest
from modules.backtest_costs import net_pips_and_r, summarize_r, jpy_estimate, COST_SCENARIOS
from modules.reality_check import build_daily_r_panel, reality_check

DEFAULT_TH = (60, 55)
LOOKBACK_DAYS = 180
OOS_FRACTION = 0.3  # 直近30%をOOS(確証用)、残りをIS(検証用)として扱う
RC_SCENARIO = "standard"  # Reality Checkに使うコストシナリオ（1本に絞って計算コストを抑える）


def split_is_oos(trades: list, price_len: int):
    """entry_idxを基準にIS(前半70%)/OOS(直近30%)へ分割する。"""
    if not trades:
        return [], []
    split_idx = int(price_len * (1 - OOS_FRACTION))
    is_trades = [t for t in trades if t["entry_idx"] < split_idx]
    oos_trades = [t for t in trades if t["entry_idx"] >= split_idx]
    return is_trades, oos_trades


def report_pair(pair, dates, prices, cb_rates):
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
              "scenarios": {}, "raw_r_by_scenario": {}, "oos_trades_dated": [],
              "cost_dominated": False}
    # standardシナリオでコスト比率を判定し、支配的ならこのペア自体を評価対象外とする
    # （ATRベースの初期SL幅がスプレッド推定値より小さい低ボラ・低単価エキゾチック
    # クロス向けの安全弁。2026-08-25判明: KRWJPY等でR=-19R等の数値破綻が発生した）
    ratios = [net_pips_and_r(t, pair, "standard")["cost_to_risk_ratio"]
              for t in oos_trades if net_pips_and_r(t, pair, "standard")]
    if ratios and (sum(ratios) / len(ratios)) >= 0.5:
        result["cost_dominated"] = True
        avg_ratio = sum(ratios) / len(ratios)
        print(f"  {pair} コスト比率過大のため評価対象外"
              f"（平均cost/risk={avg_ratio:.1f}倍・ATRベースSL幅がスプレッド推定値より小さい）")
        return result

    for scenario in COST_SCENARIOS:
        r_values = []
        for t in oos_trades:
            calc = net_pips_and_r(t, pair, scenario)
            if calc:
                r_values.append(calc["net_r"])
                if scenario == RC_SCENARIO:
                    exit_idx = min(t["exit_idx"], len(dates) - 1)
                    result["oos_trades_dated"].append(
                        {"exit_date": dates[exit_idx], "net_r": calc["net_r"]}
                    )
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
    print("コスト調整済みバックテスト（全34ペア・IS/OOSウォークフォワード・3シナリオ）")
    print("=" * 78)
    print(f"※範囲・限界はスクリプト冒頭のdocstring参照。IS/OOS分割={1-OOS_FRACTION:.0%}/{OOS_FRACTION:.0%}\n")

    cb_rates = fetch_live_central_bank_rates()

    print(f"[INFO] {len(PAIR_API)}ペアの履歴取得中(280日)...")
    print("【ペア別 OOS成績（3シナリオ）】")
    results = []
    for pair in sorted(PAIR_API):
        fetched = fetch_history_with_dates(pair, 280)
        if not fetched or len(fetched[1]) < 60:
            print(f"  {pair:<8} 履歴不足")
            continue
        dates, prices = fetched
        res = report_pair(pair, dates, prices, cb_rates)
        if res:
            results.append(res)

    excluded = [r["pair"] for r in results if r.get("cost_dominated")]
    if excluded:
        print(f"\n[INFO] コスト比率過大につき評価対象外: {excluded}")

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

    # ── Reality Check（多重比較補正）──
    print(f"\n【Reality Check（{RC_SCENARIO}シナリオ・ペア横断で最良候補の優位性を補正検定）】")
    pair_trades = {res["pair"]: res["oos_trades_dated"] for res in results if res["oos_trades_dated"]}
    all_exit_dates = [t["exit_date"] for trades in pair_trades.values() for t in trades]
    rc_result = {}
    if len(pair_trades) >= 2 and all_exit_dates:
        start_date, end_date = min(all_exit_dates), max(all_exit_dates)
        panel = build_daily_r_panel(pair_trades, start_date, end_date)
        rc_result = reality_check(panel, n_boot=1000)
        if rc_result.get("p_value") is not None:
            print(f"  候補数(ペア)={len(pair_trades)} 日次サンプル={rc_result['n_days']}日 "
                  f"ブートストラップ={rc_result['n_boot']}回")
            print(f"  最良候補: {rc_result['best_pair']} (日次平均R={rc_result['best_mean_r']:+.4f})")
            print(f"  Reality Check p値 = {rc_result['p_value']:.4f} "
                  + ("→ 有意水準5%で偶然とは言えない" if rc_result['p_value'] < 0.05
                     else "→ 有意水準5%で偶然の産物である可能性を否定できない"))
        else:
            print(f"  [SKIP] {rc_result.get('note', '検定に必要なデータが不足')}")
    else:
        print("  [SKIP] 候補ペア数またはOOSトレードが不足のため検定不能")

    print(f"\n[NOTE] 採用可否の最終判断は、上記OOS期待値・PF・maxDD・Reality Check p値を")
    print("       口座のリスク許容値と照らし合わせて人間が行うこと（本スクリプトは数値算出まで）。")
    print("[NOTE] Hansen's SPA（RCより検定力が高い版）は未実装。")

    out = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "lookback_days": LOOKBACK_DAYS,
        "oos_fraction": OOS_FRACTION,
        "results": [{k: v for k, v in res.items() if k != "oos_trades_dated"} for res in results],
        "reality_check": rc_result,
    }
    with open("data/cost_adjusted_backtest_latest.json", "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print("\n[OK] data/cost_adjusted_backtest_latest.json に結果を保存")


if __name__ == "__main__":
    main()
