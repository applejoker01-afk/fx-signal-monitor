# -*- coding: utf-8 -*-
"""D-2: カルマンフィルタ・フェアバリュー(FV)モデル

状態方程式:  F_t = F_{t-1} + beta * x_t + eta_t,   eta ~ N(0, Q)
観測方程式:  p_t = F_t + eps_t,                    eps ~ N(0, R)

  p_t : 対数価格 log(USDJPY)（週次、COT報告基準日=火曜に整列）
  x_t : 標準化済みフロー代理変数
        既定: COT投機筋ネットポジション週次変化の符号反転 z スコア
        （＋ = 円売りフロー強度。log(USDJPY)を押し上げる想定なので β>0 が期待符号）

出力はすべて「フィルタ値」＝時点tの値はt以前の情報のみで計算（ルックアヘッドなし）。

重要な設計メモ（README にも記載）:
  1次元ローカルレベル型では、フィルタ後ギャップ p_t - F_t|t は
  (1-K_t)·イノベーション に恒等的に一致する（＝毎週リセットされる）。
  したがって「持続的な乖離」の主指標には gap ではなく
  13週(≈3ヶ月)累積イノベーション cum13 を用いる。
  これは「過去3ヶ月の累積フロー関数で説明できない価格変化の総和」に相当する。
"""
from __future__ import annotations

from dataclasses import dataclass, asdict

import numpy as np
import pandas as pd
from scipy.optimize import minimize

__all__ = ["KFParams", "kalman_filter", "fit_mle", "run_model"]

CUM_WINDOW = 13          # ≈3ヶ月（週次）
CUM_MIN_PERIODS = 8
Z_WINDOW = 104           # 2年でzスコア化
Z_MIN_PERIODS = 52


@dataclass
class KFParams:
    beta: float      # フロー感応度（log価格/フローzスコア1単位）
    q: float         # 状態ノイズ分散
    r: float         # 観測ノイズ分散
    loglik: float
    n_obs: int

    def to_dict(self) -> dict:
        return asdict(self)


def kalman_filter(
    p: np.ndarray,
    x: np.ndarray,
    beta: float,
    q: float,
    r: float,
    f0: float | None = None,
    p0: float | None = None,
):
    """1次元カルマンフィルタ（因果的：時点tの出力はt以前の情報のみを使用）。

    Returns
    -------
    F     : フィルタ後状態（対数FV）
    Pc    : 状態分散
    innov : イノベーション v_t = p_t - (F_{t-1} + beta*x_t)   [t>=1]
    ivar  : イノベーション分散 S_t
    ll    : 対数尤度（t>=1 の和）
    """
    p = np.asarray(p, dtype=float)
    x = np.asarray(x, dtype=float)
    n = p.size
    if n < 3:
        raise ValueError("観測が少なすぎます (n < 3)")

    F = np.full(n, np.nan)
    Pc = np.full(n, np.nan)
    innov = np.full(n, np.nan)
    ivar = np.full(n, np.nan)

    f = p[0] if f0 is None else float(f0)
    pc = (10.0 * float(np.var(np.diff(p))) + 1e-8) if p0 is None else float(p0)
    F[0], Pc[0] = f, pc

    ll = 0.0
    for t in range(1, n):
        # 予測
        f_pred = f + beta * x[t]
        p_pred = pc + q
        # イノベーション
        v = p[t] - f_pred
        s = p_pred + r
        # 更新
        k = p_pred / s
        f = f_pred + k * v
        pc = (1.0 - k) * p_pred
        F[t], Pc[t] = f, pc
        innov[t], ivar[t] = v, s
        ll += -0.5 * (np.log(2.0 * np.pi * s) + (v * v) / s)

    return F, Pc, innov, ivar, ll


def fit_mle(p: np.ndarray, x: np.ndarray) -> KFParams:
    """最尤法で (Q, R, beta) を推定。Nelder-Mead を複数初期値で回して局所解を回避。"""
    p = np.asarray(p, dtype=float)
    x = np.asarray(x, dtype=float)
    var_dp = float(np.var(np.diff(p))) + 1e-12

    def nll(theta: np.ndarray) -> float:
        q = float(np.exp(np.clip(theta[0], -40.0, 5.0)))
        r = float(np.exp(np.clip(theta[1], -40.0, 5.0)))
        beta = float(np.clip(theta[2], -1.0, 1.0))
        try:
            *_, ll = kalman_filter(p, x, beta, q, r)
        except FloatingPointError:
            return 1e12
        return -ll if np.isfinite(ll) else 1e12

    base = np.array([np.log(var_dp * 0.5), np.log(var_dp * 0.5), 0.0])
    best = None
    for b0 in (0.0, 0.002, -0.002):
        res = minimize(
            nll,
            np.array([base[0], base[1], b0]),
            method="Nelder-Mead",
            options={"maxiter": 5000, "xatol": 1e-9, "fatol": 1e-9},
        )
        if best is None or res.fun < best.fun:
            best = res

    q = float(np.exp(np.clip(best.x[0], -40.0, 5.0)))
    r = float(np.exp(np.clip(best.x[1], -40.0, 5.0)))
    beta = float(np.clip(best.x[2], -1.0, 1.0))
    *_, ll = kalman_filter(p, x, beta, q, r)
    return KFParams(beta=beta, q=q, r=r, loglik=float(ll), n_obs=int(p.size))


def _rolling_z(s: pd.Series, window: int = Z_WINDOW, min_periods: int = Z_MIN_PERIODS) -> pd.Series:
    roll = s.rolling(window, min_periods=min_periods)
    sd = roll.std(ddof=0)
    return (s - roll.mean()) / sd.replace(0.0, np.nan)


def run_model(
    df: pd.DataFrame,
    price_col: str = "price",
    flow_col: str = "flow_x",
) -> tuple[pd.DataFrame, KFParams]:
    """週次データフレームにモデルを適用する。

    Parameters
    ----------
    df : index=報告基準日（火曜）の週次フレーム。price は水準、flow_x は標準化済みフロー。

    Returns
    -------
    d      : 入力コピー + fv / innov / innov_z / cum13 / cum13_z 列
    params : 推定パラメータ
    """
    d = df.dropna(subset=[price_col, flow_col]).copy()
    if len(d) < 60:
        raise ValueError(f"週次観測が不足しています (n={len(d)}, 最低60週)")

    p = np.log(d[price_col].to_numpy(dtype=float))
    x = d[flow_col].to_numpy(dtype=float)

    params = fit_mle(p, x)
    F, Pc, innov, ivar, _ = kalman_filter(p, x, params.beta, params.q, params.r)

    d["log_fv"] = F
    d["fv"] = np.exp(F)
    innov_s = pd.Series(innov, index=d.index)
    with np.errstate(invalid="ignore"):
        d["innov_z"] = innov / np.sqrt(ivar)
    # 3ヶ月(13週)累積の「フローで説明できない変化」。log差なので ≈ 比率。
    d["cum13"] = innov_s.rolling(CUM_WINDOW, min_periods=CUM_MIN_PERIODS).sum()
    d["cum13_z"] = _rolling_z(d["cum13"])
    return d, params
