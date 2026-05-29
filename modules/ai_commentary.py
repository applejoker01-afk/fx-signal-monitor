"""
ai_commentary.py
Claude API活用モジュール

⑮ AI市況コメンタリー : 全シグナルを俯瞰した自然言語の市況解説
⑯ AIトレード講評     : 決済トレードを「なぜ勝った/負けた」とAIが分析
⑰ AI週次総括         : 週次レポートに学びと改善提案を添える

ANTHROPIC_API_KEY が未設定の場合は全機能スキップ（コスト0・後方互換）。
"""

import json
import os
import urllib.request


ANTHROPIC_API_URL = "https://api.anthropic.com/v1/messages"
# コスト重視でHaiku、品質重視ならsonnetに変更可
MODEL = "claude-haiku-4-5-20251001"


def _call_claude(prompt: str, max_tokens: int = 1024, system: str = None) -> str:
    """
    Claude APIを呼び出してテキスト応答を返す。
    APIキー未設定・エラー時は None を返す（呼び出し側でスキップ）。
    """
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        return None

    body = {
        "model": MODEL,
        "max_tokens": max_tokens,
        "messages": [{"role": "user", "content": prompt}],
    }
    if system:
        body["system"] = system

    try:
        req = urllib.request.Request(
            ANTHROPIC_API_URL,
            data=json.dumps(body).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=40) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            # content配列からtextブロックを結合
            texts = [
                blk.get("text", "")
                for blk in data.get("content", [])
                if blk.get("type") == "text"
            ]
            return "\n".join(texts).strip()
    except urllib.error.HTTPError as e:
        body_txt = e.read().decode("utf-8", errors="ignore")[:200]
        print(f"[WARN] Claude API HTTP {e.code}: {body_txt}")
        return None
    except Exception as e:
        print(f"[WARN] Claude API failed: {e}")
        return None


# ============================================================
# ⑮ AI市況コメンタリー
# ============================================================

def generate_market_commentary(results: list, sentiment: dict,
                                currency_strength: dict,
                                market_regime: dict = None) -> str:
    """
    全シグナルとセンチメントを俯瞰した市況解説を生成。
    """
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return None

    # 上位シグナルを抽出
    strong = [r for r in results if r.get("stars", 0) >= 4]
    strong_summary = "\n".join(
        f"  {r['pair']} {'★'*r['stars']} {r.get('direction','')} "
        f"(TA{r.get('ta_score','?')}/FA{r.get('fa_score','?')})"
        for r in strong[:10]
    ) or "  ★4以上のシグナルなし"

    # 通貨強弱トップ・ボトム
    strength_summary = ""
    if currency_strength:
        sorted_cs = sorted(currency_strength.items(), key=lambda x: -x[1]["score"])
        top = ", ".join(f"{c}({v['score']:+.0f})" for c, v in sorted_cs[:3])
        bottom = ", ".join(f"{c}({v['score']:+.0f})" for c, v in sorted_cs[-3:])
        strength_summary = f"強い通貨: {top} / 弱い通貨: {bottom}"

    regime_summary = ""
    if market_regime:
        regime_summary = f"主要ペアの相場局面: {market_regime.get('regime_label','')}"

    prompt = f"""あなたはプロのFXアナリストです。以下の市場データをもとに、日本語で簡潔な市況コメンタリーを書いてください。

【市場センチメント】
VIX: {sentiment.get('vix','N/A')} ({sentiment.get('risk_mode','?')})
DXY: {sentiment.get('dxy','N/A')}
米10年債: {sentiment.get('us10y','N/A')}%
金: {sentiment.get('gold','N/A')}

【通貨強弱】
{strength_summary}

【★4以上のシグナル】
{strong_summary}

{regime_summary}

要件:
- 3〜4文で全体感を述べる
- 今日注目すべきポイントを1つ指摘
- 投資助言ではなく市況の客観的な解説として書く
- 「です・ます」調
- 200字以内"""

    return _call_claude(prompt, max_tokens=512,
                        system="あなたは冷静で客観的なFX市場アナリストです。誇張せず事実ベースで簡潔に解説します。")


# ============================================================
# ⑯ AIトレード講評
# ============================================================

def generate_trade_review(closed_trade: dict) -> str:
    """
    決済済みトレード1件について「なぜ勝った/負けたか」をAIが分析。
    """
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return None

    reason_label = {
        "TP3_HIT": "TP3到達（大勝ち）", "TP2_HIT": "TP2到達（勝ち）",
        "TP1_HIT": "TP1到達（小勝ち）", "SL_HIT": "ストップロス（負け）",
        "SIGNAL_LOST": "シグナル消滅", "REVERSED": "方向反転",
    }.get(closed_trade.get("exit_reason"), closed_trade.get("exit_reason", "?"))

    prompt = f"""以下のFXトレード結果を分析し、簡潔に講評してください。

ペア: {closed_trade.get('pair')}
方向: {closed_trade.get('direction')}
エントリー価格: {closed_trade.get('entry_price')}
決済価格: {closed_trade.get('exit_price')}
決済理由: {reason_label}
損益: {closed_trade.get('pips', 0):+.4f}
保有時間: {closed_trade.get('hold_hours', 0)}時間
エントリー時TA: {closed_trade.get('ta_score', '?')}
エントリー時FA: {closed_trade.get('fa_score', '?')}
ボラレジーム: {closed_trade.get('regime', '?')}

要件:
- 2〜3文で「なぜこの結果になったか」を分析
- 次に活かせる学びを1つ
- 100字以内・です/ます調"""

    return _call_claude(prompt, max_tokens=400,
                        system="あなたはトレード記録を客観的に振り返るコーチです。")


# ============================================================
# ⑰ AI週次総括
# ============================================================

def generate_weekly_summary(stats: dict, backtest_overall: dict = None) -> str:
    """
    週次成績を分析し、学びと改善提案を生成。
    """
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return None

    reason_counts = stats.get("reason_counts", {})
    pair_stats = stats.get("pair_stats", {})

    pair_summary = "\n".join(
        f"  {p}: {s['wins']}/{s['total']}勝 (累計{s['total_pips']:+.3f})"
        for p, s in sorted(pair_stats.items(),
                          key=lambda x: -(x[1]['wins']/x[1]['total'] if x[1]['total'] else 0))[:8]
    )

    backtest_note = ""
    if backtest_overall:
        backtest_note = (
            f"\n【過去180日バックテスト】\n"
            f"勝率: {backtest_overall.get('win_rate','?')}% / "
            f"PF: {backtest_overall.get('profit_factor','?')}"
        )

    prompt = f"""以下のFX自動売買システムの週次成績を分析し、総括と改善提案を書いてください。

【今週の決済トレード】
総数: {stats.get('total_trades',0)}件
勝率: {stats.get('win_rate',0)}%
勝ち: {stats.get('wins',0)} / 負け: {stats.get('losses',0)}
平均保有: {stats.get('avg_hold_hours',0)}時間

【決済理由内訳】
{json.dumps(reason_counts, ensure_ascii=False)}

【ペア別成績】
{pair_summary}
{backtest_note}

要件:
- 今週の総括を2〜3文
- 来週に向けた具体的な改善提案を1〜2点
- 投資助言ではなくシステム運用の振り返りとして
- 250字以内・です/ます調"""

    return _call_claude(prompt, max_tokens=600,
                        system="あなたはトレードシステムの運用を支援する冷静なアナリストです。")
