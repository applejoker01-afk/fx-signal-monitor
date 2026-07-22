# -*- coding: utf-8 -*-
"""D-2 カルマンフィルタの検証（合成データ）

1. パラメータ回復: 既知の (beta, Q, R) で生成した系列から MLE がそれらを回復するか
2. 因果性: フィルタ出力が「未来の観測に依存しない」こと（先頭部分の不変性）
3. cum13: 13週累積イノベーションの定義通りの計算
実行: python tests/test_kalman_fv.py
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from flow_layer.kalman_fv import fit_mle, kalman_filter, run_model


def make_synthetic(T=600, beta=0.004, sq=0.006, sr=0.005, seed=7):
    rng = np.random.default_rng(seed)
    x = rng.standard_normal(T)
    F = np.empty(T)
    F[0] = np.log(150.0)
    for t in range(1, T):
        F[t] = F[t - 1] + beta * x[t] + rng.normal(0.0, sq)
    p = F + rng.normal(0.0, sr, size=T)
    return p, x, F


def test_parameter_recovery():
    beta_true, sq, sr = 0.004, 0.006, 0.005
    p, x, F_true = make_synthetic(beta=beta_true, sq=sq, sr=sr)
    params = fit_mle(p, x)
    F_hat, *_ = kalman_filter(p, x, params.beta, params.q, params.r)

    corr = np.corrcoef(F_hat[10:], F_true[10:])[0, 1]
    q_ratio = params.q / (sq**2)
    r_ratio = params.r / (sr**2)

    print(
        f"  beta_hat={params.beta:+.5f} (真値 {beta_true:+.5f}) | "
        f"corr(F_hat,F_true)={corr:.4f} | Q比={q_ratio:.2f} R比={r_ratio:.2f}"
    )
    assert abs(params.beta - beta_true) < 0.0015, "betaの回復が不十分"
    assert corr > 0.99, "潜在FVの追跡が不十分"
    assert 1 / 3 < q_ratio < 3, "Qの回復が範囲外"
    assert 1 / 3 < r_ratio < 3, "Rの回復が範囲外"


def test_causality():
    """先頭250点のフィルタ出力は、その後の観測を追加しても変化しない。"""
    p, x, _ = make_synthetic(T=600)
    kw = dict(beta=0.004, q=0.006**2, r=0.005**2, f0=p[0], p0=1e-3)
    F_a, _, innov_a, ivar_a, _ = kalman_filter(p[:250], x[:250], **kw)
    F_b, _, innov_b, ivar_b, _ = kalman_filter(p, x, **kw)
    assert np.allclose(F_a, F_b[:250], atol=1e-12)
    assert np.allclose(innov_a[1:], innov_b[1:250], atol=1e-12, equal_nan=True)
    assert np.allclose(ivar_a[1:], ivar_b[1:250], atol=1e-12, equal_nan=True)
    print("  先頭250点のフィルタ出力は未来の観測に不変 → ルックアヘッドなし")


def test_run_model_cum13():
    p, x, _ = make_synthetic(T=400)
    idx = pd.date_range("2018-01-02", periods=400, freq="W-TUE")
    df = pd.DataFrame({"price": np.exp(p), "flow_x": x}, index=idx)
    out, params = run_model(df)

    # cum13 が innov の13週ローリング和に一致するか（内部再計算で確認）
    _, _, innov, ivar, _ = kalman_filter(
        np.log(out["price"].to_numpy()), out["flow_x"].to_numpy(),
        params.beta, params.q, params.r,
    )
    expected = pd.Series(innov, index=out.index).rolling(13, min_periods=8).sum()
    assert np.allclose(
        out["cum13"].to_numpy(dtype=float),
        expected.to_numpy(dtype=float),
        atol=1e-12, equal_nan=True,
    )
    assert out["fv"].notna().all()
    assert out["cum13_z"].iloc[-1] == out["cum13_z"].iloc[-1]  # not NaN
    print(f"  run_model OK: n={params.n_obs}, 末尾cum13_z={out['cum13_z'].iloc[-1]:+.2f}")


if __name__ == "__main__":
    ok = True
    for fn in (test_parameter_recovery, test_causality, test_run_model_cum13):
        print(f"[{fn.__name__}]")
        try:
            fn()
            print("  PASS")
        except AssertionError as e:
            ok = False
            print(f"  FAIL: {e}")
    sys.exit(0 if ok else 1)
