# -*- coding: utf-8 -*-
"""D-1: CFTC COT（建玉明細）取得モジュール

データソース: CFTC Public Reporting API (Socrata)
  Legacy - Futures Only : https://publicreporting.cftc.gov/resource/6dca-aqww.json

対象: JAPANESE YEN - CHICAGO MERCANTILE EXCHANGE (contract code 097741)
  net = 投機筋(noncommercial)ロング - ショート [枚]
  1枚 = 12,500,000円 → 想定元本[億円] = net × 0.125

point-in-time 規律:
  report_date (火曜集計) に対し release_date = 報告日+3営業日 (通常は金曜15:30 ET) を付与。
  ライブ運用・バックテストとも release_date 以降にのみその行を使用すること。
  ※ 米祝日週は公表が月曜等にずれることがある（保守的に扱うなら+5営業日）。

環境変数:
  CFTC_APP_TOKEN : 任意。Socrata アプリトークン（無くても週次1回なら実用上問題なし）
"""
from __future__ import annotations

import io
import os
from pathlib import Path

import numpy as np
import pandas as pd
import requests

SOCRATA_URL = "https://publicreporting.cftc.gov/resource/6dca-aqww.json"
JPY_CODE = "097741"  # JAPANESE YEN - CME

# 将来の多通貨展開用（コードは初回取得時に market_and_exchange_names で要確認）
CURRENCY_CODES_TODO = {
    "EUR": "099741",  # EURO FX（要確認）
    "GBP": "096742",  # BRITISH POUND（要確認）
    "AUD": "232741",  # AUSTRALIAN DOLLAR（要確認）
    "CAD": "090741",  # CANADIAN DOLLAR（要確認）
    "CHF": "092741",  # SWISS FRANC（要確認）
    "MXN": "095741",  # MEXICAN PESO（要確認）
}

CONTRACT_YEN = 12_500_000  # 円/枚


def fetch_cot_jpy(since: str = "2005-01-01", timeout: int = 60) -> pd.DataFrame:
    """Socrata API から JPY 先物の Legacy COT を取得して整形して返す。"""
    params = {
        "cftc_contract_market_code": JPY_CODE,
        "$select": ",".join(
            [
                "report_date_as_yyyy_mm_dd",
                "market_and_exchange_names",
                "noncomm_positions_long_all",
                "noncomm_positions_short_all",
                "open_interest_all",
            ]
        ),
        "$where": f"report_date_as_yyyy_mm_dd >= '{since}'",
        "$order": "report_date_as_yyyy_mm_dd",
        "$limit": "50000",
    }
    headers = {}
    token = os.environ.get("CFTC_APP_TOKEN")
    if token:
        headers["X-App-Token"] = token

    resp = requests.get(SOCRATA_URL, params=params, headers=headers, timeout=timeout)
    resp.raise_for_status()
    rows = resp.json()

    if not rows:
        # コード変更に備えた名称フォールバック
        params.pop("cftc_contract_market_code", None)
        params["$where"] = (
            f"report_date_as_yyyy_mm_dd >= '{since}' AND "
            "starts_with(market_and_exchange_names, 'JAPANESE YEN')"
        )
        resp = requests.get(SOCRATA_URL, params=params, headers=headers, timeout=timeout)
        resp.raise_for_status()
        rows = resp.json()
        if not rows:
            raise RuntimeError(
                "CFTC API から JPY の COT が取得できませんでした。"
                "contract code / データセットIDの変更を確認してください。"
            )

    df = pd.DataFrame(rows)
    df["report_date"] = pd.to_datetime(df["report_date_as_yyyy_mm_dd"]).dt.normalize()
    for c in ("noncomm_positions_long_all", "noncomm_positions_short_all", "open_interest_all"):
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df = (
        df.dropna(subset=["report_date"])
        .sort_values("report_date")
        .drop_duplicates("report_date", keep="last")
        .reset_index(drop=True)
    )
    out = pd.DataFrame(
        {
            "report_date": df["report_date"],
            "spec_long": df["noncomm_positions_long_all"],
            "spec_short": df["noncomm_positions_short_all"],
            "open_interest": df["open_interest_all"],
        }
    )
    out["net"] = out["spec_long"] - out["spec_short"]  # +なら投機筋は円ロング
    out["release_date"] = out["report_date"] + pd.offsets.BDay(3)  # 通常は金曜
    return out


def load_or_update_cot(cache_path: str | Path, since: str = "2005-01-01") -> pd.DataFrame:
    """キャッシュCSVとAPI取得をマージ（APIが落ちてもキャッシュで継続可能）。"""
    cache_path = Path(cache_path)
    cached = None
    if cache_path.exists():
        cached = pd.read_csv(cache_path, parse_dates=["report_date", "release_date"])

    fresh = None
    err = None
    try:
        fresh = fetch_cot_jpy(since=since)
    except Exception as e:  # ネットワーク断でも既存キャッシュで続行
        err = e

    if fresh is None and cached is None:
        raise RuntimeError(f"COTの取得もキャッシュ読込も失敗しました: {err}")

    if fresh is not None and cached is not None:
        df = (
            pd.concat([cached, fresh], ignore_index=True)
            .sort_values("report_date")
            .drop_duplicates("report_date", keep="last")
            .reset_index(drop=True)
        )
    else:
        df = (fresh if fresh is not None else cached).copy()

    cache_path.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(cache_path, index=False)
    return df


def add_flow_features(cot: pd.DataFrame) -> pd.DataFrame:
    """ポジショニング/フロー特徴量を付与する。

    net_z52 : ネットポジション水準の52週z（ポジション偏りゲージ）
              net_z52 <= -2 → 投機筋の円ショート過熱（巻き戻しリスク）
    d_net   : 週次変化[枚]
    flow_x  : 標準化フロー入力 = z156(-d_net)。＋ = 円売りフロー強度。
    """
    d = cot.sort_values("report_date").reset_index(drop=True).copy()
    net = d["net"].astype(float)

    roll52 = net.rolling(52, min_periods=26)
    sd52 = roll52.std(ddof=0).replace(0.0, np.nan)
    d["net_z52"] = (net - roll52.mean()) / sd52

    d["d_net"] = net.diff()
    yen_sell = -d["d_net"]  # ネット減少 = 円売りフロー
    roll156 = yen_sell.rolling(156, min_periods=52)
    sd156 = roll156.std(ddof=0).replace(0.0, np.nan)
    d["flow_x"] = (yen_sell - roll156.mean()) / sd156

    d["net_notional_oku_yen"] = net * (CONTRACT_YEN / 1e8)
    return d
