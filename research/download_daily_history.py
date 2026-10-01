"""Read-only public Dukascopy candles for EXP-005; no account/API credentials.

The annual native candle format is LZMA, 24-byte big-endian records:
seconds since Jan 1, open, close, low, high, float volume. JPY point=1/1000,
other FX point=1/100000. Strict OHLC checks fail on bad decoding/data.
"""

import argparse
import hashlib
import json
import lzma
import math
import struct
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CACHE = ROOT / "research" / "cache" / "dukascopy_daily_2016_2025"
PAIRS = ["USDJPY", "EURUSD", "GBPJPY", "AUDJPY", "EURJPY", "AUDUSD",
         "GBPUSD", "USDCAD", "USDCHF", "NZDJPY"]
BASE = "https://www.dukascopy.com/datafeed/"
MIRROR = "https://datafeed.dukascopy.com/datafeed/"


def sha256(raw):
    return hashlib.sha256(raw).hexdigest()


def decode_year(raw, pair, year):
    decoded = lzma.decompress(raw)
    if len(decoded) % 24 or not decoded:
        raise ValueError("Invalid native-candle record size")
    point = 1000 if pair.endswith("JPY") else 100000
    start = datetime(year, 1, 1, tzinfo=timezone.utc)
    rows, excluded = [], {"weekend": 0, "zero_volume": 0}
    previous = -1
    for seconds, opened, closed, low, high, volume in struct.iter_unpack(">5if", decoded):
        when = start + timedelta(seconds=seconds)
        if when.year != year or seconds % 86400 or seconds <= previous:
            raise ValueError("Invalid/duplicate UTC daily timestamp")
        previous = seconds
        if not math.isfinite(volume) or volume < 0:
            raise ValueError("Invalid candle volume")
        if when.weekday() >= 5:
            excluded["weekend"] += 1
            continue
        if volume == 0:
            excluded["zero_volume"] += 1
            continue
        if not (0 < low <= min(opened, closed) <= max(opened, closed) <= high):
            raise ValueError("Invalid OHLC geometry")
        rows.append({"date": when.date().isoformat(), "open": opened / point,
                     "high": high / point, "low": low / point, "close": closed / point})
    return rows, excluded


def get_file(pair, year, side):
    relative = f"{pair}/{year}/{side}_candles_day_1.bi5"
    target = CACHE / "raw" / pair / str(year) / f"{side}.bi5"
    meta_path = target.with_suffix(".source.json")
    url = BASE + relative
    if target.exists() and meta_path.exists():
        raw = target.read_bytes()
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        if meta.get("url") not in (url, MIRROR + relative) or meta.get("sha256") != sha256(raw):
            raise ValueError(f"Cache integrity failed: {relative}")
    else:
        for attempt in range(3):
            # Same bank/data, alternate verified public distribution host.
            url = (BASE if attempt % 2 == 0 else MIRROR) + relative
            request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            try:
                with urllib.request.urlopen(request, timeout=20) as response:
                    raw = response.read()
                break
            except urllib.error.HTTPError as error:
                if error.code not in (429, 500, 502, 503, 504) or attempt == 2:
                    raise
                time.sleep(2 * (attempt + 1))
            except (urllib.error.URLError, TimeoutError):
                if attempt == 2:
                    raise
                time.sleep(2 * (attempt + 1))
        meta = {"url": url, "retrieved_at": datetime.now(timezone.utc).isoformat(),
                "sha256": sha256(raw), "bytes": len(raw)}
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(raw)
        meta_path.write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")
    rows, excluded = decode_year(raw, pair, year)
    return pair, year, side, rows, {**meta, "pair": pair, "year": year,
                                  "side": side, "retained": len(rows), "excluded": excluded}


def collect():
    tasks = [(pair, year, side) for pair in PAIRS for year in range(2016, 2026)
             for side in ("BID", "ASK")]
    all_rows, sources, failures = {}, [], []
    with ThreadPoolExecutor(max_workers=4) as executor:
        futures = {executor.submit(get_file, *task): task for task in tasks}
        for count, future in enumerate(as_completed(futures), 1):
            try:
                pair, year, side, rows, source = future.result()
                all_rows.setdefault(pair, {}).setdefault(side, []).extend(rows)
                sources.append(source)
            except Exception as error:
                failures.append({"task": futures[future], "error": str(error)})
                if len(failures) <= 3:
                    print(f"failed {futures[future]}: {error}", flush=True)
            if count % 20 == 0 or count == len(tasks):
                print(f"files {count}/{len(tasks)}; errors {len(failures)}", flush=True)
    if failures:
        CACHE.mkdir(parents=True, exist_ok=True)
        (CACHE / "download_failures.json").write_text(json.dumps(failures, indent=2) + "\n", encoding="utf-8")
        raise RuntimeError(json.dumps(failures, ensure_ascii=False))
    results, qc = {}, {}
    for pair, sides in all_rows.items():
        bid = {row["date"]: row for row in sides["BID"]}
        ask = {row["date"]: row for row in sides["ASK"]}
        dates = sorted(bid.keys() & ask.keys())
        rows = []
        for date in dates:
            b, a = bid[date], ask[date]
            if any(a[key] < b[key] for key in ("open", "close")):
                raise ValueError(f"ASK below BID at open/close: {pair} {date}")
            row = {"date": date, **{key: (b[key] + a[key]) / 2
                                    for key in ("open", "high", "low", "close")}}
            row["spread_close_price"] = a["close"] - b["close"]
            rows.append(row)
        results[pair] = rows
        qc[pair] = {"rows": len(rows), "first": dates[0], "last": dates[-1],
                    "unmatched_dates": len(bid.keys() ^ ask.keys()),
                    "flat_bars": sum(row["high"] == row["low"] for row in rows)}
    payload = {"provider": "Dukascopy Bank SA", "price_basis": "BID/ASK OHLC arithmetic means",
               "results": {pair: results[pair] for pair in PAIRS}, "quality": qc}
    data_path = CACHE / "daily_mid.json"
    data_path.write_text(json.dumps(payload, sort_keys=True, allow_nan=False) + "\n", encoding="utf-8")
    manifest = {"data_sha256": sha256(data_path.read_bytes()), "quality": qc,
                "sources": sorted(sources, key=lambda x: (x["pair"], x["year"], x["side"]))}
    (CACHE / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(qc, ensure_ascii=False, indent=2), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args()
    collect()
