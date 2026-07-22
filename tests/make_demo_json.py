# -*- coding: utf-8 -*-
"""デモ用 flow_context.json 生成（オフラインでパネル表示を確認するため）

合成した「COT風」データと価格系列に実際のモデルコード（run_model /
build_context_json）を通し、docs/data/flow_context.json を書き出す。
実データ配線前にパネルの見た目と JSON 契約を確認できる。

実行: python tests/make_demo_json.py
表示: cd docs && python -m http.server 8000 → http://localhost:8000/flow_panel.html
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from flow_layer.fetch_cot import CONTRACT_YEN
from flow_layer.kalman_fv import run_model
from flow_layer.run_flow_layer import build_context_json


def main() -> None:
    rng = np.random.default_rng(20260722)
    T = 300
    idx = pd.date_range(end="2026-07-14", periods=T, freq="W-TUE")

    # COT風: 円ショートへドリフトするネットポジション（枚）
    d_net = rng.normal(-150, 6500, size=T)
    d_net[-30:] += -3000  # 直近はキャリー積み増し局面
    net = np.cumsum(d_net)
    net = np.clip(net, -200000, 90000)

    # フロー入力（本番と同じ定義: z156(-Δnet)）
    yen_sell = pd.Series(-pd.Series(net).diff().to_numpy(), index=idx)
    r156 = yen_sell.rolling(156, min_periods=52)
    flow_x = ((yen_sell - r156.mean()) / r156.std(ddof=0)).to_numpy()

    # 価格: フロー駆動FV + ノイズ、直近8週にフロー外の上振れを注入
    beta, sq, sr = 0.0035, 0.0045, 0.0040
    F = np.empty(T)
    F[0] = np.log(118.0)
    x_clean = np.nan_to_num(flow_x)
    for t in range(1, T):
        F[t] = F[t - 1] + beta * x_clean[t] + rng.normal(0, sq)
    p = F + rng.normal(0, sr, size=T)
    p[-8:] += np.linspace(0.006, 0.055, 8)  # 「イベント駆動の未説明上昇」
    price = np.exp(p) * (162.5 / np.exp(p[-1]))  # 末尾を現実的な水準に正規化

    weekly = pd.DataFrame(
        {
            "price": price,
            "flow_x": flow_x,
            "net": net.round(0),
            "net_notional_oku_yen": net * (CONTRACT_YEN / 1e8),
            "release_date": idx + pd.offsets.BDay(3),
        },
        index=idx,
    )
    r52 = pd.Series(net, index=idx).rolling(52, min_periods=26)
    weekly["net_z52"] = (weekly["net"] - r52.mean()) / r52.std(ddof=0).replace(0.0, np.nan)

    fitted, params = run_model(weekly)

    daily = pd.Series([163.005], index=[pd.Timestamp("2026-07-22")], name="price")
    ctx = build_context_json(fitted, params, daily_price=daily, demo=True)

    out = ROOT / "docs" / "data" / "flow_context.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    import json

    out.write_text(json.dumps(ctx, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"デモJSONを書き出しました: {out}")
    print(
        f"  状態={ctx['latest']['state']} / cum13_z={ctx['latest']['cum13_z']} / "
        f"net_z52={ctx['latest']['net_z52']} / アラート{len(ctx['alerts'])}件"
    )
    for a in ctx["alerts"]:
        print(f"  ⚠ {a}")


if __name__ == "__main__":
    main()
