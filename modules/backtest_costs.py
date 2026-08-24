"""
backtest_costs.py
2026-08-25追加: バックテストの成績を「正しいpip換算 → JPY損益 or R倍数」に統一し、
約定コスト（スプレッド＋スリッページ）を保守/標準/悪化の3シナリオで反映するための
共通ユーティリティ。

背景: modules/backtest.py の "pips" フィールドは生の価格差（例: GBPJPYなら円建ての
価格差そのもの、例 217.987→217.664 なら 0.323）であり、spread_monitor.py の
SPREAD_PIPS_BASE は業界慣習の「pips」単位（JPYクロスは0.01円=1pips、それ以外は
0.0001=1pips）。この2つの単位を混同すると、コストを1/100〜1/10000に見誤る。
本モジュールはその変換を一箇所に集約する。
"""

from modules.spread_monitor import SPREAD_PIPS_BASE, _DEFAULT_PIPS

# シナリオ別スリッページ倍率（スプレッドに対する倍率、往復コストとして計算）
# conservative: 提示スプレッド通りに約定できた場合（楽観的下限）
# standard    : 提示スプレッドの1.3倍相当（通常の指値ズレ・約定遅延を織り込む）
# adverse     : 提示スプレッドの2.5倍相当（指標発表直後・薄商い時の悪化ケース）
COST_SCENARIOS = {
    "conservative": 1.0,
    "standard": 1.3,
    "adverse": 2.5,
}


def pip_size(pair: str) -> float:
    """1pipsに相当する生の価格差。JPYクロスは0.01、それ以外は0.0001。"""
    return 0.01 if pair.endswith("JPY") else 0.0001


def spread_cost_raw(pair: str) -> float:
    """SPREAD_PIPS_BASE(pips単位)を生の価格差単位に変換した片道スプレッド。"""
    spread_pips = SPREAD_PIPS_BASE.get(pair, _DEFAULT_PIPS)
    return spread_pips * pip_size(pair)


def cost_per_trade_raw(pair: str, scenario: str = "standard") -> float:
    """
    1トレードあたりの往復コスト（エントリー+決済の合計、生の価格差単位）。
    スプレッドは通常「片道」として提示されるが成行の往復では実質的に
    スプレッド分がコストとして一度乗る想定とし、シナリオ倍率で
    スリッページ分を上乗せする。
    """
    mult = COST_SCENARIOS.get(scenario, COST_SCENARIOS["standard"])
    return spread_cost_raw(pair) * mult


def trade_r_multiple(trade: dict) -> float | None:
    """
    1トレードのR倍数（初期SL幅を1Rとした場合の実現pips）。
    initial_sl があればそれを優先（トレーリング等でslが動いた後の値と混同しないため）。
    """
    entry = trade.get("entry_price")
    initial_sl = trade.get("initial_sl", trade.get("sl"))
    pips = trade.get("pips")
    if entry is None or initial_sl is None or pips is None:
        return None
    risk_dist = abs(entry - initial_sl)
    if risk_dist <= 0:
        return None
    return pips / risk_dist


# 2026-08-25判明: KRWJPY/INRJPY/TRYJPY等の低ボラ・低単価エキゾチッククロスは、
# ATRベースの初期SL幅がミリ単位で極端に小さいのに対し、SPREAD_PIPS推定値
# （未検証の初期値、コード内コメントにも「要検証」と明記）は他ペアからの
# 類推でそれなりの大きさがあるため、コストがリスク幅の何倍にも達し、R倍数が
# -19R〜-48Rのように数値的に破綻する。R = pips/risk_dist という定義上、
# risk_distに対してcostが同程度以上だとR自体が意味を失う（分母が実質コストに
# 支配される）ため、機械的な閾値でこのケースを検出し「評価不能」として
# 別扱いする（除外して見なかったことにするのではなく、コスト比率過大という
# 事実そのものを報告する）。
COST_DOMINANCE_THRESHOLD = 0.5  # コストがリスク幅の50%以上ならR評価不能とみなす


def net_pips_and_r(trade: dict, pair: str, scenario: str = "standard") -> dict | None:
    """
    コスト控除後のpips・R倍数を返す。
    Returns: {"gross_pips", "net_pips", "gross_r", "net_r", "cost_raw", "cost_dominates"} または None
    cost_dominates=True の場合、net_r は数値的に信頼できない（呼び出し側で除外を推奨）。
    """
    entry = trade.get("entry_price")
    initial_sl = trade.get("initial_sl", trade.get("sl"))
    gross_pips = trade.get("pips")
    if entry is None or initial_sl is None or gross_pips is None:
        return None
    risk_dist = abs(entry - initial_sl)
    if risk_dist <= 0:
        return None

    cost = cost_per_trade_raw(pair, scenario)
    net_pips = gross_pips - cost
    cost_dominates = (cost / risk_dist) >= COST_DOMINANCE_THRESHOLD

    return {
        "gross_pips": gross_pips,
        "net_pips": net_pips,
        "gross_r": gross_pips / risk_dist,
        "net_r": net_pips / risk_dist,
        "cost_raw": cost,
        "cost_dominates": cost_dominates,
        "cost_to_risk_ratio": round(cost / risk_dist, 3),
    }


def summarize_r(trades_with_r: list) -> dict:
    """R倍数のリストから期待値・PF・最大ドローダウン(R累積の谷)を計算する。"""
    if not trades_with_r:
        return {}
    total = len(trades_with_r)
    wins = sum(1 for r in trades_with_r if r > 0)
    gross_p = sum(r for r in trades_with_r if r > 0)
    gross_l = abs(sum(r for r in trades_with_r if r < 0))
    expectancy_r = sum(trades_with_r) / total

    # 最大ドローダウン(R累積の山→谷の最大下落幅)
    cum = 0.0
    peak = 0.0
    max_dd = 0.0
    for r in trades_with_r:
        cum += r
        peak = max(peak, cum)
        max_dd = max(max_dd, peak - cum)

    return {
        "total": total,
        "win_rate": round(wins / total * 100, 1),
        "profit_factor": round(gross_p / gross_l, 2) if gross_l > 0 else 999.0,
        "expectancy_r": round(expectancy_r, 4),
        "total_r": round(sum(trades_with_r), 3),
        "max_drawdown_r": round(max_dd, 3),
    }


def jpy_estimate(expectancy_r: float, total_trades: int, assumed_risk_jpy: float = 30000.0) -> float:
    """
    R期待値をJPY目安に換算する（あくまで目安。実際のリスク額はresidual balance・
    exposure_multiplier・confidence_multiplierで毎回変わるため、正確な会計ではない）。
    """
    return round(expectancy_r * total_trades * assumed_risk_jpy, 0)
