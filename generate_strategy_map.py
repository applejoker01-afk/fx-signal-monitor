"""
適応的戦略マップ生成

毎週GitHub Actionsで実行:
  1. 直近データで7戦略を全ペア検証
  2. 各ペアで「直近で最も勝てている戦略」を選出（適応的戦略選択）
  3. Claude APIで各ペアの相場局面を判断（AI局面判断）
  4. strategy_map.json に保存
     → daytrade.html がこれを読んで各ペアに最適戦略を適用

「固定ロジックは劣化する」問題への対応:
  市場が変われば、翌週の再検証で勝者戦略が入れ替わる。
"""

import json
import os
import time
import urllib.request
from datetime import datetime, timezone

from modules.daytrade_backtest import run_strategy_comparison

PAIRS = ["USDJPY", "EURUSD", "GBPJPY", "AUDJPY", "EURJPY", "AUDUSD",
         # Step1追加: 主要国の流動性高いクロス
         "GBPUSD", "USDCAD", "USDCHF", "NZDJPY"]

# 戦略の表示名 → daytrade.htmlが使う内部キー
STRATEGY_KEYS = {
    "反発(improveB)": "rebound",
    "反発の逆(reverse)": "reverse",
    "三次元共鳴(順張り)": "resonance",
    "①ブレイクアウト": "breakout",
    "②時間帯特化": "session",
    "③ボラブレイク": "vol_breakout",
    "④平均回帰": "mean_reversion",
}

# 最低トレード数（これ未満は統計的に信頼できないので採用しない）
MIN_TRADES = 30
# 最低期待値（これ未満なら「戦略なし=見送り」とする）
MIN_EXPECTANCY = 0.1


def fetch_15m(pair, days=60):
    symbol = pair + "=X"
    url = (f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
           f"?interval=15m&range={days}d")
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode())
        result = data["chart"]["result"][0]
        q = result["indicators"]["quote"][0]
        ts_all = result.get("timestamp", [])
        highs, lows, closes, opens, timestamps = [], [], [], [], []
        for i in range(len(q["close"])):
            if (q["close"][i] is None or q["high"][i] is None
                    or q["low"][i] is None):
                continue
            highs.append(q["high"][i])
            lows.append(q["low"][i])
            closes.append(q["close"][i])
            opens.append(q["open"][i] if q["open"][i] is not None else q["close"][i])
            timestamps.append(ts_all[i] if i < len(ts_all) else None)
        return {"highs": highs, "lows": lows, "closes": closes,
                "opens": opens, "timestamps": timestamps}
    except Exception as e:
        print(f"[WARN] {pair} 取得失敗: {e}")
        return None


def select_best_strategy(summaries):
    """
    各戦略のサマリーから、最低トレード数を満たし期待値が最も高い戦略を選ぶ。
    どれも基準を満たさなければ None（見送り）。
    """
    candidates = []
    for name, s in summaries.items():
        if s.get("trades", 0) < MIN_TRADES:
            continue
        exp = s.get("expectancy", -999)
        candidates.append((name, exp, s))
    if not candidates:
        return None, None, None
    candidates.sort(key=lambda x: x[1], reverse=True)
    best_name, best_exp, best_s = candidates[0]
    if best_exp < MIN_EXPECTANCY:
        return None, best_exp, best_s  # 勝てる戦略なし
    return best_name, best_exp, best_s


def ai_judge_regime(pair, recent_closes):
    """
    Claude APIで相場局面を判断（AI局面判断）。
    APIキーが無ければ簡易判定にフォールバック。
    """
    # 簡易判定（フォールバック）: 直近の方向性
    def simple_regime():
        if len(recent_closes) < 20:
            return "unknown", "データ不足"
        recent = recent_closes[-20:]
        change = (recent[-1] - recent[0]) / recent[0] * 100
        if abs(change) > 1.0:
            return ("uptrend" if change > 0 else "downtrend",
                    f"直近20本で{change:+.1f}%")
        return "ranging", f"直近20本で{change:+.1f}%（横ばい）"

    if not os.environ.get("ANTHROPIC_API_KEY"):
        return simple_regime()

    try:
        import anthropic
        client = anthropic.Anthropic()
        # 直近の値動きサマリー
        recent = recent_closes[-30:]
        change = (recent[-1] - recent[0]) / recent[0] * 100
        high = max(recent)
        low = min(recent)
        volatility = (high - low) / recent[0] * 100
        prompt = f"""FXの{pair}の直近の15分足の値動きを分析してください。
直近30本（約7.5時間）の変化率: {change:+.2f}%
高値〜安値の幅: {volatility:.2f}%
現在値: {recent[-1]}

この相場が「強いトレンド」「弱いトレンド」「レンジ」のどれかを判断し、
デイトレードで取るべき基本姿勢を一言で。
JSON形式のみで出力: {{"regime": "uptrend|downtrend|ranging", "comment": "20字以内"}}"""
        msg = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=200,
            messages=[{"role": "user", "content": prompt}],
        )
        text = "".join(b.text for b in msg.content if b.type == "text").strip()
        text = text.replace("```json", "").replace("```", "").strip()
        data = json.loads(text)
        return data.get("regime", "unknown"), data.get("comment", "")
    except Exception as e:
        print(f"  [AI判定失敗 {pair}: {e}] 簡易判定にフォールバック")
        return simple_regime()


def main():
    print("=" * 64)
    print("適応的戦略マップ生成（自動再検証 + AI局面判断）")
    print("=" * 64)

    strategy_map = {}

    for pair in PAIRS:
        print(f"\n--- {pair} ---")
        data = fetch_15m(pair)
        if not data or len(data["closes"]) < 250:
            print(f"  データ不足のためスキップ")
            strategy_map[pair] = {"strategy": None, "strategy_key": None,
                                  "reason": "データ不足"}
            continue

        # 1. 7戦略を検証して勝者を選出
        summaries = run_strategy_comparison(data, pair, rr=2.0)
        best_name, best_exp, best_s = select_best_strategy(summaries)

        # 2. AI局面判断
        regime, regime_comment = ai_judge_regime(pair, data["closes"])

        if best_name:
            key = STRATEGY_KEYS.get(best_name, "unknown")
            print(f"  ✅ 採用戦略: {best_name}（期待値{best_exp:+.2f}pips/件・"
                  f"{best_s['trades']}件・勝率{best_s['win_rate']}%）")
            print(f"  🤖 AI局面: {regime}（{regime_comment}）")
            strategy_map[pair] = {
                "strategy": best_name,
                "strategy_key": key,
                "expectancy": best_exp,
                "trades": best_s["trades"],
                "win_rate": best_s["win_rate"],
                "pf": best_s["pf"],
                "regime": regime,
                "regime_comment": regime_comment,
            }
        else:
            print(f"  ⚠ 勝てる戦略なし（期待値が基準未満）→ このペアは見送り")
            print(f"  🤖 AI局面: {regime}（{regime_comment}）")
            strategy_map[pair] = {
                "strategy": None, "strategy_key": None,
                "reason": "期待値が基準未満のため見送り",
                "regime": regime, "regime_comment": regime_comment,
            }

        time.sleep(1)

    # 全戦略の成績も記録（参考用）
    output = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "lookback_days": 60,
        "rr": 2.0,
        "min_trades": MIN_TRADES,
        "min_expectancy": MIN_EXPECTANCY,
        "strategy_map": strategy_map,
        "note": "毎週自動再検証され、市場変化に応じて各ペアの採用戦略が入れ替わります",
    }
    with open("docs/strategy_map.json", "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    print("\n" + "=" * 64)
    print("【戦略マップ】")
    print("=" * 64)
    for pair, m in strategy_map.items():
        if m.get("strategy"):
            print(f"  {pair}: {m['strategy']} (期待値{m.get('expectancy',0):+.2f}) "
                  f"/ AI局面: {m.get('regime')}")
        else:
            print(f"  {pair}: 見送り / AI局面: {m.get('regime')}")
    print("\n結果を docs/strategy_map.json に保存しました")


if __name__ == "__main__":
    main()
