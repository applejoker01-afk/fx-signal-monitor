#!/usr/bin/env python3
"""
test_intraday_data.py
15分足・5分足データが安定取得できるか、GitHub Actions環境で検証する。

複数のデータソースを順に試し、どれが使えるかをDiscordとログに報告する。
このスクリプトはデータソース選定のための「調査専用」。
本番のデイトレシステムを作る前に、まずこれで取得可能性を確認する。

実行方法:
  Actions → Intraday Data Test → Run workflow
"""

import json
import os
import urllib.request
import time
from datetime import datetime, timezone, timedelta


def http_get(url, headers=None, timeout=15):
    req = urllib.request.Request(
        url, headers=headers or {"User-Agent": "Mozilla/5.0 (compatible; fx-monitor/1.0)"}
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8")


# テスト対象通貨ペア
TEST_PAIRS = {
    "USDJPY": {"yahoo": "USDJPY=X", "stooq": "usdjpy", "twelve": "USD/JPY"},
    "EURUSD": {"yahoo": "EURUSD=X", "stooq": "eurusd", "twelve": "EUR/USD"},
    "GBPJPY": {"yahoo": "GBPJPY=X", "stooq": "gbpjpy", "twelve": "GBP/JPY"},
    "AUDJPY": {"yahoo": "AUDJPY=X", "stooq": "audjpy", "twelve": "AUD/JPY"},
    "ZARJPY": {"yahoo": "ZARJPY=X", "stooq": "zarjpy", "twelve": "ZAR/JPY"},
}


def test_yahoo_intraday(interval="15m", rng="5d"):
    """Yahoo Finance の分足取得テスト"""
    print(f"\n{'='*55}")
    print(f"1. Yahoo Finance {interval}（range={rng}）")
    print(f"{'='*55}")
    ok, detail = 0, {}
    for pair, syms in TEST_PAIRS.items():
        sym = syms["yahoo"]
        try:
            url = (f"https://query1.finance.yahoo.com/v8/finance/chart/{sym}"
                   f"?interval={interval}&range={rng}")
            data = json.loads(http_get(url))
            result = data["chart"]["result"][0]
            ts = result.get("timestamp", [])
            closes = result["indicators"]["quote"][0].get("close", [])
            valid = [c for c in closes if c is not None]
            if valid:
                # 最新足の時刻
                last_dt = datetime.fromtimestamp(ts[-1], tz=timezone.utc) + timedelta(hours=9)
                print(f"  {pair:8} ✓ {len(valid)}本 "
                      f"直近={valid[-1]:.4f} ({last_dt.strftime('%m/%d %H:%M JST')})")
                ok += 1
                detail[pair] = {"bars": len(valid), "last": round(valid[-1], 4)}
            else:
                print(f"  {pair:8} △ データ空")
        except Exception as e:
            print(f"  {pair:8} ✗ {str(e)[:45]}")
        time.sleep(0.3)
    return ok, detail


def test_stooq_intraday(interval=5):
    """Stooq の分足取得テスト（i=分）"""
    print(f"\n{'='*55}")
    print(f"2. Stooq {interval}分足")
    print(f"{'='*55}")
    ok, detail = 0, {}
    for pair, syms in TEST_PAIRS.items():
        sym = syms["stooq"]
        try:
            url = f"https://stooq.com/q/d/l/?s={sym}&i={interval}"
            text = http_get(url)
            lines = text.strip().split("\n")
            if len(lines) > 2 and "Date" in lines[0]:
                print(f"  {pair:8} ✓ {len(lines)-1}本 最新行: {lines[-1][:50]}")
                ok += 1
                detail[pair] = {"bars": len(lines) - 1}
            else:
                print(f"  {pair:8} △ {text[:60]}")
        except Exception as e:
            print(f"  {pair:8} ✗ {str(e)[:45]}")
        time.sleep(0.5)
    return ok, detail


def test_twelvedata(interval="15min"):
    """Twelve Data の分足取得テスト（APIキー必要）"""
    print(f"\n{'='*55}")
    print(f"3. Twelve Data {interval}（要 TWELVE_DATA_API_KEY）")
    print(f"{'='*55}")
    api_key = os.environ.get("TWELVE_DATA_API_KEY", "")
    if not api_key:
        print("  → TWELVE_DATA_API_KEY 未設定のためスキップ")
        return 0, {}
    ok, detail = 0, {}
    for pair, syms in list(TEST_PAIRS.items())[:3]:  # 無料枠節約で3つだけ
        sym = syms["twelve"]
        try:
            url = (f"https://api.twelvedata.com/time_series"
                   f"?symbol={sym}&interval={interval}&outputsize=30&apikey={api_key}")
            data = json.loads(http_get(url))
            if "values" in data and data["values"]:
                latest = data["values"][0]
                print(f"  {pair:8} ✓ {len(data['values'])}本 "
                      f"直近={latest.get('close')} ({latest.get('datetime')})")
                ok += 1
                detail[pair] = {"bars": len(data["values"]), "last": latest.get("close")}
            else:
                print(f"  {pair:8} △ {str(data)[:60]}")
        except Exception as e:
            print(f"  {pair:8} ✗ {str(e)[:45]}")
        time.sleep(1.0)
    return ok, detail


def send_discord_report(results):
    webhook = os.environ.get("DISCORD_WEBHOOK_URL", "").replace("discordapp.com", "discord.com")
    if not webhook:
        return
    now_jst = datetime.now(timezone.utc) + timedelta(hours=9)

    lines = []
    for source, (ok, total) in results.items():
        mark = "✅" if ok == total and total > 0 else ("⚠" if ok > 0 else "❌")
        lines.append(f"{mark} {source}: {ok}/{total}ペア取得成功")

    # 推奨判定
    best = max(results.items(), key=lambda x: x[1][0])
    if best[1][0] > 0:
        recommendation = f"→ **{best[0]}** が最有力（{best[1][0]}/{best[1][1]}成功）"
    else:
        recommendation = "→ どのソースも失敗。設定見直しが必要"

    embeds = [{
        "title": "🔬 分足データ取得 検証結果",
        "description": "デイトレ用の分足データがどのソースから取れるかの調査結果です。",
        "color": 0x4ADE80 if best[1][0] > 0 else 0xF87171,
        "timestamp": now_jst.isoformat(),
        "fields": [
            {"name": "📊 ソース別取得可否", "value": "\n".join(lines), "inline": False},
            {"name": "💡 推奨", "value": recommendation, "inline": False},
        ],
        "footer": {"text": f"Currents FX | Intraday Test | {now_jst.strftime('%Y-%m-%d %H:%M JST')}"},
    }]
    try:
        payload = json.dumps({"embeds": embeds}).encode("utf-8")
        req = urllib.request.Request(
            webhook, data=payload,
            headers={"Content-Type": "application/json",
                     "User-Agent": "DiscordBot (fx-signal-monitor, 1.0)"},
            method="POST")
        with urllib.request.urlopen(req, timeout=15) as resp:
            print(f"\n[OK] Discord report sent (HTTP {resp.status})")
    except Exception as e:
        print(f"\n[ERROR] Discord report: {e}")


def main():
    print("=" * 55)
    print(f"分足データ取得検証 - {datetime.now(timezone.utc).isoformat()}")
    print("=" * 55)

    results = {}

    # Yahoo 15分足
    ok, _ = test_yahoo_intraday("15m", "5d")
    results["Yahoo 15分足"] = (ok, len(TEST_PAIRS))

    # Yahoo 5分足
    ok, _ = test_yahoo_intraday("5m", "1d")
    results["Yahoo 5分足"] = (ok, len(TEST_PAIRS))

    # Stooq 5分足
    ok, _ = test_stooq_intraday(5)
    results["Stooq 5分足"] = (ok, len(TEST_PAIRS))

    # Twelve Data
    ok, _ = test_twelvedata("15min")
    results["TwelveData 15分足"] = (ok, 3)

    # サマリー
    print("\n" + "=" * 55)
    print("検証結果サマリー")
    print("=" * 55)
    for source, (ok, total) in results.items():
        mark = "✅" if ok == total and total > 0 else ("⚠" if ok > 0 else "❌")
        print(f"  {mark} {source}: {ok}/{total}")

    send_discord_report(results)
    print("\n[OK] Done")


if __name__ == "__main__":
    main()
