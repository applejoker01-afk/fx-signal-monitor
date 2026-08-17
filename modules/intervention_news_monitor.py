#!/usr/bin/env python3
"""
為替介入ニュース検知モジュール v1.0

既存の calc_intervention_risk()（advanced_analytics.py）はUSDJPY専用・
価格水準やDXY等の「事後的・定量的な代理指標」のみで介入リスクを推定していた。
本モジュールは geopolitical_risk.py と同じGoogle News RSS方式で、
「為替介入」「口先介入」を示唆する報道そのものをリアルタイムに検知し、
先回りでシグナルに反映することを目的とする。

対応通貨:
  JPY: BOJ/財務省介入（円買い方向）。実績が豊富なため自動★降格まで行う。
  USD/EUR/GBP/CHF: 介入自体が稀・方向の事前判断が難しいため、
                    スコアは算出するが★の自動操作はせず警告のみに留める。
"""

import email.utils
import json
import os
import urllib.request
import urllib.parse
import xml.etree.ElementTree as ET
from datetime import datetime, timezone, timedelta

import yaml

CACHE_FILE = "data/intervention_news_cache.json"
DIARY_PATH = "docs/intervention_news_diary.jsonl"
CACHE_TTL_MINUTES = 45  # ニュースは数分単位で変わらないため、実行の都度RSSを叩かない
# 2026-08-18判明: Google News RSSは「円 介入リスク」のような常時話題になっている
# トピックだと、発行日に関係なく評論記事を毎回上位に返し続ける。スコアリングが
# 記事の鮮度を見ていなかったため、同じ見出し集合でJPYロングにHIGH判定(-1★)が
# 8/3の本モジュール導入以降ほぼ恒常的にかかり続け、指値待機シグナルが2週間以上
# ゼロになる実害が出た。ここでは「直近FRESHNESS_HOURS時間以内に発行された記事」
# のみをスコア対象にし、古い定常的な解説記事による恒久的な高スコア化を防ぐ。
FRESHNESS_HOURS = 72

IS_GITHUB_ACTIONS = os.getenv("GITHUB_ACTIONS") == "true"
OBSIDIAN_DIARY_DIR = "docs/diary_output" if IS_GITHUB_ACTIONS else "docs/intervention_news"

# 通貨ごとの検知キーワードと自動介入判定の可否
CURRENCY_WATCH = {
    "JPY": {
        "query": '(為替介入 OR "為替 介入" OR "口先介入" OR "yen intervention" OR "BOJ intervention" OR "MOF intervention")',
        "keywords": [
            "為替介入", "口先介入", "円買い介入", "円売り介入", "為替 介入",
            "財務省 為替", "yen intervention", "boj intervention",
            "mof intervention", "japan intervenes", "currency intervention",
        ],
        "auto_star_action": True,   # 実績十分（2022, 2024複数回）のため自動降格対象
    },
    "USD": {
        # "treasury intervention"/"fed intervention" 単独は国債買入・金融安定化策等の
        # 非FXニュースを大量に拾うため、"currency"/"fx"/"g7"を伴う語のみに絞る。
        "query": '("dollar fx intervention" OR "treasury currency intervention" OR "g7 coordinated intervention" OR "dollar currency intervention")',
        "keywords": [
            "dollar fx intervention", "treasury currency intervention",
            "coordinated intervention", "dollar currency intervention",
        ],
        "auto_star_action": False,  # 実例が稀・方向の事前判断が難しいため警告のみ
    },
    "EUR": {
        # "ecb intervention"単独は国債買入等の金融政策ニュースを大量に拾うため除外。
        "query": '("ecb fx intervention" OR "ecb currency intervention" OR "euro fx intervention")',
        "keywords": ["ecb fx intervention", "ecb currency intervention", "euro fx intervention"],
        "auto_star_action": False,
    },
    "GBP": {
        # "boe intervention"単独は国債市場介入（2022年のギルト危機等）を大量に拾うため除外。
        "query": '("boe fx intervention" OR "boe currency intervention" OR "sterling intervention")',
        "keywords": ["boe fx intervention", "boe currency intervention", "sterling intervention"],
        "auto_star_action": False,
    },
    "CHF": {
        # SNBはFX介入が主要な政策手段のため、単独キーワードでも比較的誤検知が少ない。
        "query": '("snb intervention" OR "franc intervention")',
        "keywords": ["snb intervention", "franc intervention"],
        "auto_star_action": False,
    },
}


def load_obsidian_vault_path():
    config_path = "config.yaml"
    if os.path.exists(config_path):
        try:
            with open(config_path, "r", encoding="utf-8") as f:
                config = yaml.safe_load(f)
            if config and "geopolitical" in config:
                return config["geopolitical"].get("obsidian_vault_path")
        except Exception:
            pass
    return None


def get_obsidian_diary_path():
    if IS_GITHUB_ACTIONS:
        return OBSIDIAN_DIARY_DIR
    vault_path = load_obsidian_vault_path()
    if vault_path and os.path.exists(vault_path):
        return os.path.join(vault_path, "Intervention News Diary")
    return OBSIDIAN_DIARY_DIR


def load_cache():
    if not os.path.exists(CACHE_FILE):
        return {}
    try:
        with open(CACHE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_cache(cache):
    os.makedirs(os.path.dirname(CACHE_FILE), exist_ok=True)
    try:
        with open(CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(cache, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"[WARN] Intervention news cache save failed: {e}")


def _cache_is_fresh(entry):
    ts = entry.get("timestamp")
    if not ts:
        return False
    try:
        cached_at = datetime.fromisoformat(ts)
    except Exception:
        return False
    return datetime.now(timezone.utc) - cached_at < timedelta(minutes=CACHE_TTL_MINUTES)


def _parse_pubdate(raw: str):
    """RFC822形式のpubDate（例: 'Mon, 17 Aug 2026 12:00:00 GMT'）をdatetimeへ。失敗時None。"""
    if not raw:
        return None
    try:
        dt = email.utils.parsedate_to_datetime(raw)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None


def fetch_news_from_google(query, timeout=12, freshness_hours=FRESHNESS_HOURS):
    """
    <item><title>のうち、直近freshness_hours時間以内に発行されたものだけを拾う
    （channel直下のフィード自身の<title>はクエリ文言のエコーであり、キーワード
    スコアリングに混ぜると自己一致で常に高スコアになってしまうため除外する）。
    pubDateが取得できない記事は鮮度判定できないため保守的に除外する。
    """
    encoded = urllib.parse.quote(query)
    url = f"https://news.google.com/rss/search?q={encoded}&hl=en-US&gl=US&ceid=US:en"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "fx-signal-monitor/intervention-news-1.0"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            content = resp.read()
            root = ET.fromstring(content)
            cutoff = datetime.now(timezone.utc) - timedelta(hours=freshness_hours)
            titles = []
            for item in root.findall(".//item"):
                title = item.findtext("title", default="")
                if not title or len(title) <= 10:
                    continue
                pub_dt = _parse_pubdate(item.findtext("pubDate", default=""))
                if pub_dt is None or pub_dt < cutoff:
                    continue
                titles.append(title)
            return titles[:10]
    except Exception as e:
        print(f"[WARN] Intervention news fetch failed for query={query[:40]}...: {str(e)[:60]}")
        return []


# 2026-08-18判明: USDJPYが心理的節目（160円近辺）にいる間、金融メディアは
# 「介入リスク」を毎日のように論説・分析記事で取り上げ続ける。これは実際の
# 介入実施や当局の公式発言（一次情報）とは性質が違う「ambient chatter」であり、
# 同じ重みで数えると鮮度フィルターだけでは高スコアが下がらない（直近72時間だけ
# でも10本前後は常時ヒットする）。見出しの言い回しから一次情報/観測記事を粗く
# 分類し、観測記事は減衰させる一方、実施を示す一次情報は即座に高スコアへ引き上げる。
PRIMARY_ACTION_MARKERS = [
    "intervenes", "intervened", "confirms intervention", "confirmed intervention",
    "buys yen", "sells yen", "buying yen", "selling yen", "steps into the market",
    "verbal intervention", "actual intervention", "spent ¥", "spent $",
]
SPECULATIVE_MARKERS = [
    "risk of", "risk near", "risks cap", " may ", " could ", "likely to",
    "case for", "myth of", "explained", "narrative", "signals growing concern",
    "questions", "precedent for future", "warns", "flags", "threat", "expects",
    "highlighted", "keeps up", "wears off", "fizzles", "never about", "seen nearing",
]


def _headline_weight(title: str) -> float:
    """見出し1本の重み。一次情報=1.0、観測・論説記事=0.3、判別不能=0.6。"""
    t = title.lower()
    if any(m in t for m in PRIMARY_ACTION_MARKERS):
        return 1.0
    if any(m in t for m in SPECULATIVE_MARKERS):
        return 0.3
    return 0.6


def score_news(titles, keywords):
    """
    ヒットしたキーワード数と、報道量（一次情報/観測記事で重み付け）から0〜100点を算出。
    geopolitical_risk.calculate_risk_score と同じ考え方（キーワード＋出現量）だが、
    観測記事の連投だけではCRITICAL/HIGHに到達しないよう重みを分ける。
    """
    if not titles:
        return {"score": 0, "level": "none", "hits": [], "reason": "関連報道なし"}

    all_text = " ".join(titles).lower()
    hits = [kw for kw in keywords if kw.lower() in all_text]
    if not hits:
        return {"score": 0, "level": "none", "hits": [], "reason": "キーワード該当なし"}

    weights = [_headline_weight(t) for t in titles]
    has_primary = any(w >= 1.0 for w in weights)

    hit_score = min(70, len(hits) * 25)
    volume_bonus = min(30, round(sum(weights) * 4))
    score = min(100, hit_score + volume_bonus)
    if has_primary:
        # 一次情報（実施確認・当局の実際の行動を示す見出し）が1本でもあれば
        # 観測記事の量に関わらず無条件でCRITICAL相当まで引き上げる。
        score = max(score, 70)

    if score >= 70:
        level = "CRITICAL"
    elif score >= 45:
        level = "HIGH"
    elif score >= 25:
        level = "MEDIUM"
    else:
        level = "LOW"

    reason = f"検知キーワード: {', '.join(hits[:3])}"
    if has_primary:
        reason += "（一次情報あり）"

    return {
        "score": score,
        "level": level,
        "hits": hits,
        "reason": reason,
    }


def log_to_diary(currency, news_result, headlines):
    jst = datetime.now(timezone.utc) + timedelta(hours=9)
    entry = {
        "timestamp": jst.isoformat(),
        "currency": currency,
        "score": news_result["score"],
        "level": news_result["level"],
        "hits": news_result["hits"],
        "reason": news_result["reason"],
        "headlines": headlines[:5],
    }
    os.makedirs(os.path.dirname(DIARY_PATH), exist_ok=True)
    with open(DIARY_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    diary_dir = get_obsidian_diary_path()
    if not diary_dir:
        return
    os.makedirs(diary_dir, exist_ok=True)
    date_str = entry["timestamp"][:10]
    md_path = os.path.join(diary_dir, f"{date_str}.md")
    if os.path.exists(md_path):
        with open(md_path, "r", encoding="utf-8") as f:
            content = f.read()
    else:
        content = f"# 介入ニュース検知日記 {date_str}\n\n"

    md_entry = (
        f"## {entry['timestamp'][11:16]} - {currency}\n\n"
        f"- **スコア**: {entry['score']}（{entry['level']}）\n"
        f"- **検知キーワード**: {', '.join(entry['hits'])}\n"
        f"- **見出し**:\n"
    )
    for h in entry["headlines"]:
        md_entry += f"  - {h}\n"
    md_entry += "\n---\n"

    with open(md_path, "w", encoding="utf-8") as f:
        f.write(content + md_entry)


def evaluate_intervention_news(currency, force_refresh=False):
    """
    指定通貨の介入示唆ニュースを評価。TTL内はキャッシュを再利用してRSS負荷を抑える。

    Returns:
        {"score": 0-100, "level": "none/LOW/MEDIUM/HIGH/CRITICAL",
         "hits": [...], "reason": str, "headlines": [...], "cached": bool}
    """
    watch = CURRENCY_WATCH.get(currency)
    if not watch:
        return {"score": 0, "level": "none", "hits": [], "reason": "監視対象外通貨", "headlines": [], "cached": False}

    cache = load_cache()
    cached_entry = cache.get(currency)
    if not force_refresh and cached_entry and _cache_is_fresh(cached_entry):
        result = dict(cached_entry)
        result["cached"] = True
        return result

    titles = fetch_news_from_google(watch["query"])
    news_result = score_news(titles, watch["keywords"])
    news_result["headlines"] = titles[:5]
    news_result["cached"] = False
    news_result["timestamp"] = datetime.now(timezone.utc).isoformat()

    cache[currency] = news_result
    save_cache(cache)

    if news_result["score"] > 0:
        log_to_diary(currency, news_result, titles)

    return news_result


def apply_intervention_news_filter(pair, result, frm, to):
    """
    シグナル評価結果に介入ニュース検知を反映する。
    signal_scanner.evaluate_full() から apply_geopolitical_filter と同じ位置で呼ぶ。

    JPY: 実績十分のため、対円ロング（＝円売り方向）シグナルを自動降格。
    その他通貨: 方向の事前判断が難しいため、スコアは付与するが★は動かさず警告のみ。
    """
    watched = [c for c in (frm, to) if c in CURRENCY_WATCH]
    if not watched:
        return result

    news_by_currency = {}
    warnings = []

    for ccy in watched:
        news = evaluate_intervention_news(ccy)
        news_by_currency[ccy] = news

        if news["score"] < 25:
            continue

        watch = CURRENCY_WATCH[ccy]
        if watch["auto_star_action"] and ccy == "JPY" and to == "JPY" and result.get("direction", "").endswith("LONG"):
            original = result.get("original_stars", result["stars"])
            result["original_stars"] = original
            if news["score"] >= 70:
                result["stars"] = max(1, result["stars"] - 2)
                result["verdict"] += f" 🚨介入ニュース検知CRITICAL({news['score']})"
            elif news["score"] >= 45:
                result["stars"] = max(1, result["stars"] - 1)
                result["verdict"] += f" ⚠介入観測報道HIGH({news['score']})"
            else:
                result["verdict"] += f" ⚠介入観測報道MEDIUM({news['score']})"
        elif not watch["auto_star_action"]:
            warnings.append(f"{ccy}介入示唆報道 score{news['score']}({news['level']}): {news['reason']}")

    result["intervention_news"] = news_by_currency
    if warnings:
        result["intervention_watch_warning"] = warnings

    return result


if __name__ == "__main__":
    for ccy in CURRENCY_WATCH:
        print(ccy, evaluate_intervention_news(ccy, force_refresh=True))
