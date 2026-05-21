"""
カスタムルールエンジン

Obsidian Vaultから抽出した signal_rule, lesson 等を、
L3シグナル評価結果に適用してスコアを補正する。

- signal_rule: 条件に合致したら★を上下、または取引ブロック
- lesson: 条件マッチで通知に教訓を付加
- analysis: 関連通貨ペアの分析メモを通知に追加
- strategy: ポートフォリオレベルの方針をメタデータに記録
"""

import re
from datetime import datetime, timezone, timedelta


# ============================================================================
# 条件式評価エンジン
# ============================================================================

# 安全な評価のための演算子マップ
COMPARISON_OPS = {
    ">=": lambda a, b: a >= b,
    "<=": lambda a, b: a <= b,
    ">":  lambda a, b: a > b,
    "<":  lambda a, b: a < b,
    "==": lambda a, b: a == b,
    "!=": lambda a, b: a != b,
}

# 評価コンテキストで使用可能な変数
def build_context(signal_result, sentiment, now=None):
    """評価コンテキストを構築"""
    if now is None:
        now = datetime.now(timezone.utc)
    jst = now + timedelta(hours=9)

    ctx = {
        # 基本シグナル情報
        "price": signal_result.get("price"),
        "ta_score": signal_result.get("ta_score"),
        "fa_score": signal_result.get("fa_score"),
        "rate_diff": signal_result.get("fa_rate_diff"),
        "stars": signal_result.get("stars"),
        "rsi": signal_result.get("rsi"),
        "macd": signal_result.get("macd"),

        # テクニカルフラグ
        "above_200dma": (
            signal_result.get("dma200") and signal_result.get("price") and
            signal_result["price"] > signal_result["dma200"]
        ),
        "below_200dma": (
            signal_result.get("dma200") and signal_result.get("price") and
            signal_result["price"] < signal_result["dma200"]
        ),
        "golden_cross_50_200": (
            signal_result.get("dma50") and signal_result.get("dma200") and
            signal_result["dma50"] > signal_result["dma200"] * 1.005
        ),
        "dead_cross_50_200": (
            signal_result.get("dma50") and signal_result.get("dma200") and
            signal_result["dma50"] < signal_result["dma200"] * 0.995
        ),

        # MACDシグナル
        "macd_signal": _macd_status(signal_result),

        # センチメント
        "vix": sentiment.get("vix") if sentiment else None,
        "dxy": sentiment.get("dxy") if sentiment else None,
        "dxy_trend": sentiment.get("dxy_trend") if sentiment else None,
        "us10y": sentiment.get("us10y") if sentiment else None,
        "gold": sentiment.get("gold") if sentiment else None,
        "gold_trend": sentiment.get("gold_trend") if sentiment else None,
        "risk_mode": sentiment.get("risk_mode") if sentiment else None,

        # ファンダメンタル
        "fa_direction": signal_result.get("fa_direction"),

        # 時間情報（JST）
        "hour_jst": jst.hour,
        "day_of_week": jst.strftime("%A").lower(),
        "weekday": jst.weekday(),  # 0=月曜
    }
    return ctx


def _macd_status(signal_result):
    """MACDシグナル状態を文字列で返す"""
    macd = signal_result.get("macd")
    sig = signal_result.get("macd_signal")
    if macd is None or sig is None:
        return None
    if macd > sig and macd > 0:
        return "golden_cross"
    if macd < sig and macd < 0:
        return "dead_cross"
    if macd > sig:
        return "rising"
    return "falling"


# 安全な式評価（eval()は使わない）
CONDITION_PATTERN = re.compile(
    r"^\s*(\w+)\s*(>=|<=|==|!=|>|<)\s*(.+?)\s*$"
)


def evaluate_condition(expr, ctx):
    """
    単一の条件式を評価。安全のためevalは使わず、簡易パーサーで処理。
    例: "price >= 155.0", "vix > 25", "above_200dma == true"
    """
    if not expr or not isinstance(expr, str):
        return False

    m = CONDITION_PATTERN.match(expr)
    if not m:
        print(f"[WARN] Cannot parse condition: {expr}")
        return False

    var_name, op, value_str = m.group(1), m.group(2), m.group(3).strip()

    # 変数の値取得
    if var_name not in ctx:
        return False
    var_value = ctx[var_name]
    if var_value is None:
        return False

    # 値の型推定
    value = _parse_literal(value_str)

    # 型を揃える
    try:
        if isinstance(var_value, bool) and isinstance(value, bool):
            pass
        elif isinstance(var_value, (int, float)) and isinstance(value, (int, float)):
            pass
        elif isinstance(var_value, str) and isinstance(value, str):
            pass
        else:
            # 数値どうしに変換可能か試す
            try:
                var_value = float(var_value)
                value = float(value)
            except (ValueError, TypeError):
                # 文字列比較
                var_value = str(var_value)
                value = str(value)
    except Exception:
        return False

    op_func = COMPARISON_OPS.get(op)
    if not op_func:
        return False
    try:
        return op_func(var_value, value)
    except Exception:
        return False


def _parse_literal(s):
    """文字列をリテラル値に変換"""
    s = s.strip()
    if (s.startswith('"') and s.endswith('"')) or (s.startswith("'") and s.endswith("'")):
        return s[1:-1]
    if s.lower() in ("true", "yes"):
        return True
    if s.lower() in ("false", "no"):
        return False
    if s.lower() in ("null", "none"):
        return None
    try:
        if "." in s:
            return float(s)
        return int(s)
    except ValueError:
        return s


def evaluate_conditions(conditions, ctx):
    """複数の条件式を全てANDで評価"""
    if not conditions:
        return False
    if isinstance(conditions, str):
        conditions = [conditions]
    if not isinstance(conditions, list):
        return False
    return all(evaluate_condition(c, ctx) for c in conditions)


# ============================================================================
# ルール適用
# ============================================================================

def apply_signal_rules(signal_result, sentiment, signal_rules_notes, now=None):
    """
    signal_rule タイプのObsidianノートを評価結果に適用。
    返り値: 補正された signal_result（in-placeで修正）
    """
    pair = signal_result["pair"]
    ctx = build_context(signal_result, sentiment, now)
    applied = []

    for note in signal_rules_notes:
        fm = note["frontmatter"]
        rule = fm.get("rule", {})

        # 対象ペアの絞り込み
        target_pairs = fm.get("pairs", [])
        if target_pairs and "all" not in target_pairs and pair not in target_pairs:
            continue

        # 条件評価
        conditions = rule.get("when", [])
        if not evaluate_conditions(conditions, ctx):
            continue

        # アクション適用
        then = rule.get("then", {})
        action = then.get("action")
        severity = then.get("severity", 1)
        if not action:
            continue

        original_stars = signal_result["stars"]
        new_stars = original_stars

        if action == "upgrade_long" and signal_result["direction"].endswith("LONG"):
            new_stars = min(5, original_stars + severity)
        elif action == "downgrade_long" and signal_result["direction"].endswith("LONG"):
            new_stars = max(1, original_stars - severity)
        elif action == "upgrade_short" and signal_result["direction"].endswith("SHORT"):
            new_stars = min(5, original_stars + severity)
        elif action == "downgrade_short" and signal_result["direction"].endswith("SHORT"):
            new_stars = max(1, original_stars - severity)
        elif action == "block_trade":
            new_stars = 1
            signal_result["verdict"] = "⛔ Wikiルールによりブロック"
            signal_result["direction"] = "BLOCKED_BY_RULE"

        if new_stars != original_stars or action == "force_review":
            # ルール名は note の rule.name → frontmatter.name → ファイル名 の順で取得
            rule_name = rule.get("name") or fm.get("name") or note.get("filename", note["path"].split("/")[-1])
            applied.append({
                "rule_name": rule_name,
                "rule_path": note["path"],
                "rule_filename": note.get("filename", note["path"].split("/")[-1]),
                "filename": note.get("filename", note["path"].split("/")[-1]),
                "action": action,
                "severity": severity,
                "stars_before": original_stars,
                "stars_after": new_stars,
                "note": rule.get("note", ""),
                "confidence": fm.get("confidence"),
            })
            signal_result["stars"] = new_stars

    if applied:
        signal_result["obsidian_rules_applied"] = applied

    return signal_result


def attach_relevant_lessons(signal_result, sentiment, lessons_notes, now=None):
    """関連する lessonノートを signal_result に添付"""
    ctx = build_context(signal_result, sentiment, now)
    pair = signal_result["pair"]
    relevant = []

    for note in lessons_notes:
        fm = note["frontmatter"]
        target_pairs = fm.get("pairs", [])
        if target_pairs and "all" not in target_pairs and pair not in target_pairs:
            continue

        trigger = fm.get("trigger_pattern", [])
        if not trigger:
            continue
        if evaluate_conditions(trigger, ctx):
            relevant.append({
                "path": note["path"],
                "filename": note["filename"],
                "title": fm.get("title", note["filename"]),
                "priority": fm.get("priority", "medium"),
                "excerpt": note.get("body_excerpt", "")[:200],
            })

    if relevant:
        signal_result["relevant_lessons"] = relevant
    return signal_result


def attach_relevant_analyses(signal_result, analyses_notes):
    """対象通貨ペアの分析メモを signal_result に添付"""
    pair = signal_result["pair"]
    relevant = []
    for note in analyses_notes:
        target_pairs = note["frontmatter"].get("pairs", [])
        if pair in target_pairs:
            fm = note["frontmatter"]
            title = fm.get("title") or fm.get("topic") or note["filename"]
            relevant.append({
                "path": note["path"],
                "filename": note["filename"],
                "title": title,
                "topic": fm.get("topic", ""),
                "summary": fm.get("summary", ""),
            })
    if relevant:
        signal_result["wiki_analyses"] = relevant
    return signal_result


def get_recent_journal_count(pair, journals_notes, days=30):
    """対象通貨ペアの直近トレード数を集計"""
    cutoff = datetime.now(timezone.utc).date() - timedelta(days=days)
    count = 0
    profits = 0
    losses = 0
    total_pips = 0

    for note in journals_notes:
        fm = note["frontmatter"]
        if pair not in (fm.get("pairs", []) or []):
            continue
        trade_date_str = fm.get("trade_date")
        if not trade_date_str:
            continue
        try:
            trade_date = datetime.fromisoformat(str(trade_date_str)).date()
        except (ValueError, TypeError):
            continue
        if trade_date < cutoff:
            continue
        count += 1
        pips = fm.get("pips", 0) or 0
        total_pips += pips
        result = fm.get("result", "")
        if result == "profit":
            profits += 1
        elif result == "loss":
            losses += 1

    return {
        "count": count, "profits": profits, "losses": losses,
        "total_pips": total_pips,
        "win_rate": (profits / count * 100) if count > 0 else None,
    }


# ============================================================================
# 統合適用エントリーポイント
# ============================================================================

def apply_obsidian_intelligence(signal_result, sentiment, obsidian_data, now=None):
    """
    Obsidianから抽出した全データを signal_result に適用。
    """
    if not obsidian_data:
        return signal_result

    # signal_ruleの適用（★を上下する可能性あり）
    signal_rules = obsidian_data.get("signal_rules", [])
    if signal_rules:
        apply_signal_rules(signal_result, sentiment, signal_rules, now)

    # lessonの関連性チェック
    lessons = obsidian_data.get("lessons", [])
    if lessons:
        attach_relevant_lessons(signal_result, sentiment, lessons, now)

    # analysisノートの添付
    analyses = obsidian_data.get("analyses", [])
    if analyses:
        attach_relevant_analyses(signal_result, analyses)

    # journal統計
    journals = obsidian_data.get("journals", [])
    if journals:
        signal_result["journal_stats_30d"] = get_recent_journal_count(
            signal_result["pair"], journals
        )

    return signal_result
