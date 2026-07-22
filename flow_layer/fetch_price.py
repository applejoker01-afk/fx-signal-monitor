# -*- coding: utf-8 -*-
"""日次 USD/JPY 終値の取得（多段フォールバック + キャッシュ）

優先順位:
  1. stooq        https://stooq.com/q/d/l/?s=usdjpy&i=d   （キー不要・日次OHLC）
  2. frankfurter  https://api.frankfurter.app              （ECB参照レート・キー不要）
  3. FRED DEXJPUS（既存の FRED_API_KEY を流用。H.10は公表ラグあり）
  4. ローカルキャッシュ data/usdjpy_daily.csv

週次モデルには火曜終値を merge_asof で割り当てるため、
どのソースでも「日次終値の系列」であれば整合する。
"""
from __future__ import annotations

import io
import os
from pathlib import Path

import pandas as pd
import requests

STOOQ_URL = "https://stooq.com/q/d/l/?s=usdjpy&i=d"
FRANKFURTER_URL = "https://api.frankfurter.app/{start}.."
FRED_URL = "https://api.stlouisfed.org/fred/series/observations"


def _from_stooq(timeout: int = 60) -> pd.Series:
    r = requests.get(STOOQ_URL, timeout=timeout)
    r.raise_for_status()
    df = pd.read_csv(io.StringIO(r.text))
    if "Close" not in df.columns or "Date" not in df.columns:
        raise RuntimeError(f"stooq応答が想定外です: columns={list(df.columns)[:6]}")
    s = pd.Series(
        pd.to_numeric(df["Close"], errors="coerce").to_numpy(),
        index=pd.to_datetime(df["Date"]),
        name="price",
    ).dropna()
    if len(s) < 100:
        raise RuntimeError("stooqの取得行数が少なすぎます")
    return s.sort_index()


def _from_frankfurter(start: str = "2005-01-01", timeout: int = 60) -> pd.Series:
    r = requests.get(
        FRANKFURTER_URL.format(start=start),
        params={"from": "USD", "to": "JPY"},
        timeout=timeout,
    )
    r.raise_for_status()
    rates = r.json().get("rates", {})
    if not rates:
        raise RuntimeError("frankfurter応答が空です")
    s = pd.Series(
        {pd.Timestamp(k): v.get("JPY") for k, v in rates.items()}, name="price"
    ).dropna()
    return s.sort_index()


def _from_fred(start: str = "2005-01-01", timeout: int = 60) -> pd.Series:
    key = os.environ.get("FRED_API_KEY")
    if not key:
        raise RuntimeError("FRED_API_KEY が未設定です")
    r = requests.get(
        FRED_URL,
        params={
            "series_id": "DEXJPUS",
            "api_key": key,
            "file_type": "json",
            "observation_start": start,
        },
        timeout=timeout,
    )
    r.raise_for_status()
    obs = r.json().get("observations", [])
    s = pd.Series(
        {
            pd.Timestamp(o["date"]): float(o["value"])
            for o in obs
            if o.get("value") not in (None, ".", "")
        },
        name="price",
    ).dropna()
    if s.empty:
        raise RuntimeError("FRED応答が空です")
    return s.sort_index()


def get_daily_usdjpy(cache_path: str | Path, start: str = "2005-01-01") -> pd.Series:
    """日次終値 Series（index=日付）。取得成功分はキャッシュにマージ保存。"""
    cache_path = Path(cache_path)
    cached = None
    if cache_path.exists():
        c = pd.read_csv(cache_path, parse_dates=["date"])
        cached = pd.Series(c["price"].to_numpy(), index=c["date"], name="price").sort_index()

    fresh, errors = None, []
    for fn in (_from_stooq, lambda: _from_frankfurter(start), lambda: _from_fred(start)):
        try:
            fresh = fn()
            break
        except Exception as e:
            errors.append(f"{getattr(fn, '__name__', 'fallback')}: {e}")

    if fresh is None and cached is None:
        raise RuntimeError("価格の取得もキャッシュ読込も失敗しました:\n" + "\n".join(errors))

    if fresh is not None and cached is not None:
        s = pd.concat([cached, fresh]).sort_index()
        s = s[~s.index.duplicated(keep="last")]
    else:
        s = fresh if fresh is not None else cached

    cache_path.parent.mkdir(parents=True, exist_ok=True)
    s.rename("price").rename_axis("date").reset_index().to_csv(cache_path, index=False)
    return s
