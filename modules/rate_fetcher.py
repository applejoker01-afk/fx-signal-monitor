"""
金利・債券利回り取得モジュール
- 中央銀行政策金利（手動メンテのJSONから読込）
- 米国債利回り（U.S. Treasury Fiscal Data API）
- 金利差スコアの計算
"""

import json
import os
import urllib.request
from datetime import datetime, timezone, timedelta

CENTRAL_BANK_FILE = "data/central_bank_rates.json"

# FRED シリーズID（主要国の政策金利）
# 取得できた通貨はFREDの最新値で上書き、取れないものは手動JSON/固定値を使う
# 【方針】信頼できる米国の日次データ(USD)のみ自動化。
# 他国はOECD月次系列が遅延・誤値を返すため手動JSON/固定値を維持する。
FRED_RATE_SERIES = {
    "USD": "DFEDTARU",        # Fed Funds 目標上限（日次・信頼性高）
}


def fetch_fred_series_history(series_id, api_key, days=300):
    """
    FRED APIから指定シリーズの過去時系列を取得。
    返り値: {date_str: value, ...} の辞書（日付→値）。失敗時は空dict。
    例: DGS10（米10年債利回り）
    """
    from datetime import date
    api_key = (api_key or "").strip()
    start = (datetime.now(timezone.utc).date() -
             timedelta(days=days)).isoformat()
    url = (
        "https://api.stlouisfed.org/fred/series/observations"
        f"?series_id={series_id}&api_key={api_key}&file_type=json"
        f"&observation_start={start}&sort_order=asc"
    )
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "fx-signal-monitor/1.0"})
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        result = {}
        for obs in data.get("observations", []):
            v = obs.get("value")
            d = obs.get("date")
            if v not in (".", "", None) and d:
                try:
                    result[d] = float(v)
                except ValueError:
                    continue
        return result
    except urllib.error.HTTPError as e:
        try:
            body = e.read().decode("utf-8")[:200]
        except Exception:
            body = ""
        print(f"[WARN] FRED history {series_id} HTTP{e.code}: {body}")
        return {}
    except Exception as e:
        print(f"[WARN] FRED history {series_id} failed: {e}")
        return {}


def fetch_fred_rate(series_id, api_key):
    """FRED APIから指定シリーズの最新値を取得。失敗時None。"""
    # APIキーの前後の空白・改行を除去（Secret登録時の混入対策）
    api_key = (api_key or "").strip()
    url = (
        "https://api.stlouisfed.org/fred/series/observations"
        f"?series_id={series_id}&api_key={api_key}&file_type=json"
        "&sort_order=desc&limit=1"
    )
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "fx-signal-monitor/1.0"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        obs = data.get("observations", [])
        if obs and obs[0].get("value") not in (".", "", None):
            return float(obs[0]["value"])
    except urllib.error.HTTPError as e:
        # エラー本文を表示（原因特定のため）
        try:
            body = e.read().decode("utf-8")[:200]
        except Exception:
            body = ""
        print(f"[WARN] FRED {series_id} HTTP{e.code}: {body}")
    except Exception as e:
        print(f"[WARN] FRED {series_id} fetch failed: {e}")
    return None


def fetch_live_central_bank_rates(base_rates=None):
    """
    FRED APIから主要国の政策金利を自動取得し、手動JSON/固定値を上書きする。
    FRED APIキー（環境変数 FRED）が無い、または取得失敗した通貨は base_rates のまま。
    stance（利上げ/利下げ姿勢）は手動JSONの値を維持（FREDからは判定できないため）。
    """
    if base_rates is None:
        base_rates = load_central_bank_rates()
    rates = {k: dict(v) for k, v in base_rates.items()}  # メタ情報を保持

    api_key = os.environ.get("FRED_API_KEY") or os.environ.get("FRED")
    if not api_key:
        print("[INFO] FRED APIキー未設定。手動の金利データを使用します")
        return rates

    updated = []
    for ccy, series_id in FRED_RATE_SERIES.items():
        live = fetch_fred_rate(series_id, api_key)
        if live is not None:
            old = rates.get(ccy, {}).get("rate")
            if ccy not in rates:
                rates[ccy] = {"stance": "neutral", "cb_name": ccy}
            rates[ccy]["rate"] = round(live, 2)
            rates[ccy]["rate_source"] = "FRED"
            if old is not None and abs(old - live) >= 0.01:
                updated.append(f"{ccy}:{old}→{live}%")
            else:
                updated.append(f"{ccy}:{live}%")
    if updated:
        print(f"[INFO] FRED金利自動取得: {', '.join(updated)}")
    return rates


def check_stance_consistency(rates, snapshot_file="docs/rates_snapshot.json"):
    """
    FRED金利の変化と手動stanceの矛盾を検知する。
    前回スナップショットと比較し、金利が動いたのにstanceが整合しない通貨を警告。

    矛盾の例:
      stance="tighten"（利上げ中）なのに金利が下がった
      stance="ease"（利下げ中）なのに金利が上がった

    返り値: {
      "warnings": [{ccy, stance, old_rate, new_rate, message}],
      "checked_at": iso,
      "rates": {ccy: rate},
    }
    """
    import json as _json
    # 前回スナップショット読込
    prev = {}
    try:
        if os.path.exists(snapshot_file):
            with open(snapshot_file, "r", encoding="utf-8") as f:
                prev = _json.load(f).get("rates", {})
    except Exception:
        prev = {}

    warnings = []
    current = {}
    for ccy, info in rates.items():
        rate = info.get("rate")
        stance = info.get("stance", "neutral")
        if rate is None:
            continue
        current[ccy] = rate
        old = prev.get(ccy)
        if old is None:
            continue
        delta = rate - old
        # 金利が有意に動いた（±0.05%以上）場合のみ判定
        if abs(delta) < 0.05:
            continue
        cb = info.get("cb_name", ccy)
        if delta < 0 and stance == "tighten":
            warnings.append({
                "ccy": ccy, "stance": stance, "old_rate": old, "new_rate": rate,
                "message": f"{cb}({ccy}): 金利が{old}%→{rate}%に低下したのに stance=tighten（利上げ中）。利下げ転換の可能性。stanceの見直しを。",
            })
        elif delta > 0 and stance == "ease":
            warnings.append({
                "ccy": ccy, "stance": stance, "old_rate": old, "new_rate": rate,
                "message": f"{cb}({ccy}): 金利が{old}%→{rate}%に上昇したのに stance=ease（利下げ中）。利上げ転換の可能性。stanceの見直しを。",
            })

    warnings.extend(check_central_bank_data_staleness())

    return {
        "warnings": warnings,
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "rates": current,
    }


def check_central_bank_data_staleness(max_age_days: int = 45) -> list:
    """
    central_bank_rates.json の last_updated が古すぎないか検知する（2026-07-21追加）。

    背景: 2026-07-20にAUD/NZDのstance誤り（実際には発生していない利下げを前提と
    していた）を手動訂正したが、その後の別コミットとのマージで訂正が silent に
    元へ巻き戻り、システムは古い誤データのまま数日間シグナル生成を続けていた
    （2026-07-21判明・訂正2回目）。他通貨の政策金利はFRED等での自動取得対象外
    （手動メンテ）のため、更新が止まっても気づく仕組みがなかった。
    このチェックはstance/rateの中身までは検証しないが、「最終更新から
    max_age_days日以上経過」を検知して通知することで、今回のような巻き戻りや
    更新忘れが長期間気づかれないリスクを減らす。
    """
    import json as _json
    if not os.path.exists(CENTRAL_BANK_FILE):
        return []
    try:
        with open(CENTRAL_BANK_FILE, "r", encoding="utf-8") as f:
            data = _json.load(f)
        last_updated_str = data.get("last_updated")
        if not last_updated_str:
            return []
        last_updated = datetime.fromisoformat(last_updated_str).replace(tzinfo=timezone.utc)
        age_days = (datetime.now(timezone.utc) - last_updated).days
        if age_days >= max_age_days:
            return [{
                "ccy": "ALL",
                "stance": None,
                "message": (
                    f"central_bank_rates.json の最終更新が{age_days}日前"
                    f"（{last_updated_str}）と古く、fa_score算出に使われる各国政策金利・"
                    f"stanceが実態と乖離している可能性があります。中銀会合の結果を確認し、"
                    f"手動更新を検討してください。"
                ),
            }]
    except Exception as e:
        print(f"[WARN] central_bank_rates.json 鮮度チェック失敗: {e}")
    return []


def save_rates_snapshot(rates, snapshot_file="docs/rates_snapshot.json"):
    """現在の金利をスナップショットとして保存（次回の比較用）"""
    import json as _json
    snap = {
        "saved_at": datetime.now(timezone.utc).isoformat(),
        "rates": {ccy: info.get("rate") for ccy, info in rates.items()
                  if info.get("rate") is not None},
    }
    try:
        os.makedirs(os.path.dirname(snapshot_file), exist_ok=True)
        with open(snapshot_file, "w", encoding="utf-8") as f:
            _json.dump(snap, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"[WARN] スナップショット保存失敗: {e}")


def load_central_bank_rates():
    """JSONファイルから中央銀行政策金利を読込"""
    if not os.path.exists(CENTRAL_BANK_FILE):
        print(f"[WARN] {CENTRAL_BANK_FILE} not found, using fallback")
        return _fallback_rates()
    try:
        with open(CENTRAL_BANK_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data.get("rates", {})
    except Exception as e:
        print(f"[WARN] Failed to load central bank rates: {e}")
        return _fallback_rates()


def _fallback_rates():
    """
    FRED自動取得できない通貨の手動フォールバック（2026年6月時点）。
    USDはFRED(DFEDTARU)で自動上書きされるため、ここの値は参考。
    他通貨は手動メンテ。中銀の利上げ/利下げ時はこのstanceとrateを更新すること。
    rate_momentum: accelerating/decelerating/peak/trough/stable
      accelerating = 利上げ/利下げが加速中（stance方向が強まる）
      decelerating  = 利上げ/利下げが鈍化中（ピーク/底近し）
      peak          = 利上げサイクルの天井（次は利下げ）
      trough        = 利下げサイクルの底（次は利上げ）
      stable        = 方向感なし・中立
    """
    return {
        "USD": {"rate": 3.75, "stance": "neutral", "rate_momentum": "stable",
                "cb_name": "FRB"},    # higher-for-longer pause
        "EUR": {"rate": 2.15, "stance": "ease", "rate_momentum": "decelerating",
                "cb_name": "ECB"},    # 利下げサイクル後半・ペース鈍化
        "JPY": {"rate": 1.00, "stance": "pause", "rate_momentum": "peak",
                "cb_name": "日銀"},   # 2026-06-17 利上げ決定(0.75→1.0%)。次は様子見・ピーク到達
        "GBP": {"rate": 3.75, "stance": "neutral", "rate_momentum": "stable",
                "cb_name": "BOE"},    # データ依存・中立
        "AUD": {"rate": 4.10, "stance": "ease", "rate_momentum": "stable",
                "cb_name": "RBA"},    # 2026-06-03 4.35→4.10に引き下げ
        "NZD": {"rate": 3.25, "stance": "ease", "rate_momentum": "decelerating",
                "cb_name": "RBNZ"},   # 利下げサイクル後半
        "CAD": {"rate": 2.75, "stance": "neutral", "rate_momentum": "stable",
                "cb_name": "BOC"},    # 据え置き
        "CHF": {"rate": 0.25, "stance": "ease", "rate_momentum": "trough",
                "cb_name": "SNB"},    # ゼロ金利接近・底打ち近し
        "SGD": {"rate": 2.50, "stance": "neutral", "rate_momentum": "stable",
                "cb_name": "MAS"},
        "HKD": {"rate": 3.75, "stance": "neutral", "rate_momentum": "stable",
                "cb_name": "HKMA"},   # USDペッグ
        "CNY": {"rate": 3.00, "stance": "ease", "rate_momentum": "stable",
                "cb_name": "PBOC"},
        "MXN": {"rate": 9.00, "stance": "ease", "rate_momentum": "decelerating",
                "cb_name": "Banxico"},
        "TRY": {"rate": 46.00, "stance": "ease", "rate_momentum": "decelerating",
                "cb_name": "CBRT"},
        "ZAR": {"rate": 7.25, "stance": "neutral", "rate_momentum": "stable",
                "cb_name": "SARB"},
        "INR": {"rate": 5.50, "stance": "ease", "rate_momentum": "stable",
                "cb_name": "RBI"},
    }


def fetch_us_treasury_yields():
    """
    米国財務省のFiscalデータAPIから米国債利回りを取得。
    APIキー不要・無制限。
    返り値: {"2y": 4.85, "5y": 4.60, "10y": 4.35, "30y": 4.55, "spread_10y2y": -0.50}
    """
    today = datetime.now(timezone.utc).date()
    # 過去14日のレンジで最新値を取得（土日祝で空く可能性を考慮）
    start_date = today - timedelta(days=14)
    url = (
        "https://api.fiscaldata.treasury.gov/services/api/fiscal_service/"
        "v2/accounting/od/avg_interest_rates"
        f"?filter=record_date:gte:{start_date}"
        "&sort=-record_date"
        "&page[size]=100"
    )
    try:
        req = urllib.request.Request(
            url, headers={"User-Agent": "fx-signal-monitor/1.0"}
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        if not data.get("data"):
            print("[WARN] US Treasury API returned no data")
            return _fallback_yields()

        # security_descに「Treasury Notes」「Treasury Bonds」等が含まれる行から取得
        yields = {}
        for row in data["data"]:
            desc = row.get("security_desc", "").lower()
            try:
                rate = float(row.get("avg_interest_rate_amt", 0))
            except (ValueError, TypeError):
                continue
            if "treasury notes" in desc and "2y" not in yields:
                yields["10y"] = rate  # 国債総合（10年に近似）
            elif "treasury bonds" in desc and "30y" not in yields:
                yields["30y"] = rate
            elif "treasury bills" in desc and "short" not in yields:
                yields["short"] = rate

        if not yields:
            return _fallback_yields()

        # Estimateで埋める
        result = {
            "short": yields.get("short", 5.00),
            "2y": yields.get("short", 5.00) - 0.15,
            "10y": yields.get("10y", 4.35),
            "30y": yields.get("30y", 4.55),
        }
        result["spread_10y2y"] = result["10y"] - result["2y"]
        result["source"] = "U.S. Treasury Fiscal Data API"
        return result
    except Exception as e:
        print(f"[WARN] US Treasury fetch failed: {e}")
        return _fallback_yields()


def _fallback_yields():
    """API失敗時のフォールバック（2026年5月時点）"""
    return {
        "short": 4.85, "2y": 4.85, "10y": 4.35, "30y": 4.55,
        "spread_10y2y": -0.50,
        "source": "fallback (estimated)",
    }


# ---------------------------------------------------------------------------
# 金利差ベースのFAスコア計算（添付資料に基づく）
# ---------------------------------------------------------------------------

def _rate_momentum_bonus(stance: str, momentum: str) -> float:
    """
    金利サイクルのモメンタムによる追加ボーナス（-8〜+8）。
    プラス = その通貨が強くなる方向（buy FROM / sell TO で加算）。

    accelerating + tighten = 利上げ加速 → その通貨が強くなる確実性高 → +8
    decelerating + tighten = 利上げ鈍化 → ピーク近し → -4（やや弱まる）
    peak                   = 利上げ頂点 → 次は利下げ → -6
    trough                 = 利下げ底  → 次は利上げ → +6
    accelerating + ease    = 利下げ加速 → その通貨が弱くなる確実性高 → -8
    decelerating + ease    = 利下げ鈍化 → 底打ち近し → +4
    stable / その他        = 0
    """
    if momentum == "accelerating":
        return 8.0 if stance == "tighten" else (-8.0 if stance == "ease" else 0.0)
    elif momentum == "decelerating":
        return -4.0 if stance == "tighten" else (4.0 if stance == "ease" else 0.0)
    elif momentum == "peak":
        return -6.0
    elif momentum == "trough":
        return 6.0
    return 0.0


def compute_fa_score(pair, pair_api, central_bank_rates, bond_trend=None):
    """
    金利差・中銀スタンス・金利サイクルモメンタムから動的にFAスコアを算出。
    bond_trend: 米10年債の直近トレンド（"up"/"down"/None）。
                USD絡みペアのみFAスコアを±5点補正する。
    rate_momentum: accelerating/decelerating/peak/trough/stable。
                  金利サイクルの「勢い」を±8点で反映。
    返り値: dict (score: 0-100, direction: buy/sell/neutral, detail: str, rate_diff: float)
    """
    from_ccy, to_ccy = pair_api[pair]
    rate_from = central_bank_rates.get(from_ccy, {}).get("rate")
    rate_to = central_bank_rates.get(to_ccy, {}).get("rate")
    stance_from = central_bank_rates.get(from_ccy, {}).get("stance", "neutral")
    stance_to = central_bank_rates.get(to_ccy, {}).get("stance", "neutral")
    # 2026-06-11: 金利サイクルモメンタム（研究D反映）
    momentum_from = central_bank_rates.get(from_ccy, {}).get("rate_momentum", "stable")
    momentum_to = central_bank_rates.get(to_ccy, {}).get("rate_momentum", "stable")

    if rate_from is None or rate_to is None:
        return {
            "score": 50.0,
            "direction": "neutral",
            "rate_diff": None,
            "detail": "金利データ取得失敗",
            "stance_from": stance_from,
            "stance_to": stance_to,
        }

    rate_diff = rate_from - rate_to  # FROM買い・TO売りした際の年間スワップ相当

    # 基本スコア: 金利差の絶対値（最大±30点）
    diff_magnitude = min(abs(rate_diff) * 7.5, 30)
    diff_score = diff_magnitude if rate_diff > 0 else -diff_magnitude

    # スタンスダイバージェンスボーナス（金利差が将来も拡大か縮小か）
    stance_bonus = 0
    if rate_diff > 0:
        if stance_from == "tighten" and stance_to == "ease":
            stance_bonus = 15
        elif stance_from == "ease":
            stance_bonus = -10
        elif stance_from == "tighten":
            stance_bonus = 5
    elif rate_diff < 0:
        if stance_from == "ease" and stance_to == "tighten":
            stance_bonus = -15
        elif stance_from == "tighten":
            stance_bonus = 10

    # 債券補正（米10年債トレンド・USD絡みペアのみ）
    bond_bonus = 0
    bond_note = ""
    if bond_trend in ("up", "down"):
        if from_ccy == "USD":
            # USDがFROM（USDロング方向）: 利回り上昇でUSD買い+
            bond_bonus = 5 if bond_trend == "up" else -5
            bond_note = f" / 米10年債{('↑' if bond_trend=='up' else '↓')}"
        elif to_ccy == "USD":
            # USDがTO（USDショート方向）: 利回り上昇はFROM売り方向-
            bond_bonus = -5 if bond_trend == "up" else 5
            bond_note = f" / 米10年債{('↑' if bond_trend=='up' else '↓')}"

    # 金利サイクルモメンタム補正（2026-06-11 研究D反映）
    # FROM通貨のモメンタム（プラス = FROM強い = 買い方向）
    # TO通貨のモメンタム（プラス = TO強い = 売り方向 = スコア減算）
    mb_from = _rate_momentum_bonus(stance_from, momentum_from)
    mb_to = _rate_momentum_bonus(stance_to, momentum_to)
    momentum_bonus = mb_from - mb_to  # net: FROM有利 - TO有利
    momentum_note = ""
    if abs(momentum_bonus) >= 4:
        if momentum_bonus > 0:
            momentum_note = f" / {from_ccy}サイクル強化({momentum_from})"
        else:
            momentum_note = f" / {to_ccy}サイクル強化({momentum_to})"

    # 最終スコア（50を中立基準）
    final_score = 50 + diff_score + stance_bonus + bond_bonus + momentum_bonus
    final_score = max(0, min(100, final_score))

    if final_score >= 60:
        direction = "buy"
    elif final_score <= 40:
        direction = "sell"
    else:
        direction = "neutral"

    cb_from = central_bank_rates.get(from_ccy, {}).get("cb_name", from_ccy)
    cb_to = central_bank_rates.get(to_ccy, {}).get("cb_name", to_ccy)
    detail = (
        f"{cb_from}({rate_from:.2f}% {stance_from}/{momentum_from}) vs "
        f"{cb_to}({rate_to:.2f}% {stance_to}/{momentum_to}) "
        f"差{rate_diff:+.2f}%{bond_note}{momentum_note}"
    )

    return {
        "score": round(final_score, 1),
        "direction": direction,
        "rate_diff": round(rate_diff, 2),
        "detail": detail,
        "rate_from": rate_from,
        "rate_to": rate_to,
        "stance_from": stance_from,
        "stance_to": stance_to,
        "momentum_from": momentum_from,
        "momentum_to": momentum_to,
        "momentum_bonus": round(momentum_bonus, 1),
        "cb_from": cb_from,
        "cb_to": cb_to,
    }


def generate_rates_html(rates, consistency, out_file="docs/rates.html"):
    """
    金利モニターHTMLを生成。各通貨の金利・stance・矛盾警告を一覧表示。
    矛盾があれば赤字で目立たせる。
    """
    import json as _json
    warnings = consistency.get("warnings", [])
    warn_ccy = {w["ccy"]: w for w in warnings}
    checked = consistency.get("checked_at", "")[:19].replace("T", " ")

    # stance表示の日本語化
    stance_jp = {"tighten": "利上げ", "ease": "利下げ", "neutral": "中立"}
    stance_color = {"tighten": "#4ade80", "ease": "#f87171", "neutral": "#9ca3af"}

    # 警告バナー
    if warnings:
        warn_items = "".join(
            f'<div class="warn-item">⚠ {w["message"]}</div>' for w in warnings
        )
        warn_banner = f'''
        <div class="warn-banner">
          <div class="warn-title">🔴 stance（金利スタンス）の見直しが必要です（{len(warnings)}件）</div>
          {warn_items}
          <div class="warn-note">data/central_bank_rates.json の stance を更新してください。</div>
        </div>'''
    else:
        warn_banner = '<div class="ok-banner">✅ 金利スタンスに矛盾は検知されていません</div>'

    # 金利テーブル行
    rows = ""
    for ccy in sorted(rates.keys()):
        info = rates[ccy]
        rate = info.get("rate")
        if rate is None:
            continue
        stance = info.get("stance", "neutral")
        cb = info.get("cb_name", ccy)
        src = info.get("rate_source", "手動")
        has_warn = ccy in warn_ccy
        row_cls = "warn-row" if has_warn else ""
        rate_cls = "rate-warn" if has_warn else "rate-val"
        warn_mark = "🔴" if has_warn else ""
        rows += f'''
        <tr class="{row_cls}">
          <td class="ccy">{ccy} {warn_mark}</td>
          <td>{cb}</td>
          <td class="{rate_cls}">{rate:.2f}%</td>
          <td style="color:{stance_color.get(stance,'#9ca3af')};">{stance_jp.get(stance, stance)}</td>
          <td class="src">{src}</td>
        </tr>'''

    html = f'''<!DOCTYPE html>
<html lang="ja"><head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>金利モニター · Currents FX</title>
<style>
:root{{--bg:#0a0e14;--panel:#131820;--border:#1f2937;--gold:#d4a574;--text:#e5e7eb;--muted:#9ca3af;--sell:#f87171;}}
*{{box-sizing:border-box;margin:0;padding:0;}}
body{{background:var(--bg);color:var(--text);font-family:'Zen Kaku Gothic New',sans-serif;padding:1.5rem;max-width:900px;margin:0 auto;}}
h1{{font-family:'Cormorant Garamond',serif;font-size:1.8rem;color:var(--gold);margin-bottom:0.3rem;}}
.sub{{color:var(--muted);font-size:0.8rem;margin-bottom:1.2rem;}}
.top-nav{{display:flex;gap:0.4rem;flex-wrap:wrap;margin-bottom:1.2rem;}}
.top-nav a{{font-family:monospace;font-size:0.74rem;color:var(--muted);text-decoration:none;padding:0.3rem 0.6rem;border:1px solid var(--border);border-radius:4px;}}
.top-nav a:hover{{border-color:var(--gold);color:var(--gold);}}
.top-nav a.active{{background:var(--gold);color:var(--bg);font-weight:700;}}
.warn-banner{{background:rgba(248,113,113,0.1);border:2px solid var(--sell);border-radius:8px;padding:1rem;margin-bottom:1.2rem;}}
.warn-title{{color:var(--sell);font-weight:700;font-size:1rem;margin-bottom:0.6rem;}}
.warn-item{{color:var(--sell);font-size:0.85rem;margin:0.4rem 0;line-height:1.5;}}
.warn-note{{color:var(--muted);font-size:0.75rem;margin-top:0.6rem;}}
.ok-banner{{background:rgba(74,222,128,0.08);border:1px solid #4ade80;color:#4ade80;border-radius:8px;padding:0.8rem;margin-bottom:1.2rem;font-size:0.9rem;}}
table{{width:100%;border-collapse:collapse;background:var(--panel);border-radius:8px;overflow:hidden;}}
th,td{{padding:0.6rem 0.8rem;text-align:left;border-bottom:1px solid var(--border);font-size:0.85rem;}}
th{{background:rgba(212,165,116,0.1);color:var(--gold);font-weight:600;}}
.ccy{{font-family:monospace;font-weight:700;}}
.rate-val{{font-family:monospace;font-weight:700;}}
.rate-warn{{font-family:monospace;font-weight:700;color:var(--sell);font-size:1rem;}}
.warn-row{{background:rgba(248,113,113,0.06);}}
.src{{color:var(--muted);font-size:0.72rem;}}
.foot{{color:var(--muted);font-size:0.72rem;margin-top:1rem;text-align:center;}}
</style></head><body>
<nav class="top-nav">
  <a href="./">📊 ダッシュボード</a>
  <a href="./terminal.html">🖥 分析ターミナル</a>
  <a href="./daytrade.html">⚡ デイトレ</a>
  <a href="./position_manager.html">📋 ポジション管理</a>
  <a href="./rates.html" class="active">💴 金利モニター</a>
</nav>
<h1>💴 金利モニター</h1>
<div class="sub">中央銀行政策金利（FRED自動取得）· stance矛盾検知 · {checked} 時点</div>
{warn_banner}
<table>
  <thead><tr><th>通貨</th><th>中央銀行</th><th>政策金利</th><th>スタンス</th><th>取得元</th></tr></thead>
  <tbody>{rows}</tbody>
</table>
<div class="foot">金利数値はFRED APIで自動更新。stance（利上げ/利下げ姿勢）は手動メンテ。<br>
金利が動いたのにstanceが矛盾する場合は赤字で警告します。</div>
</body></html>'''

    try:
        os.makedirs(os.path.dirname(out_file), exist_ok=True)
        with open(out_file, "w", encoding="utf-8") as f:
            f.write(html)
        print(f"[OK] 金利モニターHTML生成: {out_file}")
    except Exception as e:
        print(f"[WARN] 金利HTML生成失敗: {e}")
    return html
