#!/usr/bin/env python3
"""
run_intervention_impact_analysis.py
介入ニュース検知（modules/intervention_news_monitor.py）が実際の値動きを
先読みできていたかを事後検証するツール。

docs/intervention_news_diary.jsonl に蓄積された検知ログを読み込み、
検知日から+1/+3/+5営業日でUSDJPYがどう動いたか（介入の意図通り
「円高＝USDJPY下落」が続いたか）を集計する。

閾値（現状HIGH>=45で自動★降格）が妥当かどうかを、
このレポートの的中率・平均値幅で継続的に見直すために使う。
"""

import json
import os
import urllib.request
from datetime import datetime, timedelta

DIARY_PATH = "docs/intervention_news_diary.jsonl"
REPORT_PATH = "docs/intervention_impact_report.md"
MIN_SCORE = 25  # score_news()のMEDIUM以上のみ検証対象（LOW/noneはノイズが多い）


def http_get_json(url, timeout=15):
    req = urllib.request.Request(url, headers={"User-Agent": "fx-signal-monitor/impact-analysis"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def fetch_dated_history(frm, to, start_date, end_date):
    """Frankfurterから日付付き終値系列を取得。{date_str: close} を返す。"""
    endpoints = [
        f"https://api.frankfurter.dev/v1/{start_date}..{end_date}?base={frm}&symbols={to}",
        f"https://api.frankfurter.app/{start_date}..{end_date}?from={frm}&to={to}",
    ]
    for url in endpoints:
        try:
            data = http_get_json(url)
            if data and "rates" in data:
                return {d: v[to] for d, v in data["rates"].items() if to in v}
        except Exception as e:
            print(f"[WARN] history fetch failed: {e}")
    return {}


def load_diary_entries():
    if not os.path.exists(DIARY_PATH):
        print(f"[INFO] {DIARY_PATH} が存在しません。介入ニュース検知の実績がまだありません。")
        return []
    entries = []
    with open(DIARY_PATH, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return entries


def dedupe_by_date(entries, currency):
    """同一通貨・同一日の複数検知はその日の最大スコアのみ残す。"""
    by_date = {}
    for e in entries:
        if e.get("currency") != currency:
            continue
        if e.get("score", 0) < MIN_SCORE:
            continue
        date_str = e["timestamp"][:10]
        if date_str not in by_date or e["score"] > by_date[date_str]["score"]:
            by_date[date_str] = e
    return sorted(by_date.values(), key=lambda e: e["timestamp"])


def nearest_close(history, target_date, max_forward_days=5):
    """target_date以降で最初に見つかる終値（休日はスキップ）。"""
    d = datetime.fromisoformat(target_date).date()
    for i in range(max_forward_days + 1):
        key = str(d + timedelta(days=i))
        if key in history:
            return history[key]
    return None


def analyze_jpy_impact(alerts):
    """
    JPY介入ニュース検知 → USDJPYが+1/+3/+5営業日でどう動いたか集計。
    介入は円買い方向（USDJPY下落）を狙うため、下落=シナリオ的中とみなす。
    """
    if not alerts:
        return None

    first_date = alerts[0]["timestamp"][:10]
    start = (datetime.fromisoformat(first_date).date() - timedelta(days=2))
    end = datetime.now().date()
    history = fetch_dated_history("USD", "JPY", start, end)
    if not history:
        print("[WARN] USDJPY履歴の取得に失敗したため分析をスキップします")
        return None

    rows = []
    for alert in alerts:
        date_str = alert["timestamp"][:10]
        base_price = nearest_close(history, date_str, max_forward_days=0) or history.get(date_str)
        if base_price is None:
            # 起点日が休日等でヒットしない場合、直近の値を探す
            base_price = nearest_close(history, date_str, max_forward_days=3)
        if base_price is None:
            continue

        row = {"date": date_str, "score": alert["score"], "level": alert["level"], "base": base_price}
        for label, days in (("+1d", 1), ("+3d", 3), ("+5d", 5)):
            target = str(datetime.fromisoformat(date_str).date() + timedelta(days=days))
            future_price = nearest_close(history, target, max_forward_days=3)
            if future_price is not None:
                pips = round((future_price - base_price) * 100, 1)  # JPYクロスは100=1銭
                row[label] = pips
            else:
                row[label] = None
        rows.append(row)

    return rows


def summarize(rows):
    summary = {}
    for label in ("+1d", "+3d", "+5d"):
        vals = [r[label] for r in rows if r.get(label) is not None]
        if not vals:
            summary[label] = None
            continue
        hits = sum(1 for v in vals if v < 0)  # 下落=円高=介入の意図通り
        summary[label] = {
            "n": len(vals),
            "hit_rate": round(100 * hits / len(vals), 1),
            "avg_pips": round(sum(vals) / len(vals), 1),
        }
    return summary


def format_report(rows, summary):
    lines = []
    lines.append(f"# 介入ニュース検知 効果検証レポート")
    lines.append(f"生成日時: {datetime.now().isoformat()[:19]}")
    lines.append("")
    lines.append(f"対象: JPY介入ニュース検知（score>={MIN_SCORE}）、USDJPYの事後値動き")
    lines.append(f"検知件数: {len(rows)}件")
    lines.append("")
    lines.append("## サマリー（下落＝円高＝介入意図通りをヒットとみなす）")
    lines.append("")
    lines.append("| 期間 | サンプル数 | 的中率 | 平均値幅(pips) |")
    lines.append("|---|---|---|---|")
    for label in ("+1d", "+3d", "+5d"):
        s = summary.get(label)
        if s is None:
            lines.append(f"| {label} | - | - | - |")
        else:
            lines.append(f"| {label} | {s['n']} | {s['hit_rate']}% | {s['avg_pips']} |")
    lines.append("")
    lines.append("## 個別検知")
    lines.append("")
    lines.append("| 日付 | スコア | レベル | 起点USDJPY | +1d | +3d | +5d |")
    lines.append("|---|---|---|---|---|---|---|")
    for r in rows:
        lines.append(
            f"| {r['date']} | {r['score']} | {r['level']} | {r['base']:.3f} | "
            f"{r['+1d']} | {r['+3d']} | {r['+5d']} |"
        )
    lines.append("")
    lines.append(
        "※ 的中率が低い/サンプルが少ない場合、apply_intervention_news_filter の"
        "自動降格閾値（現状 score>=45 でHIGH扱い）を見直す判断材料にする。"
    )
    return "\n".join(lines)


def main():
    entries = load_diary_entries()
    alerts = dedupe_by_date(entries, "JPY")
    print(f"[INFO] JPY介入ニュース検知（score>={MIN_SCORE}）: {len(alerts)}件（日付ユニーク）")

    if not alerts:
        print("[INFO] 検証可能な検知データがまだ蓄積されていません。運用を続けて再実行してください。")
        return

    rows = analyze_jpy_impact(alerts)
    if not rows:
        print("[WARN] 値動きデータが取得できず、レポートを生成できませんでした")
        return

    summary = summarize(rows)
    report = format_report(rows, summary)

    print("\n" + report)

    os.makedirs(os.path.dirname(REPORT_PATH), exist_ok=True)
    with open(REPORT_PATH, "w", encoding="utf-8") as f:
        f.write(report)
    print(f"\n[OK] レポートを {REPORT_PATH} に保存しました")


if __name__ == "__main__":
    main()
