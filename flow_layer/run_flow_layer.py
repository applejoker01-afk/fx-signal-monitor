# -*- coding: utf-8 -*-
"""D系統オーケストレーター: 取得 → モデル → JSON出力 → 通知

実行:  python flow_layer/run_flow_layer.py

環境変数:
  FLOW_DATA_DIR       既定 "data"                     … キャッシュ/ログ置き場
  FLOW_OUTPUT_JSON    既定 "docs/data/flow_context.json" … Pages が読むJSON
  DISCORD_WEBHOOK_URL 任意 … 設定時のみ状態エスカレーションで通知
  FORCE_NOTIFY=1      任意 … 状態変化がなくても通知（動作確認用）
  FRED_API_KEY        任意 … 価格フォールバック用
  CFTC_APP_TOKEN      任意 … Socrata アプリトークン
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from flow_layer.fetch_cot import add_flow_features, load_or_update_cot
from flow_layer.fetch_price import get_daily_usdjpy
from flow_layer.kalman_fv import KFParams, run_model

JST = timezone(timedelta(hours=9))
SERIES_TAIL = 260  # パネルに渡す週数（約5年）


# ---------------------------------------------------------------- 状態判定
def classify(cum13_z, innov_z, net_z52) -> tuple[str, list[str]]:
    """(状態ラベル, アラート文リスト) を返す。文脈表示なので断定は避けた表現にする。"""
    alerts: list[str] = []

    cz = None if cum13_z is None or not np.isfinite(cum13_z) else float(cum13_z)
    iz = None if innov_z is None or not np.isfinite(innov_z) else float(innov_z)
    nz = None if net_z52 is None or not np.isfinite(net_z52) else float(net_z52)

    if cz is not None and abs(cz) >= 2.0:
        state = "乖離大"
        side = "円安側に上振れ" if cz > 0 else "円高側に下振れ"
        alerts.append(f"3ヶ月累積の未説明変化が大 (z={cz:+.1f})｜フロー対比で{side}")
    elif cz is not None and abs(cz) >= 1.0:
        state = "乖離注意"
    else:
        state = "フロー整合圏"

    if iz is not None and abs(iz) >= 2.5:
        alerts.append(f"今週の未説明変動が突出 (z={iz:+.1f})｜イベント/介入等の非フロー要因を確認")

    if nz is not None and nz <= -2.0:
        alerts.append(f"投機筋の円ショートが過熱 (z52={nz:+.1f})｜巻き戻し(2024年8月型)リスクの文脈")
    elif nz is not None and nz >= 2.0:
        alerts.append(f"投機筋の円ロングが過熱 (z52={nz:+.1f})")

    return state, alerts


# ---------------------------------------------------------------- JSON構築
def _round(v, nd):
    if v is None:
        return None
    f = float(v)
    return round(f, nd) if np.isfinite(f) else None


def build_context_json(
    weekly: pd.DataFrame,
    params: KFParams,
    daily_price: pd.Series | None = None,
    demo: bool = False,
) -> dict:
    last = weekly.iloc[-1]
    state, alerts = classify(
        last.get("cum13_z"), last.get("innov_z"), last.get("net_z52")
    )

    latest = {
        "report_date": str(pd.Timestamp(weekly.index[-1]).date()),
        "release_date": str(pd.Timestamp(last["release_date"]).date())
        if "release_date" in weekly.columns and pd.notna(last.get("release_date"))
        else None,
        "price_weekly": _round(last["price"], 3),
        "fair_value": _round(last["fv"], 3),
        "cum13_pct": _round(float(last["cum13"]) * 100.0, 2)
        if pd.notna(last.get("cum13"))
        else None,
        "cum13_z": _round(last.get("cum13_z"), 2),
        "innov_z": _round(last.get("innov_z"), 2),
        "net_spec_contracts": int(last["net"]) if pd.notna(last.get("net")) else None,
        "net_notional_oku_yen": _round(last.get("net_notional_oku_yen"), 0),
        "net_z52": _round(last.get("net_z52"), 2),
        "state": state,
    }

    now_block = None
    if daily_price is not None and len(daily_price) > 0:
        p_now = float(daily_price.iloc[-1])
        gap_now_log = np.log(p_now) - float(last["log_fv"])
        rel = latest["release_date"]
        staleness = None
        if rel:
            staleness = (datetime.now(JST).date() - pd.Timestamp(rel).date()).days
        now_block = {
            "price_date": str(pd.Timestamp(daily_price.index[-1]).date()),
            "price": _round(p_now, 3),
            "gap_vs_fv_pct": _round((np.exp(gap_now_log) - 1.0) * 100.0, 2),
            "cot_staleness_days": staleness,
            "note": "FVは直近COT報告時点の据え置き予測。次回公表まで参考値。",
        }

    tail = weekly.tail(SERIES_TAIL)
    series = {
        "date": [str(pd.Timestamp(d).date()) for d in tail.index],
        "price": [_round(v, 3) for v in tail["price"]],
        "fair_value": [_round(v, 3) for v in tail["fv"]],
        "cum13_z": [_round(v, 2) for v in tail["cum13_z"]],
        "net_spec": [int(v) if pd.notna(v) else None for v in tail["net"]],
    }

    return {
        "layer": "D系統: フロー文脈レイヤー",
        "pair": "USDJPY",
        "demo": bool(demo),
        "generated_at": datetime.now(JST).isoformat(timespec="seconds"),
        "model": {
            "type": "kalman_local_level_flow",
            "spec": "F_t = F_{t-1} + beta*x_t + eta(Q) / p_t = F_t + eps(R)",
            "flow_input": "z156(-ΔCOT投機筋ネット) ＝ 円売りフロー強度（β>0が期待符号）",
            **{k: _round(v, 8) if k in ("beta", "q", "r") else v for k, v in params.to_dict().items()},
        },
        "latest": latest,
        "now": now_block,
        "series": series,
        "alerts": alerts,
        "disclaimer": "文脈表示レイヤー（D-4検証前）。売買シグナルではありません。",
    }


# ---------------------------------------------------------------- 通知
def maybe_notify_discord(ctx: dict, prev: dict | None) -> None:
    url = os.environ.get("DISCORD_WEBHOOK_URL")
    if not url:
        return
    force = os.environ.get("FORCE_NOTIFY") == "1"
    prev_alerts = set((prev or {}).get("alerts", []))
    new_alerts = set(ctx.get("alerts", []))
    if not force and (not new_alerts or new_alerts == prev_alerts):
        return

    import requests

    lt, now = ctx["latest"], ctx.get("now") or {}
    lines = [
        f"**[fx-signal-monitor / D系統] USDJPY フロー文脈 更新** ({ctx['latest']['state']})",
        f"価格 {now.get('price', lt['price_weekly'])} / FV {lt['fair_value']} "
        f"/ 3M累積未説明 {lt['cum13_pct']:+.2f}% (z={lt['cum13_z']:+.1f})"
        if lt.get("cum13_pct") is not None
        else f"価格 {now.get('price', lt['price_weekly'])} / FV {lt['fair_value']}",
        f"COT投機筋ネット {lt['net_spec_contracts']:+,}枚 (z52={lt['net_z52']:+.1f}) "
        f"報告 {lt['report_date']} / 公表 {lt['release_date']}",
    ]
    lines += [f"⚠ {a}" for a in ctx.get("alerts", [])]
    lines.append(f"_{ctx['disclaimer']}_")
    try:
        requests.post(url, json={"content": "\n".join(lines)}, timeout=30)
    except Exception as e:
        print(f"[warn] Discord通知に失敗: {e}")


# ---------------------------------------------------------------- メイン
def main() -> int:
    data_dir = Path(os.environ.get("FLOW_DATA_DIR", "data"))
    out_path = Path(os.environ.get("FLOW_OUTPUT_JSON", "docs/data/flow_context.json"))

    print("[1/5] COT取得...")
    cot = add_flow_features(load_or_update_cot(data_dir / "cot_jpy.csv"))
    print(f"      {len(cot)}週 (最終報告 {cot['report_date'].iloc[-1].date()})")

    print("[2/5] 価格取得...")
    px = get_daily_usdjpy(data_dir / "usdjpy_daily.csv")
    print(f"      {len(px)}営業日 (最終 {px.index[-1].date()} = {px.iloc[-1]:.3f})")

    print("[3/5] 週次整列 + モデル推定...")
    weekly = cot.set_index("report_date").sort_index()
    px_df = px.rename("price").rename_axis("date").reset_index().sort_values("date")
    wk_df = weekly.reset_index().sort_values("report_date")
    merged = pd.merge_asof(
        wk_df, px_df, left_on="report_date", right_on="date", direction="backward"
    ).set_index("report_date")
    weekly = merged.drop(columns=["date"])

    fitted, params = run_model(weekly)
    print(
        f"      n={params.n_obs} beta={params.beta:+.5f} "
        f"sqrt(Q)={np.sqrt(params.q):.5f} sqrt(R)={np.sqrt(params.r):.5f} "
        f"logL={params.loglik:.1f}"
    )

    # パラメータドリフト監視ログ
    hist_path = data_dir / "kf_params_history.csv"
    row = pd.DataFrame(
        [
            {
                "run_date": datetime.now(JST).date(),
                **params.to_dict(),
            }
        ]
    )
    row.to_csv(hist_path, mode="a", header=not hist_path.exists(), index=False)

    print("[4/5] JSON出力...")
    prev = None
    if out_path.exists():
        try:
            prev = json.loads(out_path.read_text(encoding="utf-8"))
        except Exception:
            prev = None
    ctx = build_context_json(fitted, params, daily_price=px)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(ctx, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"      -> {out_path} (状態: {ctx['latest']['state']}, アラート{len(ctx['alerts'])}件)")

    print("[5/5] 通知判定...")
    maybe_notify_discord(ctx, prev)
    print("完了。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
