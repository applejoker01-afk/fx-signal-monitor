"""
reality_check.py
2026-08-25追加: White's Reality Check (White 2000) / Sullivan, Timmermann & White (1999)
の統計的補正を実装する。

背景: run_cost_adjusted_backtest.py で複数ペア（今後34ペア）× 複数コストシナリオを
比較し「一番良さそうなペア」を見つけると、それだけで見かけの優位性（data snooping
bias）が生じる——候補が多いほど、たまたま良い結果を出す候補が現れる確率が上がる。
Reality Checkは「観測された最良候補の優位性が、L個の候補を試したことによる偶然の
産物ではないか」を、ブートストラップで補正したp値として検定する。

方法（White 2000 / Sullivan et al. 1999 に準拠、簡略実装）:
  1. 各候補（ここではペア）について、共通の日次カレンダー上で「その日に決済された
     トレードのR倍数（無ければ0）」の日次系列を作る（複数ペアを同じ時間軸に揃える
     ことが、cross-strategy相関を保ったブートストラップに必須）。
  2. 観測統計量: 各候補の日次平均R（=期待値R）。最良候補 f_max = max_k mean(R_k)。
  3. Politis-Romano の定常ブートストラップ（stationary bootstrap）で日次パネルを
     ブロック単位で複数回リサンプリング（全ペア共通のリサンプリング添字を使うことで
     ペア間の同時点相関を保持する）。
  4. 各ブートストラップ標本で、候補ごとに「元の平均を差し引いた」統計量を計算し、
     その最大値の分布を作る。観測されたf_maxがこの分布の何パーセンタイルかがp値。

限界:
  - 候補数Lが小さい・日次サンプルが薄いと検定力が低く、p値は保守的（帰無仮説を
    棄却しにくい）方向に出やすい。
  - Hansen(2005)のSPA（studentized・no-worse-than-benchmark候補の除外）までは
    未実装。SPAはRCより検定力が高いが、実装するなら次のステップ。
"""

import random
from datetime import datetime, timedelta


def build_daily_r_panel(pair_trades: dict, start_date: str, end_date: str) -> dict:
    """
    pair_trades: {pair: [{"exit_date": "YYYY-MM-DD", "net_r": float}, ...]}
    同じ日に複数トレードが決済された場合は合算する。

    Returns: {pair: [r_day0, r_day1, ...]}  (start_date〜end_dateの全暦日、0埋め)
    """
    start = datetime.strptime(start_date, "%Y-%m-%d")
    end = datetime.strptime(end_date, "%Y-%m-%d")
    n_days = (end - start).days + 1
    if n_days <= 0:
        return {}

    panel = {}
    for pair, trades in pair_trades.items():
        series = [0.0] * n_days
        for t in trades:
            try:
                idx = (datetime.strptime(t["exit_date"], "%Y-%m-%d") - start).days
            except (ValueError, KeyError):
                continue
            if 0 <= idx < n_days:
                series[idx] += t["net_r"]
        panel[pair] = series
    return panel


def _stationary_bootstrap_indices(n: int, block_prob: float, rng: random.Random) -> list:
    """
    Politis-Romano定常ブートストラップの添字列を1本生成する。
    block_prob: ブロックを打ち切る確率（期待ブロック長 = 1/block_prob）。
    """
    indices = []
    idx = rng.randrange(n)
    for _ in range(n):
        indices.append(idx)
        if rng.random() < block_prob:
            idx = rng.randrange(n)  # 新しいブロックの開始点をランダムに選び直す
        else:
            idx = (idx + 1) % n  # ブロックを継続（循環）
    return indices


def reality_check(panel: dict, n_boot: int = 1000, block_prob: float = 0.1,
                   seed: int = 42) -> dict:
    """
    White's Reality Check p値を計算する。

    panel: build_daily_r_panel() の出力（全候補が同じ長さの日次系列を持つこと）。

    Returns:
        {
          "candidates": {pair: observed_mean_r},
          "best_pair": str, "best_mean_r": float,
          "n_boot": int, "n_days": int,
          "p_value": float,  # 小さいほど「最良候補の優位性は偶然でない」根拠が強い
        }
    """
    if not panel:
        return {}
    pairs = list(panel.keys())
    n_days = len(panel[pairs[0]])
    if any(len(s) != n_days for s in panel.values()):
        raise ValueError("全候補の日次系列の長さが揃っていません")
    if n_days < 30:
        return {
            "candidates": {p: sum(s) / n_days if n_days else 0.0 for p, s in panel.items()},
            "n_days": n_days, "n_boot": 0, "p_value": None,
            "note": f"日次サンプル{n_days}日は少なすぎて検定不能（目安30日以上）",
        }

    observed_means = {p: sum(s) / n_days for p, s in panel.items()}
    f_max = max(observed_means.values())
    best_pair = max(observed_means, key=observed_means.get)

    rng = random.Random(seed)
    exceed_count = 0
    for _ in range(n_boot):
        idx_seq = _stationary_bootstrap_indices(n_days, block_prob, rng)
        boot_max = -float("inf")
        for p in pairs:
            series = panel[p]
            boot_mean = sum(series[i] for i in idx_seq) / n_days
            # 元の平均を差し引いて「帰無仮説(真の期待値0)下での分布」を作る
            centered = boot_mean - observed_means[p]
            boot_max = max(boot_max, centered)
        if boot_max >= f_max:
            exceed_count += 1

    p_value = exceed_count / n_boot

    return {
        "candidates": observed_means,
        "best_pair": best_pair,
        "best_mean_r": round(f_max, 4),
        "n_days": n_days,
        "n_boot": n_boot,
        "p_value": round(p_value, 4),
    }


if __name__ == "__main__":
    # 簡易セルフテスト: 3候補中1つだけ本物の正の期待値を持つ合成データ
    rng = random.Random(1)
    n = 200
    panel = {
        "NOISE_A": [rng.gauss(0, 1) for _ in range(n)],
        "NOISE_B": [rng.gauss(0, 1) for _ in range(n)],
        "REAL_EDGE": [rng.gauss(0.15, 1) for _ in range(n)],
    }
    result = reality_check(panel, n_boot=500)
    print(result)
