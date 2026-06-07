#!/usr/bin/env python3
"""
地政学・政治リスク評価モジュール（新興国通貨向け） v2.5
- GitHub Actions対応（CI時は別出力ディレクトリに保存）
"""

import urllib.request
import urllib.parse
import re
import json
import os
import yaml
from datetime import datetime, timedelta, timezone

EM_COUNTRY_MAP = {
    "MXN": "Mexico", "ZAR": "South Africa", "TRY": "Turkey",
    "INR": "India", "BRL": "Brazil", "RUB": "Russia",
    "CNY": "China", "IDR": "Indonesia", "THB": "Thailand", "PHP": "Philippines",
}

RISK_CATEGORIES = {
    "political_instability": {"weight": 30, "keywords": ["coup", "protest", "election crisis", "political violence", "government collapse"]},
    "policy_surprise":       {"weight": 25, "keywords": ["central bank surprise", "emergency rate", "unexpected policy", "capital control"]},
    "geopolitical":          {"weight": 25, "keywords": ["sanction", "war", "conflict", "geopolitical tension", "border dispute"]},
    "economic_crisis":       {"weight": 20, "keywords": ["default", "recession", "currency crisis", "inflation spike", "debt crisis"]},
    "social_unrest":         {"weight": 15, "keywords": ["riot", "demonstration", "strike", "civil unrest", "mass protest"]},
}

HIGH_RISK_KEYWORDS = [k for cat in RISK_CATEGORIES.values() for k in cat["keywords"]]

DIARY_PATH = "docs/geopolitical_risk_diary.jsonl"

# GitHub Actions環境かどうかを判定
IS_GITHUB_ACTIONS = os.getenv("GITHUB_ACTIONS") == "true"

# CI時は専用ディレクトリに出力（ワークフロー側で別リポジトリにpush）
if IS_GITHUB_ACTIONS:
    OBSIDIAN_DIARY_DIR = "docs/diary_output"
else:
    OBSIDIAN_DIARY_DIR = "docs/geopolitical_risk"


def load_obsidian_vault_path():
    env_path = os.getenv("OBSIDIAN_VAULT_PATH")
    if env_path:
        return env_path

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
        return os.path.join(vault_path, "Geopolitical Risk Diary")
    return OBSIDIAN_DIARY_DIR


def log_to_diary(entry):
    os.makedirs(os.path.dirname(DIARY_PATH), exist_ok=True)
    with open(DIARY_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    write_obsidian_markdown_diary(entry)


def write_obsidian_markdown_diary(entry):
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
        content = f"# 地政学リスク日記 {date_str}\n\n"

    md_entry = f"""## {entry['timestamp'][11:16]} - {entry['pair']}

- **リスクスコア**: {entry['risk_score']}（{entry['risk_level']}）
- **調整**: ★{entry['original_stars']} → ★{entry['adjusted_stars']}（-{entry['penalty']}）
- **検知カテゴリ**:
"""
    for cat, info in entry.get("categories", {}).items():
        md_entry += f"  - {cat}: {', '.join(info.get('hits', []))}\n"

    md_entry += f"- **使用ニュース**:\n"
    for news in entry.get("news_used", []):
        md_entry += f"  - {news}\n"

    md_entry += f"\n**理由**: {entry['reason']}\n\n---\n"

    with open(md_path, "w", encoding="utf-8") as f:
        f.write(content + md_entry)


def fetch_news_from_google(country, timeout=12):
    query = f"{country} (election OR protest OR crisis OR sanction OR \"central bank\" OR geopolitics)"
    encoded = urllib.parse.quote(query)
    url = f"https://news.google.com/rss/search?q={encoded}&hl=en-US&gl=US&ceid=US:en"

    try:
        req = urllib.request.Request(url, headers={"User-Agent": "fx-signal-monitor/2.5"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            content = resp.read().decode("utf-8", errors="ignore")
            titles = re.findall(r"<title>(.*?)</title>", content)
            return [t for t in titles if len(t) > 10][:8]
    except Exception as e:
        print(f"[WARN] Google News failed for {country}: {str(e)[:60]}")
        return []


def fetch_recent_news(country, max_results=8, timeout=12):
    titles = fetch_news_from_google(country, timeout)
    return titles[:max_results]


def calculate_risk_score(news_titles, country):
    if not news_titles:
        return {
            "risk_score": 15, "risk_level": "low",
            "categories": {}, "reason": "最近の関連ニュースが少ない"
        }

    all_text = " ".join(news_titles).lower()
    category_scores = {}
    total_score = 0

    for category, data in RISK_CATEGORIES.items():
        hits = [kw for kw in data["keywords"] if kw in all_text]
        if hits:
            score = min(data["weight"], len(hits) * (data["weight"] // 2))
            category_scores[category] = {"score": score, "hits": hits}
            total_score += score

    volume_bonus = min(15, len(news_titles) * 2)
    total_score = min(100, total_score + volume_bonus)

    if total_score >= 70: level = "extreme"
    elif total_score >= 50: level = "high"
    elif total_score >= 30: level = "medium"
    else: level = "low"

    reason_parts = []
    for cat, info in category_scores.items():
        reason_parts.append(f"{cat}: {', '.join(info['hits'][:2])}")

    return {
        "risk_score": round(total_score, 1),
        "risk_level": level,
        "categories": category_scores,
        "reason": " / ".join(reason_parts) if reason_parts else "特段のリスクキーワードは検出されず",
        "news_volume": len(news_titles)
    }


def evaluate_geopolitical_risk(pair, price=None):
    base_currency = None
    for ccy in EM_COUNTRY_MAP:
        if ccy in pair:
            base_currency = ccy
            break

    if not base_currency:
        return {
            "risk_level": "low", "risk_score": 10,
            "reason": "先進国または監視対象外",
            "recent_news": [], "categories": {}
        }

    country = EM_COUNTRY_MAP[base_currency]
    news_titles = fetch_recent_news(country)
    risk_data = calculate_risk_score(news_titles, country)

    return {
        "risk_level": risk_data["risk_level"],
        "risk_score": risk_data["risk_score"],
        "reason": risk_data["reason"],
        "recent_news": news_titles[:4],
        "categories": risk_data.get("categories", {}),
        "country": country,
        "news_volume": risk_data.get("news_volume", 0)
    }


def apply_geopolitical_filter(pair, result):
    risk = evaluate_geopolitical_risk(pair)
    original_stars = result.get("stars", 3)
    adjusted_stars = original_stars
    penalty = 0

    if risk["risk_score"] >= 30:
        penalty = int(risk["risk_score"] // 20)
        adjusted_stars = max(1, original_stars - penalty)
        result["geopolitical_risk"] = risk
        result["original_stars"] = original_stars
        result["stars"] = adjusted_stars
        result["verdict"] = f"⚠ 地政学リスク({risk['risk_level']})考慮 {result.get('verdict', '')}"
        result["risk_adjustment"] = -risk["risk_score"]

    jst = datetime.now(timezone.utc) + timedelta(hours=9)
    log_entry = {
        "timestamp": jst.isoformat(),
        "pair": pair,
        "risk_score": risk["risk_score"],
        "risk_level": risk["risk_level"],
        "categories": risk.get("categories", {}),
        "news_used": risk.get("recent_news", []),
        "original_stars": original_stars,
        "adjusted_stars": adjusted_stars,
        "penalty": penalty,
        "reason": risk["reason"]
    }
    log_to_diary(log_entry)

    return result


if __name__ == "__main__":
    for pair in ["MXNJPY", "ZARJPY", "TRYJPY", "USDJPY"]:
        print(pair, evaluate_geopolitical_risk(pair))