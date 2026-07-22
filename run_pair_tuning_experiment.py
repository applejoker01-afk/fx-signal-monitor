#!/usr/bin/env python3
"""
run_pair_tuning_experiment.py
レバー1実験: ペア除外リスト + ペア固有閾値（2026-06-09 閾値最適化レポートの提言）を
本番コードに触れずにバックテストで検証する。

比較バリアント:
  V0_現状        : 全ペア一律 (ta60/fa55)
  V1_除外        : V0 から INRJPY/TRYJPY を除外
  V2_除外+制限   : V1 + USDCHF/EURUSD を★5相当 (ta75/fa65) に格上げ
  V3_フル提言    : V2 + SGDJPY/EURAUD を (ta55/fa50) に緩和

検証窓:
  180日 … 2026-06-09 レポートと同じ土俵（ただし窓は約6週シフト済み）
   30日 … 提言日(6/9)より後のデータが主体の準アウトオブサンプル（件数少・方向確認用）

判断基準（実装可否）:
  ・180日窓で V3 (or V2) が V0 より PF・pips とも改善
  ・30日窓で改善方向が矛盾しない（悪化していない）
  ・除外ペアの直近成績が依然として悪い（除外根拠の持続確認）
"""

import json
import os
from datetime import datetime, timezone
from pathlib import Path

from signal_scanner import compute_ta_score, fetch_history, PAIR_API
from modules.rate_fetcher import fetch_live_central_bank_rates, compute_fa_score
from modules.backtest import run_backtest

DEFAULT_TH = (60, 55)

VARIANTS = {
    "V0_現状": {
        "excluded": set(),
        "thresholds": {},
    },
    "V1_除外": {
        "excluded": {"INRJPY", "TRYJPY"},
        "thresholds": {},
    },
    "V2_除外+制限": {
        "excluded": {"INRJPY", "TRYJPY"},
        "thresholds": {"USDCHF": (75, 65), "EURUSD": (75, 65)},
    },
    "V3_フル提言": {
        "excluded": {"INRJPY", "TRYJPY"},
        "thresholds": {
            "USDCHF": (75, 65), "EURUSD": (75, 65),
            "SGDJPY": (55, 50), "EURAUD": (55, 50),
        },
    },
    # 2026-07-22追加: ペア別内訳で判明した新ペア(7/20拡張分)の低成績を反映。
    # PLNJPYは180日窓(37.5%/PF0.7/最大pips損失)と30日窓(0%/2連敗)の両方で悪い。
    # SEKJPY/EURCHFは180日窓のみ悪い(片窓根拠)ため除外でなく★5相当への制限に留める。
    "V4_本日提言": {
        "excluded": {"INRJPY", "TRYJPY", "PLNJPY"},
        "thresholds": {
            "USDCHF": (75, 65), "EURUSD": (75, 65),
            "SEKJPY": (75, 65), "EURCHF": (75, 65),
        },
    },
    # 本番の PAIR_EXCLUDE (performance_intelligence.py, 2026-06-16/19) を正確に再現。
    # 注意: 本番はさらに動的パフォーマンス重み付け・BOJ/VIX/スプレッド等のフィルタを
    # 持つため、これは「除外構成だけを本番に合わせた近似」。
    "V5_本番相当": {
        "excluded": {"INRJPY", "TRYJPY", "EURUSD", "USDCHF", "NZDJPY", "CADJPY"},
        "thresholds": {},
    },
    # V5 + 本日の発見（PLNJPY除外、SEKJPY/EURCHF制限）
    "V6_本番+本日": {
        "excluded": {"INRJPY", "TRYJPY", "EURUSD", "USDCHF", "NZDJPY", "CADJPY", "PLNJPY"},
        "thresholds": {"SEKJPY": (75, 65), "EURCHF": (75, 65)},
    },
}

WATCH_PAIRS = ["INRJPY", "TRYJPY", "USDCHF", "EURUSD", "SGDJPY", "EURAUD"]


def aggregate(by_pair: dict) -> dict:
    """ペア別結果を全体集計する（PFは全トレードの粗利/粗損から再計算）。"""
    if not by_pair:
        return {}
    all_trades = [t for r in by_pair.values() for t in r["trades"]]
    total = len(all_trades)
    if total == 0:
        return {}
    wins = sum(1 for t in all_trades if t["result"] == "WIN")
    gross_p = sum(t["pips"] for t in all_trades if t["pips"] > 0)
    gross_l = abs(sum(t["pips"] for t in all_trades if t["pips"] < 0))
    return {
        "total": total,
        "wins": wins,
        "win_rate": round(wins / total * 100, 1),
        "profit_factor": round(gross_p / gross_l, 2) if gross_l > 0 else 999.0,
        "total_pips": round(sum(t["pips"] for t in all_trades), 2),
        "sl_rate": round(sum(1 for t in all_trades if t["exit_reason"] == "SL_HIT") / total * 100, 1),
        "tp1": sum(1 for t in all_trades if t["exit_reason"] == "TP1_HIT"),
        "tp2": sum(1 for t in all_trades if t["exit_reason"] == "TP2_HIT"),
        "tp3": sum(1 for t in all_trades if t["exit_reason"] == "TP3_HIT"),
    }


def run_variant(histories, cb_rates, variant, lookback):
    by_pair = {}
    for pair, prices in histories.items():
        if pair in variant["excluded"]:
            continue
        th = variant["thresholds"].get(pair, DEFAULT_TH)
        r = run_backtest(
            pair, prices, compute_ta_score, compute_fa_score,
            cb_rates, PAIR_API, lookback_days=lookback, ta_thresholds=th,
        )
        if r.get("total", 0) > 0:
            by_pair[pair] = r
    return by_pair


def main():
    print("=" * 68)
    print("レバー1実験: ペア除外 + ペア固有閾値（2026-06-09提言の検証）")
    print("=" * 68)

    cb_rates = fetch_live_central_bank_rates()

    print(f"[INFO] {len(PAIR_API)}ペアの履歴取得中...")
    histories = {}
    for pair in PAIR_API:
        prices = fetch_history(pair, 280)
        if prices and len(prices) >= 60:
            histories[pair] = prices
    print(f"[INFO] {len(histories)}ペア取得完了")

    report = {"generated_at": datetime.now(timezone.utc).isoformat(), "windows": {}}

    for lookback in (180, 30):
        label = f"{lookback}日窓"
        print(f"\n{'─' * 68}\n【{label}】")
        win_report = {}
        v0_by_pair = None
        for name, variant in VARIANTS.items():
            by_pair = run_variant(histories, cb_rates, variant, lookback)
            if name == "V0_現状":
                v0_by_pair = by_pair
            ov = aggregate(by_pair)
            win_report[name] = ov
            if not ov:
                print(f"  {name:<14} トレードなし")
                continue
            print(f"  {name:<14} PF{ov['profit_factor']:<6} 勝率{ov['win_rate']:<5}% "
                  f"{ov['total_pips']:+9.2f}pips {ov['total']:>3}件 SL率{ov['sl_rate']}% "
                  f"TP1/2/3:{ov['tp1']}/{ov['tp2']}/{ov['tp3']}")

        # 対象ペアの単体成績（V0ベース）— 6月の除外/制限根拠が持続しているか
        print(f"\n  [対象ペア単体成績 (V0・{label})]")
        for p in WATCH_PAIRS:
            r = (v0_by_pair or {}).get(p)
            if r:
                print(f"    {p:<8} 勝率{r['win_rate']:>5}% PF{r['profit_factor']:<6} "
                      f"{r['total_pips']:+8.2f}pips {r['total']}件")
            else:
                print(f"    {p:<8} トレードなし")
        win_report["watch_pairs_v0"] = {
            p: {k: (v0_by_pair[p][k] if p in (v0_by_pair or {}) else None)
                for k in ("win_rate", "profit_factor", "total_pips", "total")}
            if p in (v0_by_pair or {}) else None
            for p in WATCH_PAIRS
        }
        report["windows"][label] = win_report

    out = Path(os.environ.get("EXPERIMENT_OUT", "data/pair_tuning_experiment.json"))
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\n[INFO] 結果JSON -> {out}")


if __name__ == "__main__":
    main()
