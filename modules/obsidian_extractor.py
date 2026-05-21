"""
Obsidian Vault 抽出モジュール

GitHub Privateリポジトリ obsidian-vault から finance domain のノートを取得し、
YAML frontmatterからシグナルルールを抽出する。

依存: pyyaml
"""

import os
import re
import json
import base64
import urllib.request
import urllib.error
from datetime import datetime, timezone

try:
    import yaml
except ImportError:
    yaml = None
    print("[WARN] pyyaml not installed; install with: pip install pyyaml")


# ============================================================================
# 設定
# ============================================================================

VAULT_OWNER = os.environ.get("VAULT_OWNER", "applejoker01-afk")
VAULT_REPO = os.environ.get("VAULT_REPO", "obsidian-vault")
VAULT_BRANCH = os.environ.get("VAULT_BRANCH", "main")
VAULT_FINANCE_PATH = os.environ.get("VAULT_FINANCE_PATH", "02_Domains/finance")
GITHUB_PAT = os.environ.get("OBSIDIAN_VAULT_PAT")

EXTRACTED_CACHE_FILE = "data/obsidian_cache.json"
VALID_TYPES = {"signal_rule", "analysis", "journal", "lesson", "strategy"}


# ============================================================================
# GitHub API ヘルパー
# ============================================================================

def github_api_get(url, timeout=15):
    """GitHub APIにPAT認証で取得"""
    if not GITHUB_PAT:
        raise RuntimeError("OBSIDIAN_VAULT_PAT environment variable not set")
    req = urllib.request.Request(url, headers={
        "Authorization": f"token {GITHUB_PAT}",
        "Accept": "application/vnd.github.v3+json",
        "User-Agent": "fx-signal-monitor/1.0"
    })
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def urllib_quote(s):
    """URL safe quote (standard library)"""
    from urllib.parse import quote
    return quote(s, safe="/")


def list_vault_files(path=None):
    """Vault内の指定パスのファイル一覧を取得（サブフォルダも再帰的に）"""
    if path is None:
        path = VAULT_FINANCE_PATH
    url = (
        f"https://api.github.com/repos/{VAULT_OWNER}/{VAULT_REPO}/contents/"
        f"{urllib_quote(path)}?ref={VAULT_BRANCH}"
    )
    try:
        items = github_api_get(url)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            print(f"[INFO] Path not found: {path} (Vault may not have this folder yet)")
            return []
        raise

    md_files = []
    for item in items:
        if item["type"] == "file" and item["name"].endswith(".md"):
            md_files.append(item)
        elif item["type"] == "dir":
            md_files.extend(list_vault_files(item["path"]))
    return md_files


def fetch_file_content(file_info):
    """ファイル内容を取得"""
    if "content" in file_info and file_info.get("encoding") == "base64":
        return base64.b64decode(file_info["content"]).decode("utf-8", errors="ignore")
    url = file_info.get("download_url")
    if url:
        req = urllib.request.Request(url, headers={
            "Authorization": f"token {GITHUB_PAT}",
            "User-Agent": "fx-signal-monitor/1.0"
        })
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.read().decode("utf-8", errors="ignore")
    return None


# ============================================================================
# Markdown / YAML frontmatter パーサー
# ============================================================================

FRONTMATTER_PATTERN = re.compile(
    r"^---\s*\n(.*?)\n---\s*\n?(.*)$", re.DOTALL
)


def parse_frontmatter(content):
    """
    MarkdownからYAML frontmatterと本文を分離。
    返り値: (frontmatter_dict, body_str) または (None, content)
    """
    if not yaml:
        return None, content
    m = FRONTMATTER_PATTERN.match(content)
    if not m:
        return None, content
    yaml_text = m.group(1)
    body = m.group(2)
    try:
        fm = yaml.safe_load(yaml_text)
        if not isinstance(fm, dict):
            return None, content
        return fm, body
    except yaml.YAMLError as e:
        print(f"[WARN] YAML parse error: {e}")
        return None, content


# ============================================================================
# ルール抽出
# ============================================================================

def extract_rules_from_vault():
    """Vault全体から finance domain のノートを取得し、type別に分類"""
    print(f"[INFO] Listing files in {VAULT_FINANCE_PATH}")
    try:
        files = list_vault_files()
    except Exception as e:
        print(f"[WARN] Cannot list vault files: {e}")
        return _empty_result(errors=[str(e)])

    print(f"[INFO] Found {len(files)} markdown files")

    signal_rules = []
    analyses = []
    journals = []
    lessons = []
    strategies = []
    errors = []

    for f in files:
        try:
            file_detail = github_api_get(
                f"https://api.github.com/repos/{VAULT_OWNER}/{VAULT_REPO}/"
                f"contents/{urllib_quote(f['path'])}?ref={VAULT_BRANCH}"
            )
            content = fetch_file_content(file_detail)
            if not content:
                continue

            fm, body = parse_frontmatter(content)
            if not fm:
                continue

            # finance domain以外はスキップ
            if fm.get("domain") != "finance":
                continue

            # archived ステータスはスキップ
            if fm.get("status") == "archived":
                continue

            note_type = fm.get("type")
            if note_type not in VALID_TYPES:
                continue

            note_meta = {
                "path": f["path"],
                "filename": f["name"],
                "frontmatter": fm,
                "body_excerpt": body[:300] if body else "",
                "tags": fm.get("tags", []) or [],
                "pairs": fm.get("pairs", []) or [],
                "priority": fm.get("priority", "medium"),
            }

            if note_type == "signal_rule":
                if "rule" in fm:
                    signal_rules.append(note_meta)
            elif note_type == "analysis":
                analyses.append(note_meta)
            elif note_type == "journal":
                journals.append(note_meta)
            elif note_type == "lesson":
                lessons.append(note_meta)
            elif note_type == "strategy":
                strategies.append(note_meta)

        except Exception as e:
            errors.append(f"{f['path']}: {e}")
            print(f"[WARN] Failed to process {f['path']}: {e}")

    print(f"[INFO] Extracted: {len(signal_rules)} rules, {len(analyses)} analyses, "
          f"{len(journals)} journals, {len(lessons)} lessons, {len(strategies)} strategies")

    return {
        "signal_rules": signal_rules,
        "analyses": analyses,
        "journals": journals,
        "lessons": lessons,
        "strategies": strategies,
        "errors": errors,
        "extracted_at": datetime.now(timezone.utc).isoformat(),
    }


def _empty_result(errors=None):
    return {
        "signal_rules": [], "analyses": [], "journals": [],
        "lessons": [], "strategies": [],
        "errors": errors or [],
        "extracted_at": datetime.now(timezone.utc).isoformat(),
    }


def save_extracted_rules(extracted):
    """抽出結果をJSONとして保存"""
    os.makedirs(os.path.dirname(EXTRACTED_CACHE_FILE), exist_ok=True)
    with open(EXTRACTED_CACHE_FILE, "w", encoding="utf-8") as f:
        json.dump(extracted, f, ensure_ascii=False, indent=2, default=str)
    print(f"[OK] Saved to {EXTRACTED_CACHE_FILE}")


def load_extracted_rules():
    """前回抽出した結果をロード"""
    if not os.path.exists(EXTRACTED_CACHE_FILE):
        return None
    try:
        with open(EXTRACTED_CACHE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        print(f"[WARN] Failed to load cache: {e}")
        return None


def fetch_and_save():
    """Vaultから抽出してJSON保存。失敗時はキャッシュを返す。"""
    if not GITHUB_PAT:
        print("[INFO] OBSIDIAN_VAULT_PAT not set, skipping Obsidian extraction")
        return load_extracted_rules() or _empty_result()
    try:
        extracted = extract_rules_from_vault()
        save_extracted_rules(extracted)
        return extracted
    except Exception as e:
        print(f"[ERROR] Obsidian extraction failed: {e}")
        return load_extracted_rules() or _empty_result(errors=[str(e)])


if __name__ == "__main__":
    result = fetch_and_save()
    if result:
        print(json.dumps({
            "signal_rules_count": len(result.get("signal_rules", [])),
            "analyses_count": len(result.get("analyses", [])),
            "journals_count": len(result.get("journals", [])),
            "lessons_count": len(result.get("lessons", [])),
            "strategies_count": len(result.get("strategies", [])),
        }, indent=2))
