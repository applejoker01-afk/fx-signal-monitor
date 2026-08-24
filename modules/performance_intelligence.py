"""
performance_intelligence.py
パフォーマンス知能モジュール

⑩ 自己学習型シグナル重み付け（closed_tradesの実績で信頼度調整）
⑪ ドローダウン監視・連敗クールダウン
⑬ 相場局面判定（トレンド/レンジ）
"""

import json
import os
from datetime import datetime, timedelta, timezone


# ============================================================
# バックテスト実証済み ペア静的ベースライン（2026-06-09 180日実績）
# ============================================================

# 完全除外ペア: 流動性・政治リスク or バックテスト実証済み慢性不振で構造的に取引不可
# 2026-06-18: EURUSD(40%)/USDCHF(37.5%) を不振ペアのためハードブロックに昇格
#              ソフトブロック(-1調整)では★4シグナルが届いてしまい実際に取引されていた
# 2026-06-19: NZDJPY(33%・3件)/CADJPY(0%・2件) を追加
#              autoresearch実証: BOJ引き締め + RBNZ/BOC政策でダブル逆風。
#              サンプルは少ないが、マクロ構造（RBNZ ease + JPY strong）で根拠十分。
#              NZDJPY・AUDJPY は高相関のため、NZDJPY除外で AUDJPY を集中管理する。
# 2026-07-22: PLNJPY を追加（7/20のSBIペア拡張分は未検証のまま取引対象になっていた）。
#              run_pair_tuning_experiment.py で180日窓(勝率37.5%/PF0.7/全ペア最大の
#              pips損失)・30日窓(0%・2連敗)の両方で悪化を確認。
#              ※本件はバックテスト2窓のみの根拠（実運用実績はまだ僅少）。1ヶ月後に
#              data/pair_tuning_experiment.json を再生成して妥当性を再確認すること。
PAIR_EXCLUDE = frozenset([
    "INRJPY", "TRYJPY",           # 流動性・政治リスク（当初から）
    "EURUSD", "USDCHF",           # 慢性不振（実証40%/37.5%）2026-06-18追加
    "NZDJPY", "CADJPY",           # BOJ局面でダブル逆風（実証33%/0%）2026-06-19追加
    "PLNJPY",                     # 新規追加ペアの不振（両窓実証37.5%/0%）2026-07-22追加
])

# 静的★調整: バックテスト勝率が明確に良/悪で、closed_tradesが少ない段階でも反映
# 値は adjustment (整数 or 0.5刻み)。build_pair_performance_mapの実績値とマージ
#
# 2026-07-20 autoresearch追記: AUDJPYの静的+1調整は2026-06-09の180日バックテスト
# （72.2%勝率）に基づくが、その後の実運用71件（2026-06-01〜07-02）ではAUDJPY実績は
# 5戦1勝3敗1分（勝率20%、-89.4pips）と大きく乖離。しかも5件全てがLONGで、
# 「LONGはBOJサイクルフィルタでブロック済み」という想定と異なりLONGシグナルが
# ★4-5で通過していた（該当フィルタは2026-06-19導入・これらのトレードの一部は
# それ以前）。バックテストの想定と実運用実績が乖離しているため adjustment を
# +1→+0.5 に引き下げ、build_pair_performance_map による動的な実績評価
# （min_trades=5で既にAUDJPYは対象、実績が悪ければ自動的に負の調整が乗る）
# に判断をより委ねる。詳細: wiki/finance/fx-signal-monitor-nzd-aud-improvement-plan.md
PAIR_STATIC_BASELINE = {
    # 🏆 主力ペア昇格（76.9%勝率）
    "SGDJPY": {"adjustment": +1,  "note": "主力ペア(実証76.9%)"},
    "EURAUD": {"adjustment": +1,  "note": "主力ペア(実証76.9%)"},
    # ⚠️ 静的ベースラインを引き下げ（2026-07-20、旧+1から変更）
    # AUDJPY: バックテスト(72.2%)と実運用71件(20%・全てLONG)が乖離。
    # 動的な build_pair_performance_map の実績評価に判断の重みを移す。
    "AUDJPY": {"adjustment": +0.5, "note": "バックテスト72.2%だが実運用71件は20%(1W/3L/1E)に乖離・要監視"},
    "GBPJPY": {"adjustment": +1,  "note": "好成績(実証69.2%)"},
    # ⚠️ 7/20拡張ペアの低成績組（2026-07-22 run_pair_tuning_experiment.py）:
    # 180日窓のみの片窓根拠のためハードブロックにせずソフト降格(-1)に留める。
    # ★5級のスコアが出た時だけ★4として取引可能になる設計。
    # 30日窓でも悪化が続くようなら PAIR_EXCLUDE への昇格を検討（PLNJPYは昇格済み）。
    "SEKJPY": {"adjustment": -1, "note": "新規ペア不振(180日窓37.5%/PF0.6)・要監視"},
    "EURCHF": {"adjustment": -1, "note": "新規ペア不振(180日窓40.0%/PF0.82)・要監視"},
    # ❌ 慢性不振ペア（40%以下）→ PAIR_EXCLUDEに移動（ハードブロック）
    # "EURUSD": {"adjustment": -1,  "note": "不振ペア(実証40.0%)"},  # 除外済み
    # "USDCHF": {"adjustment": -1,  "note": "不振ペア(実証37.5%)"},  # 除外済み
    # "NZDJPY": {"adjustment": -1,  "note": "不振ペア(実証33%)"},     # 除外済み
    # "CADJPY": {"adjustment": -1,  "note": "不振ペア(実証0%)"},      # 除外済み
}


def apply_static_baseline(perf_map: dict) -> dict:
    """
    closed_tradesの実績マップに静的ベースラインをマージする。
    実績データがある場合は adjustment を合算（ただし -2〜+2 でクランプ）。
    実績データがないペアには静的値をそのまま追加。
    """
    for pair, static in PAIR_STATIC_BASELINE.items():
        if pair in perf_map:
            # 実績あり: adjustmentを加算（累積しすぎないようクランプ）
            combined = perf_map[pair]["adjustment"] + static["adjustment"]
            perf_map[pair]["adjustment"] = max(-2, min(2, combined))
            perf_map[pair]["note"] = perf_map[pair]["note"] + " / " + static["note"]
        else:
            # 実績なし: 静的値をそのまま登録
            perf_map[pair] = {
                "win_rate": None,
                "total": 0,
                "adjustment": static["adjustment"],
                "note": static["note"] + " (静的ベースライン)",
            }
    return perf_map


# ============================================================
# BOJ引き締めサイクル 方向性レジームフィルタ（2026-06-19追加）
# autoresearch: BOJ tightening + 対通貨ease = JPYクロスLONGは構造的逆風
# ============================================================

# JPYがピーク/引き締め継続中と判断するスタンスセット
_JPY_STRONG_STANCES = frozenset(["tighten", "pause"])  # pause=一時停止だがまだ高金利

# 対通貨が"ease"かつ積極的に利下げ中 → LONGは強くブロック
_CCY_EASE_HARD = frozenset(["ease"])                  # stance=="ease"
_CCY_EASE_HARD_MOMENTUM = frozenset(["stable", "accelerating"])  # ease方向が定着
# ease+deceleratingは利下げ減速中（底打ち近し）→ 軽めのペナルティ
_CCY_EASE_SOFT_MOMENTUM = frozenset(["decelerating"])
# trough（底打ち）は次が利上げ → ブロック不要
_CCY_EASE_NO_BLOCK_MOMENTUM = frozenset(["trough"])


def apply_boj_cycle_directional_filter(result: dict, cb_rates: dict) -> dict:
    """
    BOJ引き締めサイクル局面フィルタ: JPYクロスペアの方向性を中銀スタンスで制限する。

    対象: JPYを含む全クロスペア（XXX/JPY 形式）
    条件: JPY スタンスが強い（tighten/pause） AND 対通貨が ease中
    効果:
      - ease + stable/accelerating → LONGを★2にハードブロック（期待値マイナス）
      - ease + decelerating        → LONG★を1段降格（利下げ減速中で完全ブロックは過剰）
    SHORTシグナルには影響しない（BOJ局面ではSHORT有利のため）。

    実取引根拠（2026/5/26〜6/18, 66件）:
      ロング 37%勝率 vs ショート 57%勝率
      AUDJPY ロング -106.6pips（最大損失ペア）
      NZDJPY ロング -90.7pips（最大単発損失）

    Returns:
        result dict with optional added fields:
          regime_filter_applied: bool
          regime_filter_reason: str
    """
    pair = result.get("pair", "")
    direction = result.get("direction", "")

    # JPYクロスかどうか判定（XXJPY or JPY含む）
    if "JPY" not in pair:
        return result

    # LONG系シグナルのみが対象（SHORT/NO_TRADE/WAIT系はスルー）
    if "LONG" not in direction:
        return result

    # cb_rates の形式を正規化（"rates" キーがある場合とフラットな場合の両方に対応）
    rates_data = cb_rates.get("rates", cb_rates) if isinstance(cb_rates, dict) else {}

    # JPYのスタンスを取得
    jpy_info = rates_data.get("JPY", {})
    jpy_stance = jpy_info.get("stance", "neutral")
    if jpy_stance not in _JPY_STRONG_STANCES:
        # JPYが中立 or easeなら円高圧力がないためフィルタ不要
        return result

    # 対通貨（非JPY側）を特定
    # PAIR_API形式: AUDJPY → ("AUD", "JPY"), SGDJPY → ("SGD", "JPY")
    # JPYが後ろにあるパターン（XXX/JPY）の場合、FROM通貨 = pair[:3]
    # JPYが前にあるパターン（JPY/XXX）はPAIR_APIに存在しないため考慮不要
    non_jpy_ccy = pair.replace("JPY", "")
    if len(non_jpy_ccy) != 3:
        return result

    other_info = rates_data.get(non_jpy_ccy, {})
    other_stance = other_info.get("stance", "neutral")
    other_momentum = other_info.get("rate_momentum", "stable")

    if other_stance not in _CCY_EASE_HARD:
        # 対通貨が ease でない → フィルタ不要（neutral/tighten は問題なし）
        return result

    if other_momentum in _CCY_EASE_NO_BLOCK_MOMENTUM:
        # trough（底打ち）= もうすぐ利上げ転換 → ブロック不要
        return result

    cb_other = other_info.get("cb_name", non_jpy_ccy)
    cb_jpy = jpy_info.get("cb_name", "日銀")

    if other_momentum in _CCY_EASE_HARD_MOMENTUM:
        # ease + stable/accelerating = 積極利下げ中 → ハードブロック（★2固定）
        original_stars = result.get("stars", 1)
        result["stars"] = min(2, original_stars)  # ★2以下に抑制（元が★1ならそのまま）
        reason = (
            f"⚠️ BOJサイクルフィルタ: {non_jpy_ccy}({cb_other} ease/{other_momentum}) + "
            f"JPY({cb_jpy} {jpy_stance}) = LONGはダブル逆風"
        )
        result["verdict"] = f"🔻 {reason.lstrip('⚠️ ')}"
        result["direction"] = "NO_TRADE"  # 取引しない
        result["regime_filter_applied"] = True
        result["regime_filter_reason"] = reason
    elif other_momentum in _CCY_EASE_SOFT_MOMENTUM:
        # ease + decelerating = 利下げ減速中 → ★1段降格（見送り推奨だが禁止ではない）
        original_stars = result.get("stars", 1)
        new_stars = max(1, original_stars - 1)
        reason = (
            f"⚠️ BOJサイクル軽警告: {non_jpy_ccy}({cb_other} ease/{other_momentum}) + "
            f"JPY({cb_jpy} {jpy_stance}) = LONG方向注意"
        )
        if new_stars != original_stars:
            result["stars"] = new_stars
            result["regime_filter_applied"] = True
            result["regime_filter_reason"] = reason

    return result


# ============================================================
# VIXレジームフィルタ（2026-06-22追加）
# autoresearch: wiki/finance/vix-fx-signal-filter.md
# VIX×JPY安全資産 — キャリー崩壊リスクをVIX実数値で判定
# 参考: 2024年8月5日 VIX日中~65・USD/JPY -14%・日経 -12.4%
# ============================================================

def apply_vix_regime_filter(result: dict, sentiment: dict) -> dict:
    """
    VIXレジームフィルタ: 高VIX局面でJPYクロスのLONGをブロック/降格する。

    VIXの実数値だけで判定する。risk_mode は金・債券など複数の材料からも
    risk_off になり得るため、このフィルタの発動条件には使わない。

      VIX > 30 : JPYクロスLONG 完全ブロック（NO_TRADE, ★≤2）
      VIX > 25 : JPYクロスLONG ★1段降格
      VIX > 20 : 警告のみ（★変更なし）
      VIX <= 20 または未取得 : フィルタなし

    SHORTシグナルは対象外（キャリー崩壊時のJPY急騰はSHORTに追い風）。
    """
    if not sentiment:
        return result

    vix_value = sentiment.get("vix")
    pair = result.get("pair", "")
    direction = result.get("direction", "")

    # LONG系シグナルのみ対象
    if "LONG" not in direction:
        return result

    # JPYペア以外はスルー（将来の拡張余地として残す）
    if "JPY" not in pair:
        return result

    # VIX未取得・不正値では保守的にフィルタを掛けず、観測値が閾値を超える時だけ作用させる。
    try:
        vix_value = float(vix_value)
    except (TypeError, ValueError):
        return result

    vix_str = f"VIX={vix_value:.1f}"

    if vix_value > 30:
        # VIX > 30 = 2024年8月型キャリー崩壊リスク → ハードブロック
        result["stars"] = min(2, result.get("stars", 1))
        result["direction"] = "NO_TRADE"
        result["vix_filter_applied"] = True
        result["vix_filter_reason"] = (
            f"VIXパニックフィルタ: {vix_str} — "
            f"キャリー崩壊リスク: JPYクロスLONG禁止"
        )

    elif vix_value > 25:
        # VIX > 25 = キャリー不安定化 → ★1段降格
        original_stars = result.get("stars", 1)
        new_stars = max(1, original_stars - 1)
        if new_stars != original_stars:
            result["stars"] = new_stars
            result["vix_filter_applied"] = True
            result["vix_filter_reason"] = (
                f"VIXリスクオフフィルタ: {vix_str} — "
                f"JPYクロスLONG -{original_stars - new_stars}★降格"
            )

    elif vix_value > 20:
        # VIX > 20 = 警告のみ（★変更なし、情報付与のみ）
        result["vix_caution"] = True
        result["vix_caution_reason"] = (
            f"VIX警戒域: {vix_str} — JPYクロスLONGは要注意"
        )

    return result


# ============================================================
# 💸 スプレッド/ATR比フィルタ（2026-06-23 追加）
# ============================================================

def apply_spread_filter(result: dict) -> dict:
    """
    スプレッド/ATR 比に応じてエキゾチック系シグナルを降格する。

    根拠: Frankfurter API は中値(mid)のみ提供のため、bid/ask スプレッドが
    広い通貨では、実際の ASK エントリーから見ると SL に既に近い状態となる。
    特に ZARJPY/TRYJPY/INRJPY/MXNJPY などエキゾチック系は致命的。

    閾値:
      spread/ATR > 30%  → ★≤2 強制（実質取引禁止）
      spread/ATR > 10%  → ★≤3 上限（エントリー注意）
      spread/ATR ≤ 10%  → 影響軽微・降格なし

    staged_tp['spread_atr_ratio'] を見て判定する。
    """
    staged = result.get("staged_tp") or {}
    ratio = staged.get("spread_atr_ratio")
    spread_pips = staged.get("spread_pips", 0)
    if ratio is None or ratio == 0:
        return result

    pair = result.get("pair", "")
    rr_eff = staged.get("rr_effective")
    rr_mid = staged.get("rr_tp")

    if ratio > 0.30:
        # 致命的スプレッド: ★≤2 強制
        original_stars = result.get("stars", 1)
        new_stars = min(2, original_stars)
        if new_stars != original_stars:
            result["stars"] = new_stars
            result["spread_filter_applied"] = True
            result["spread_filter_reason"] = (
                f"💸 スプレッド致命的: {pair} spread={spread_pips:.1f}pips "
                f"({ratio*100:.0f}% of ATR) — 実効RR 1:{rr_mid}→1:{rr_eff} に劣化。"
                f"取引非推奨で★{original_stars}→★{new_stars}降格。"
            )
    elif ratio > 0.10:
        # スプレッド広い: ★≤3 上限
        original_stars = result.get("stars", 1)
        new_stars = min(3, original_stars)
        if new_stars != original_stars:
            result["stars"] = new_stars
            result["spread_filter_applied"] = True
            result["spread_filter_reason"] = (
                f"💸 スプレッド広め: {pair} spread={spread_pips:.1f}pips "
                f"({ratio*100:.0f}% of ATR) — 実効RR 1:{rr_mid}→1:{rr_eff}。"
                f"★{original_stars}→★{new_stars}降格。"
            )
        else:
            # 元から★3以下でも警告だけ残す
            result["spread_caution"] = True
            result["spread_caution_reason"] = (
                f"💸 スプレッド広め: {pair} spread={spread_pips:.1f}pips "
                f"({ratio*100:.0f}% of ATR)。実効RR 1:{rr_eff}"
            )

    return result


# ============================================================
# 🕐 セッションフィルタ（2026-07-20追加）
# autoresearch: wiki/finance/fx-signal-monitor-nzd-aud-improvement-plan.md
# closed_trades.jsonl 71件をUTCセッション区分で実集計した結果、
# 「Londonセッション単独（08-13 UTC）」が全ペア共通で決着勝率11.1%(n=9)と
# 際立って悪いことが判明。Tokyo(00-08 UTC)も-271.6pipsと不振だが決着勝率
# 自体は42.9%と極端ではないため、閾値超過が明確なLondon単独枠のみを対象とする。
# ============================================================

_LONDON_ONLY_START_HOUR = 8   # UTC
_LONDON_ONLY_END_HOUR = 13    # UTC (exclusive)


def apply_session_filter(result: dict, now) -> dict:
    """
    Londonセッション単独（08-13 UTC、日本時間17-22時）でのシグナルを降格する。

    実データ根拠（closed_trades.jsonl 71件、2026-07-20実データ検証）:
      London単独(08-13 UTC): n=9, 決着勝率11.1%, 合計-170.4pips（全区分中最悪）
      比較) Overlap(13-17 UTC): 56.2% / NY(17-22 UTC): 57.1%

    効果: ★≤2 に抑制（実質エントリー禁止。stars>=4のみが実取引される設計のため）。
    サンプルはn=9とやや小さいため、NO_TRADEへのハードブロックではなく
    ★上限による降格に留める（他フィルタのVIX panicほど強くしない）。
    """
    from datetime import timezone as _tz

    if now is None:
        return result

    hour = now.astimezone(_tz.utc).hour if now.tzinfo else now.hour
    if not (_LONDON_ONLY_START_HOUR <= hour < _LONDON_ONLY_END_HOUR):
        return result

    pair = result.get("pair", "")
    original_stars = result.get("stars", 1)
    new_stars = min(2, original_stars)
    if new_stars != original_stars:
        result["stars"] = new_stars
        result["session_filter_applied"] = True
        result["session_filter_reason"] = (
            f"🕐 セッションフィルタ: {pair} London単独枠({hour:02d}:00 UTC, 08-13 UTC) — "
            f"実データで決着勝率11.1%(n=9)と最弱の時間帯。★{original_stars}→★{new_stars}降格。"
        )
    else:
        result["session_caution"] = True
        result["session_caution_reason"] = (
            f"🕐 London単独枠({hour:02d}:00 UTC) — 実データ最弱の時間帯"
        )

    return result


# ============================================================
# 📅 季節性フィルタ（2026-07-20追加）
# autoresearch: wiki/finance/nzdjpy-audjpy-pair-specific-analysis.md
# AUDJPYは過去20-24年で8月が最弱の月（平均-1.58〜-1.66%、下落率70%、
# 夏季リスク選好低下+円安全資産フロー）と複数ソースで確認（seasonax.com）。
# NZDJPYは直接データ未確認のため対象外（推測で適用しない）。
# ============================================================

_AUDJPY_WEAK_MONTH = 8  # August


def apply_seasonal_filter(result: dict, now) -> dict:
    """
    AUDJPYの8月季節性弱含みをLONGシグナルの確信度に反映する。

    実証根拠（seasonax.com, 過去20-24年統計）:
      8月平均リターン: -1.58%〜-1.66%
      下落確率: 70%（20年間）
      最大下落: 2007年8月-8.38%、2008年8月-6.25%（ともにリスクオフ局面）

    NZDJPYは同種データを未確認のため対象外。
    SHORTシグナルは季節性と方向が一致するため対象外（降格しない）。
    効果: ★≤3 上限（ハードブロックではなく、テクニカル・金利差など他要因との
    総合判断の余地を残す中程度の警告）。
    """
    if now is None:
        return result

    pair = result.get("pair", "")
    direction = result.get("direction", "")

    if pair != "AUDJPY" or "LONG" not in direction:
        return result

    month = now.month
    if month != _AUDJPY_WEAK_MONTH:
        return result

    original_stars = result.get("stars", 1)
    new_stars = min(3, original_stars)
    if new_stars != original_stars:
        result["stars"] = new_stars
        result["seasonal_filter_applied"] = True
        result["seasonal_filter_reason"] = (
            f"📅 季節性フィルタ: AUDJPY 8月は過去20-24年で最弱の月"
            f"(平均-1.58〜-1.66%, 下落率70%) — LONGを★{original_stars}→★{new_stars}降格。"
        )
    else:
        result["seasonal_caution"] = True
        result["seasonal_caution_reason"] = (
            "📅 AUDJPY 8月は季節的に弱含みやすい時期（過去20-24年で下落率70%）"
        )

    return result


# ============================================================
# ⑩ 自己学習型シグナル重み付け
# ============================================================

# 2026-06-10: TP1/TP2/TP3多段階決済 → 単一TP+トレーリング戦略へ刷新。
# 2026-08-25判明: AUDJPYの実績調整(-2)が、この刷新より前(2026-06-03〜06-08)の
# 旧戦略下での5件（1勝3敗1分・勝率20%）のみで決まっていた。旧戦略はTP到達率が
# 低く（変更前 TP到達率6.5%）現行の単一TP+トレーリングとは決済特性が別物のため、
# 別戦略の実績で現行戦略を評価するのは不当な比較。再検証(run_pair_reverification.py)
# でも同期間のAUDJPYはフィルタなしの生シグナルで280日53.3%/90日42.9%と、この
# -2調整ほど悪くないことを確認済み。min_trades=5も1ヶ月未満のサンプルで
# 確定判定してしまう閾値としては緩すぎた反省を踏まえ、刷新前のトレードは
# 実績調整の対象から除外する。
STRATEGY_CUTOVER_DATE = "2026-06-10"

# 2026-08-25凍結: 動的な実績ベース★調整（勝率に応じてadjustmentを±付与する仕組み）は
# 一時凍結する。理由: 刷新後(STRATEGY_CUTOVER_DATE以降)の全ペア合計はまだ29件
# （9勝15敗5分）、JPY損益が記録されているのはそのうち7件で合計-25,393円。
# min_trades=5というペア単位の閾値はこの規模に対して緩すぎ、数件のブレで
# ★を動かしてしまう（AUDJPYの-2調整が旧戦略時代の5件だけで決まっていた件と同根）。
# 解除条件: ペア単位でmin_trades件数を少なくとも30件程度まで引き上げた上で、
# JPY損益ベースの実績が十分蓄積されてから再評価する。凍結中もwin_rate/totalの
# 集計自体は返す（監視用）が、adjustmentは常に0に固定する。
DYNAMIC_ADJUSTMENT_FROZEN = True


def build_pair_performance_map(closed_trades: list, min_trades: int = 30) -> dict:
    """
    決済済みトレードから、ペアごとの実績勝率を集計して
    信頼度調整マップを作る。

    STRATEGY_CUTOVER_DATE より前にエントリーしたトレードは、決済ロジックが
    別物（旧TP1/2/3多段階）だった旧戦略下の結果なので集計対象から除外する。

    DYNAMIC_ADJUSTMENT_FROZEN が True の間は、win_rate/totalの集計は返すが
    adjustmentは常に0（凍結中である旨をnoteに明記）。

    Returns:
        {
          "AUDJPY": {"win_rate": 67.0, "total": 18, "adjustment": +0.5, "note": "実績良好"},
          "TRYJPY": {"win_rate": 38.0, "total": 15, "adjustment": -1, "note": "実績不振"},
        }
    """
    pair_stats = {}
    for t in closed_trades:
        pair = t.get("pair")
        if not pair:
            continue
        entry_date = t.get("entry_date") or (t.get("entry_time", "")[:10])
        if entry_date and entry_date < STRATEGY_CUTOVER_DATE:
            continue
        if pair not in pair_stats:
            pair_stats[pair] = {"wins": 0, "total": 0, "pips": 0.0}
        pair_stats[pair]["total"] += 1
        if t.get("result") == "WIN":
            pair_stats[pair]["wins"] += 1
        pair_stats[pair]["pips"] += t.get("pips", 0) or 0

    perf_map = {}
    for pair, s in pair_stats.items():
        win_rate = s["wins"] / s["total"] * 100 if s["total"] else 0

        if DYNAMIC_ADJUSTMENT_FROZEN:
            perf_map[pair] = {
                "win_rate": round(win_rate, 1),
                "total": s["total"],
                "adjustment": 0,
                "note": f"動的実績調整は凍結中(サンプル{s['total']}件・2026-08-25〜)",
            }
            continue

        if s["total"] < min_trades:
            # サンプル数が少なすぎる場合は調整なし
            perf_map[pair] = {
                "win_rate": round(win_rate, 1),
                "total": s["total"],
                "adjustment": 0,
                "note": f"サンプル不足({s['total']}件)",
            }
            continue

        # 勝率に応じた★調整
        if win_rate >= 65:
            adjustment = 1
            note = "実績優秀"
        elif win_rate >= 55:
            adjustment = 0.5
            note = "実績良好"
        elif win_rate >= 45:
            adjustment = 0
            note = "実績平均"
        elif win_rate >= 35:
            adjustment = -1
            note = "実績不振"
        else:
            adjustment = -2
            note = "実績低調・要注意"

        perf_map[pair] = {
            "win_rate": round(win_rate, 1),
            "total": s["total"],
            "total_pips": round(s["pips"], 4),
            "adjustment": adjustment,
            "note": note,
        }

    return perf_map


def apply_performance_weighting(result: dict, perf_map: dict) -> dict:
    """
    シグナルに実績ベースの信頼度調整を適用。
    ★を増減させ、resultにperformance情報を付与。
    """
    pair = result.get("pair")
    if pair not in perf_map:
        return result

    perf = perf_map[pair]
    adjustment = perf.get("adjustment", 0)

    # ★4以上のシグナルにのみ調整を適用（誤シグナルの増幅を防ぐ）
    if result.get("stars", 0) >= 4 and adjustment != 0:
        original = result["stars"]
        # adjustmentは0.5刻みだが★は整数なので四捨五入的に適用
        new_stars = original + adjustment
        new_stars = max(1, min(5, round(new_stars)))
        if new_stars != original:
            result["stars"] = int(new_stars)
            result["performance_adjusted"] = True

    result["performance"] = {
        "win_rate": perf.get("win_rate"),
        "total_trades": perf.get("total"),
        "adjustment": adjustment,
        "note": perf.get("note"),
    }
    return result


# ============================================================
# ⑪ ドローダウン監視・連敗クールダウン
# ============================================================

# 2026-08-11追加: 連敗アラートの重複通知抑制用の状態ファイル。
# 背景: check_drawdown_alertは決済履歴から毎回「今の連敗状況」を再計算するだけで、
# 「前回いつ通知したか」を覚えていなかった。そのため決済が止まった状態（新規に
# 勝っても負けてもいない）が何日続いても、毎時スキャンのたびに同じ「3連敗中」
# 判定が繰り返され、それだけでメール送信条件を満たしてしまっていた
# （1日15〜24回の定期スキャン全てでメールが飛ぶ＝「シグナル無しのメールが
# 10通以上/日」の直接原因）。この状態ファイルで「前回通知した連敗内容」を
# 覚えておき、状況が変化（連敗が伸びた／レベルが上がった／新しい連敗が
# 始まった／連敗が解消した）した時だけ再通知する。
DRAWDOWN_STATE_FILE = "data/drawdown_notify_state.json"


def _load_drawdown_state() -> dict:
    if not os.path.exists(DRAWDOWN_STATE_FILE):
        return {}
    try:
        with open(DRAWDOWN_STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save_drawdown_state(state: dict):
    os.makedirs("data", exist_ok=True)
    with open(DRAWDOWN_STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2, default=str)


def check_drawdown_alert(closed_trades: list, recent_n: int = 5) -> dict:
    """
    直近の決済トレードから連敗・ドローダウンを検知。

    Returns:
        {
          "alert": True/False,
          "level": "warning" | "critical" | "none",
          "recent_losses": 4,
          "recent_total": 5,
          "consecutive_losses": 3,
          "recent_pips": -8.5,
          "message": "...",
          "recommendation": "...",
          "is_new_escalation": True/False,  # 前回通知時から状況が変化したか
        }

    is_new_escalation について:
        alertがTrueでも、前回通知した内容（レベル・連敗数・起点となった決済
        時刻）と変わっていなければFalse。呼び出し側はこれをメール送信の
        トリガーに使うことで、「同じ3連敗を毎時間通知し続ける」事態を防ぐ。
        alertの値そのもの（ログ出力用）は従来通り常に正しい現状を返す。
    """
    if not closed_trades:
        if _load_drawdown_state():
            _save_drawdown_state({})
        return {"alert": False, "level": "none", "is_new_escalation": False}

    # 決済時刻でソート（新しい順）
    sorted_trades = sorted(
        closed_trades,
        key=lambda t: t.get("exit_time", ""),
        reverse=True
    )

    recent = sorted_trades[:recent_n]
    if len(recent) < 3:
        return {"alert": False, "level": "none", "recent_total": len(recent), "is_new_escalation": False}

    recent_losses = sum(1 for t in recent if t.get("result") == "LOSS")
    recent_pips = sum(t.get("pips", 0) or 0 for t in recent)

    # 連続損失をカウント（最新から）
    consecutive_losses = 0
    for t in sorted_trades:
        if t.get("result") == "LOSS":
            consecutive_losses += 1
        else:
            break

    # 連敗の起点（＝最新の決済時刻）。これが変わらない限り「同じ連敗が続いている」
    streak_since = sorted_trades[0].get("exit_time") if consecutive_losses > 0 else None

    # 判定
    alert = False
    level = "none"
    message = ""
    recommendation = ""

    if consecutive_losses >= 4:
        alert = True
        level = "critical"
        message = f"🚨 {consecutive_losses}連敗中"
        recommendation = "相場環境が戦略と不一致の可能性。48時間の新規エントリー停止を強く推奨"
    elif consecutive_losses >= 3:
        alert = True
        level = "warning"
        message = f"⚠ {consecutive_losses}連敗中"
        recommendation = "24時間の新規エントリー見送りを推奨。相場局面を再確認"
    elif recent_losses >= 4 and len(recent) >= 5:
        alert = True
        level = "warning"
        message = f"⚠ 直近{len(recent)}件中{recent_losses}件が損失"
        recommendation = "勝率が低下中。ロットを半減するか一時休止を検討"

    # 経過時間が長い場合、「24時間の見送りを推奨」という文言が陳腐化するのを防ぐ
    if alert and streak_since:
        try:
            since_dt = datetime.fromisoformat(str(streak_since).replace("Z", "+00:00"))
            elapsed_hours = (datetime.now(timezone.utc) - since_dt).total_seconds() / 3600
            if elapsed_hours >= 24:
                message += f"（直近の損失決済から{elapsed_hours / 24:.1f}日経過・新規決済なし）"
        except Exception:
            pass

    # --- 重複通知の抑制判定 ---
    state = _load_drawdown_state()
    is_new_escalation = False
    if alert:
        prev_level = state.get("level")
        prev_streak_since = state.get("streak_since")
        prev_consecutive = state.get("consecutive_losses", 0)
        if (level != prev_level
                or streak_since != prev_streak_since
                or consecutive_losses > prev_consecutive):
            is_new_escalation = True
            _save_drawdown_state({
                "level": level,
                "consecutive_losses": consecutive_losses,
                "streak_since": streak_since,
            })
    elif state:
        # 連敗が解消した（勝ち決済が入った等）。次回の連敗発生時にまた通知できるようリセット
        _save_drawdown_state({})

    return {
        "alert": alert,
        "level": level,
        "recent_losses": recent_losses,
        "recent_total": len(recent),
        "consecutive_losses": consecutive_losses,
        "recent_pips": round(recent_pips, 4),
        "message": message,
        "recommendation": recommendation,
        "is_new_escalation": is_new_escalation,
    }


# ============================================================
# ⑬ 相場局面判定（トレンド/レンジ）
# ============================================================

def detect_market_regime(prices: list, period: int = 14) -> dict:
    """
    ADX的な指標で「トレンド相場かレンジ相場か」を判定。

    簡易ADX計算:
      +DM, -DM から方向性指数を求め、トレンドの強さを0〜100で表す。

    Returns:
        {
          "adx": 32.5,
          "regime": "trending" | "ranging" | "weak_trend",
          "regime_label": "トレンド相場",
          "trend_direction": "up" | "down" | "neutral",
          "note": "順張りシグナル有効",
        }
    """
    if not prices or len(prices) < period * 2:
        return {"adx": None, "regime": "unknown", "regime_label": "判定不可"}

    # True Range, +DM, -DM の計算
    plus_dm = []
    minus_dm = []
    tr = []

    for i in range(1, len(prices)):
        high = max(prices[i], prices[i - 1])
        low = min(prices[i], prices[i - 1])
        up_move = prices[i] - prices[i - 1]
        down_move = prices[i - 1] - prices[i]

        plus_dm.append(up_move if (up_move > down_move and up_move > 0) else 0)
        minus_dm.append(down_move if (down_move > up_move and down_move > 0) else 0)
        tr.append(high - low if high != low else abs(prices[i] - prices[i - 1]) or 0.0001)

    def smooth(arr, p):
        if len(arr) < p:
            return sum(arr) / len(arr) if arr else 0
        return sum(arr[-p:]) / p

    atr = smooth(tr, period)
    if atr == 0:
        return {"adx": None, "regime": "unknown", "regime_label": "判定不可"}

    plus_di = (smooth(plus_dm, period) / atr) * 100
    minus_di = (smooth(minus_dm, period) / atr) * 100

    di_sum = plus_di + minus_di
    if di_sum == 0:
        adx = 0
    else:
        dx = abs(plus_di - minus_di) / di_sum * 100
        adx = dx  # 簡易版（本来はDXの移動平均）

    # トレンド方向
    if plus_di > minus_di * 1.1:
        trend_direction = "up"
    elif minus_di > plus_di * 1.1:
        trend_direction = "down"
    else:
        trend_direction = "neutral"

    # 局面判定
    if adx >= 30:
        regime = "trending"
        regime_label = "トレンド相場"
        note = "順張りシグナル有効"
    elif adx >= 20:
        regime = "weak_trend"
        regime_label = "弱トレンド相場"
        note = "順張りやや有効"
    else:
        regime = "ranging"
        regime_label = "レンジ相場"
        note = "順張りは機能しにくい・逆張り向き"

    return {
        "adx": round(adx, 1),
        "plus_di": round(plus_di, 1),
        "minus_di": round(minus_di, 1),
        "regime": regime,
        "regime_label": regime_label,
        "trend_direction": trend_direction,
        "note": note,
    }
