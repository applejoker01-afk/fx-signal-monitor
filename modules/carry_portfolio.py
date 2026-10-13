"""
carry_portfolio.py — 金利差ベースのキャリーポートフォリオ構築

2026-09-11 新設。research/EXPERIMENT_REGISTRY.md の EXP-007 で唯一OOSを生き延びた
「横断（クロスセクショナル）キャリー」を本番構造として実装したもの。

■ 設計の根拠（詳細は wiki/finance/2026-09-09-structural-diagnosis.md）
  - 方向予測（TA/MACD/RSI/★判定）は22年・12変種すべてで買い持ちを超えられなかった
  - 横断キャリーは全変種が train/test 両方でプラス・符号反転ゼロ
  - 現行の「常に円ショート」と比べ、リターン同等でDDが半分
    → ランキングが足しているのは収益ではなく「分散によるリスク低減」

■ 実装に合わせて再検証した数値（research/carry_jpy_numeraire.py, JPY建て・USD含む）
  対象2002-04〜2026-09。train 2002-2016 / test 2016-2026。コスト片道2bp、月次リバランス。

  | 構成                        | test Sharpe | test年率 | test最大DD | 全期間最大DD |
  |-----------------------------|-------------|----------|------------|--------------|
  | diversified 3/3             | +0.34       | +1.84%   |  9.4%      | 36.2%        |
  | diversified 3/3 +ボラ8%     | +0.34       | +1.84%   |  9.4%      | 26.9%        |
  | long_only 上位3             | +0.66       | +6.09%   | 17.6%      | 59.3%        |
  | long_only 上位3 +ボラ8%     | +0.64       | +5.44%   | 17.8%      | 36.1%        |

  long_only はリターンが高いが、これは円安トレンドへの方向性の賭けを含むため。
  全期間の最大DDが59.3%に達し、ボラ管理を入れて初めて36.1%に収まる。
  diversified は円安に依存しない代わりにリターンが小さい。どちらを採るかは
  「方向性リスクをどれだけ取るか」の判断であり、統計だけでは決まらない。

■ 実装上の要点
  weights の総和がゼロ（ドルニュートラルなロング・ショート）である限り、
  ポートフォリオは XXXJPY ペアだけで完全に表現できる。
    通貨cのエクスポージャー = XXXJPYの建玉 n_c
    JPYのエクスポージャー   = -Σ n_c
  したがって n_c = w_c (c≠JPY) と置けば w_JPY = -Σ w_c が自動的に満たされる。
  エキゾチッククロス（CHFSEK等）は不要。

■ 重要な前提の変化（2026-09時点）
  円はもはや最低金利通貨ではない（CHF -0.04% < JPY 1.27%）。
  対円の最大金利差は3年前の5.66%から3.30%へ圧縮している。
  過去の「円ショートで儲かった」実績をそのまま外挿してはならない。
"""
from __future__ import annotations

import csv
import math
import os
import statistics
from bisect import bisect_right
from datetime import date

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FRED_CACHE = os.path.join(REPO, 'data', 'fred_cache')

# 通貨 -> (FRED 3ヶ月インターバンク金利のキャッシュファイル, FRED系列ID)
# 系列IDは data_refresh 側で更新する際に使う。
RATE_SERIES = {
    'USD': ('r_usd.csv', 'IR3TIB01USM156N'),
    'JPY': ('r_jpy.csv', 'IR3TIB01JPM156N'),
    'EUR': ('r_eur.csv', 'IR3TIB01EZM156N'),
    'GBP': ('r_gbp.csv', 'IR3TIB01GBM156N'),
    'AUD': ('r_aud.csv', 'IR3TIB01AUM156N'),
    'NZD': ('r_nzd.csv', 'IR3TIB01NZM156N'),
    'CAD': ('r_cad.csv', 'IR3TIB01CAM156N'),
    'CHF': ('r_chf.csv', 'IR3TIB01CHM156N'),
    'NOK': ('r_nok.csv', 'IR3TIB01NOM156N'),
    'SEK': ('r_sek.csv', 'IR3TIB01SEM156N'),
}

# JPYクロスで建てられる通貨（SBI取扱に存在するもの）。
# USD は USDJPY があるので含む。JPY 自身は建玉の対価として自動的に発生する。
TRADABLE_VS_JPY = ['USD', 'EUR', 'GBP', 'AUD', 'NZD', 'CAD', 'CHF', 'NOK', 'SEK']

DEFAULT_TARGET_VOL = 0.08   # 年率8%。EXP-007で全期間DDを59.1%→36.4%に改善した設定
DEFAULT_VOL_LOOKBACK = 60   # 営業日


# ---------------------------------------------------------------- 金利データ

def _read_fred_csv(path):
    rows = []
    if not os.path.exists(path):
        return rows
    with open(path, encoding='utf-8') as f:
        rd = csv.reader(f)
        next(rd, None)
        for r in rd:
            if len(r) < 2:
                continue
            v = r[1].strip()
            if not v or v == '.':
                continue
            try:
                rows.append((date.fromisoformat(r[0].strip()), float(v)))
            except ValueError:
                continue
    rows.sort()
    return rows


def load_rate_history():
    """通貨 -> [(date, rate), ...] を返す。"""
    out = {}
    for ccy, (fn, _) in RATE_SERIES.items():
        rows = _read_fred_csv(os.path.join(FRED_CACHE, fn))
        if rows:
            out[ccy] = rows
    return out


def rates_as_of(history, as_of=None):
    """指定日時点で判明している最新金利。未来の値は使わない（look-ahead防止）。"""
    as_of = as_of or date.today()
    out = {}
    for ccy, rows in history.items():
        dates = [d for d, _ in rows]
        i = bisect_right(dates, as_of) - 1
        if i >= 0:
            out[ccy] = {'rate': rows[i][1], 'as_of': rows[i][0], 'source': 'FRED-IR3TIB'}
    return out


def load_policy_rates():
    """
    data/central_bank_rates.json の政策金利を読む（手動＋自動更新で維持されている）。

    FREDのIR3TIB（3ヶ月インターバンク）は月次かつ公表遅延が大きく、
    実運用では数ヶ月古い値になる（2026-09時点でEUR/GBPは8ヶ月前）。
    順位付けに8ヶ月前の金利を使うのは危険なため、ライブでは政策金利を主系列とする。

    ただしキャリー収益の実体は政策金利ではなくインターバンク金利差なので、
    両者は完全一致しない。cross_check_rankings() で乖離を検出して警告する。
    """
    path = os.path.join(REPO, 'data', 'central_bank_rates.json')
    if not os.path.exists(path):
        return {}
    import json
    with open(path, encoding='utf-8') as f:
        data = json.load(f)
    default_verified = data.get('stance_last_verified') or data.get('last_updated')
    out = {}
    for ccy, info in (data.get('rates') or {}).items():
        if ccy not in RATE_SERIES:
            continue
        r = info.get('rate')
        if r is None:
            continue
        # 2026-10-13: as_of は「その通貨の金利が最後に確認された日」。
        # 旧実装はファイル全体の last_updated（日次自動取得で毎日書き換わっていた）を
        # 使っていたため、手動メンテ通貨が数ヶ月古くても常に「今日」扱いだった。
        as_of = _latest_date(info.get('last_auto_check'), info.get('last_auto_update'),
                             info.get('stance_verified'), default_verified)
        try:
            out[ccy] = {'rate': float(r), 'as_of': as_of,
                        'source': info.get('auto_source') or 'policy',
                        'stance': info.get('stance')}
        except (TypeError, ValueError):
            continue
    return out


def _latest_date(*values):
    best = None
    for v in values:
        if not v:
            continue
        try:
            d = date.fromisoformat(str(v)[:10])
        except ValueError:
            continue
        best = d if best is None or d > best else best
    return best


def cross_check_rankings(primary, secondary, n_long=3, n_short=3, universe=None):
    """
    2つの金利系列で選択される通貨集合が一致するか検査する。
    不一致は「どちらの系列を使うかで建玉が変わる」ことを意味し、
    その選択は根拠が薄いという警告になる。
    """
    warns = []
    if not primary or not secondary:
        return warns
    ra = rank_by_rate(primary, universe)
    rb = rank_by_rate(secondary, universe)
    if len(ra) < n_long + n_short or len(rb) < n_long + n_short:
        return warns
    for name, a, b in (('ロング', set(ra[:n_long]), set(rb[:n_long])),
                       ('ショート', set(ra[-n_short:]), set(rb[-n_short:]))):
        if a != b:
            warns.append(
                f'{name}脚が金利系列によって変わる: 政策金利={sorted(a)} / '
                f'FRED={sorted(b)} — この選択は根拠が弱い')
    return warns


def near_tie_warnings(rates, n_long=3, n_short=3, margin=0.25, universe=None):
    """
    採用/不採用の境界にある通貨が僅差なら警告する。
    僅差での入れ替えは回転コストだけ払って中身が変わらない典型パターン。
    """
    warns = []
    ranked = rank_by_rate(rates, universe)
    if len(ranked) < n_long + n_short + 1:
        return warns
    if n_long > 0:
        inside, outside = ranked[n_long - 1], ranked[n_long]
        gap = rates[inside]['rate'] - rates[outside]['rate']
        if gap < margin:
            warns.append(f'ロング境界が僅差: {inside}({rates[inside]["rate"]:.2f}%) vs '
                         f'{outside}({rates[outside]["rate"]:.2f}%) 差{gap:.2f}%')
    if n_short > 0:
        inside, outside = ranked[-n_short], ranked[-n_short - 1]
        gap = rates[outside]['rate'] - rates[inside]['rate']
        if gap < margin:
            warns.append(f'ショート境界が僅差: {inside}({rates[inside]["rate"]:.2f}%) vs '
                         f'{outside}({rates[outside]["rate"]:.2f}%) 差{gap:.2f}%')
    return warns


def rate_staleness_warnings(rates, max_days=95):
    """金利データが古すぎないかの検査。月次系列なので通常は最大2ヶ月程度の遅れ。"""
    today = date.today()
    warns = []
    for ccy, info in sorted(rates.items()):
        if info.get('as_of') is None:
            warns.append(f'{ccy}: 金利データの確認日が不明')
            continue
        age = (today - info['as_of']).days
        if age > max_days:
            warns.append(f'{ccy}: 金利データが{age}日前（{info["as_of"]}）で古い')
    return warns


# ---------------------------------------------------------------- ウェイト構築

def rank_by_rate(rates, universe=None):
    """金利の高い順に通貨を並べる。"""
    pool = [c for c in rates if universe is None or c in universe]
    return sorted(pool, key=lambda c: -rates[c]['rate'])


def target_weights(rates, mode='diversified', n_long=3, n_short=3, universe=None):
    """
    目標ウェイト（通貨 -> ウェイト）を返す。総和は mode により異なる。

    mode='diversified': 上位n_longロング / 下位n_shortショート。総和ゼロ。
        research の「横断キャリー3/3」に対応（test Sharpe +0.25, 最大DD 17.0%）。
    mode='long_only'  : 上位n_longのみロング、対価は全額JPY。
        research の「上位N vs JPY」に対応（test Sharpe +0.33, 最大DD 35.2%）。
        リターンは高いがDDが約2倍になる点に注意。
    """
    universe = universe or (set(TRADABLE_VS_JPY) | {'JPY'})
    ranked = rank_by_rate(rates, universe)
    if len(ranked) < max(n_long, n_short) + 1:
        return {}

    w = {}
    if mode == 'long_only':
        for c in ranked[:n_long]:
            if c == 'JPY':
                continue
            w[c] = 1.0 / n_long
        w['JPY'] = -sum(w.values())
        return w

    for c in ranked[:n_long]:
        w[c] = w.get(c, 0.0) + 1.0 / n_long
    for c in ranked[-n_short:]:
        w[c] = w.get(c, 0.0) - 1.0 / n_short
    # 総和ゼロを保証（上位と下位が重なる極端なケースの保険）
    drift = sum(w.values())
    if abs(drift) > 1e-9 and 'JPY' in w:
        w['JPY'] -= drift
    return {c: v for c, v in w.items() if abs(v) > 1e-9}


def to_jpy_cross_positions(weights):
    """
    通貨ウェイト -> XXXJPY ペアの建玉ウェイト。

    総和ゼロのウェイトなら、c≠JPY について n_c = w_c と置くだけで
    JPYのエクスポージャー -Σn_c が w_JPY に一致する（モジュール冒頭の説明を参照）。
    """
    pos = {}
    for ccy, w in weights.items():
        if ccy == 'JPY' or abs(w) < 1e-9:
            continue
        if ccy not in TRADABLE_VS_JPY:
            continue
        pos[f'{ccy}JPY'] = w
    return pos


def implied_jpy_exposure(positions):
    """XXXJPY建玉から逆算したJPYエクスポージャー。"""
    return -sum(positions.values())


# ---------------------------------------------------------------- ボラ管理

def realized_vol(daily_returns, lookback=DEFAULT_VOL_LOOKBACK):
    """年率換算した実現ボラティリティ。"""
    xs = [r for r in daily_returns[-lookback:] if r is not None]
    if len(xs) < 5:
        return None
    return statistics.stdev(xs) * math.sqrt(252)


def vol_scale_factor(port_vol, target_vol=DEFAULT_TARGET_VOL, cap=1.0):
    """
    目標ボラに合わせた倍率。上限capでレバレッジ増を禁止する（researchと同じ設定）。
    ボラが未知（データ不足）のときは 1.0 を返さず None を返し、
    呼び出し側に「まだスケールできない」ことを明示する。
    """
    if port_vol is None or port_vol <= 1e-6:
        return None
    return min(cap, target_vol / port_vol)


def apply_vol_scale(positions, factor):
    if factor is None:
        return dict(positions)
    return {p: w * factor for p, w in positions.items()}


# ---------------------------------------------------------------- サマリ出力

def describe(rates, weights, positions, scale=None, port_vol=None):
    """人間が判断するための説明テキストを組み立てる。"""
    lines = []
    ranked = rank_by_rate(rates)
    lines.append('■ 金利ランキング')
    for c in ranked:
        mark = ''
        if c in weights:
            mark = '  ← ロング' if weights[c] > 0 else '  ← ショート'
        lines.append(f'   {c}: {rates[c]["rate"]:6.2f}%  ({rates[c]["as_of"]}){mark}')

    lines.append('')
    lines.append('■ 目標建玉（XXXJPY、+はロング / -はショート）')
    if not positions:
        lines.append('   （建玉なし）')
    for p, w in sorted(positions.items(), key=lambda kv: -kv[1]):
        lines.append(f'   {p}: {w:+.4f}')
    lines.append(f'   → 逆算されるJPYエクスポージャー: {implied_jpy_exposure(positions):+.4f}')

    if port_vol is not None:
        lines.append('')
        lines.append(f'■ リスク: 実現ボラ(年率) {port_vol*100:.1f}%'
                     + (f' → 倍率 {scale:.2f}' if scale is not None else ' → 倍率未算出'))
    return '\n'.join(lines)
