"""
paper_ledger.py — キャリーポートフォリオのペーパー運用台帳

2026-09-11 新設。**このモジュールは一切発注しない。** 実取引の判断は人間が行う。

日次で時価評価（為替変動＋金利差収益）し、月次でリバランス指示を出す。
research のバックテストと同じ計算式を使うことで、
「検証した挙動」と「運用中の挙動」が一致することを保証する
（現行システムはここが乖離しており、検証結果が実運用に対応していなかった）。

状態ファイル: data/paper_portfolio.json
"""
from __future__ import annotations

import json
import math
import os
import statistics
from datetime import date, datetime, timezone

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATE_FILE = os.path.join(REPO, 'data', 'paper_portfolio.json')
TRADING_DAYS = 252

# 片道コスト（bp）。researchのバックテストと同値にすること。
COST_BPS = 2.0


def _today():
    return datetime.now(timezone.utc).date()


def new_state(initial_nav=1_000_000.0, mode='diversified'):
    return {
        'schema': 1,
        'mode': mode,
        'initial_nav': initial_nav,
        'nav': initial_nav,
        'started': _today().isoformat(),
        'positions': {},        # pair -> {'weight': float, 'mark': float}
        'last_rebalance': None,
        'last_mark_date': None,
        'daily': [],            # [{'date':..., 'nav':..., 'ret':...}]
        'rebalances': [],       # 履歴
        'note': 'ペーパー運用。実発注は行わない。',
    }


def load_state(path=STATE_FILE):
    if not os.path.exists(path):
        return None
    with open(path, encoding='utf-8') as f:
        return json.load(f)


def save_state(state, path=STATE_FILE):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


# ------------------------------------------------------------------ 時価評価

def daily_carry(pair, rates, days=1):
    """XXXJPY を days 暦日保有したときの金利差収益（率）。

    2026-10-13修正: 旧実装は「実行1回 = 1/252年」で計上していたため、
    土日も実行すると週7/252（検証の約1.4倍）、実行が抜けると過少になった。
    research（carry_jpy_numeraire.excess）と同じ「経過暦日/365」に統一。
    """
    base = pair[:3]
    if base not in rates or 'JPY' not in rates or days <= 0:
        return 0.0
    return (rates[base]['rate'] - rates['JPY']['rate']) / 100.0 * days / 365.0


def _days_between(a, b):
    try:
        return (date.fromisoformat(b) - date.fromisoformat(a)).days
    except (TypeError, ValueError):
        return 0


def mark_to_market(state, prices, rates, as_of=None):
    """
    保有建玉を時価評価してNAVを更新する。同日に二重計上しない。
    戻り値: (更新されたか, 内訳リスト)
    """
    as_of = (as_of or _today()).isoformat()
    if state.get('last_mark_date') == as_of:
        return False, []

    breakdown = []
    total_ret = 0.0
    for pair, pos in state['positions'].items():
        px = prices.get(pair)
        prev = pos.get('mark')
        if px is None or not prev or prev <= 0:
            continue
        fx_ret = px / prev - 1.0
        # 建玉ごとに最後に評価した日からの経過暦日（価格未取得で評価を飛ばした日も
        # 次回にまとめて計上される）
        since = pos.get('mark_date') or state.get('last_mark_date') or as_of
        c = daily_carry(pair, rates, _days_between(since, as_of))
        contrib = pos['weight'] * (fx_ret + c)
        total_ret += contrib
        breakdown.append({
            'pair': pair, 'weight': pos['weight'], 'prev': prev, 'now': px,
            'fx_ret': fx_ret, 'carry': c, 'contrib': contrib,
        })
        pos['mark'] = px
        pos['mark_date'] = as_of

    state['nav'] = state['nav'] * (1.0 + total_ret)
    state['daily'].append({'date': as_of, 'nav': round(state['nav'], 2),
                           'ret': total_ret})
    state['daily'] = state['daily'][-2000:]
    state['last_mark_date'] = as_of
    return True, breakdown


def daily_returns(state):
    return [d['ret'] for d in state.get('daily', []) if d.get('ret') is not None]


# ------------------------------------------------------------------ リバランス

def rebalance(state, target_positions, prices, reason='monthly'):
    """
    目標建玉へ入れ替える。コストを控除する。
    target_positions: {pair: weight}
    """
    old = {p: v['weight'] for p, v in state['positions'].items()}
    turnover = sum(abs(target_positions.get(p, 0.0) - old.get(p, 0.0))
                   for p in set(old) | set(target_positions))
    cost = turnover * COST_BPS / 10_000.0
    state['nav'] = state['nav'] * (1.0 - cost)

    newpos = {}
    missing = []
    for pair, w in target_positions.items():
        px = prices.get(pair)
        if px is None or px <= 0:
            missing.append(pair)
            continue
        newpos[pair] = {'weight': w, 'mark': px, 'mark_date': _today().isoformat()}
    state['positions'] = newpos
    state['last_rebalance'] = _today().isoformat()
    state['rebalances'].append({
        'date': state['last_rebalance'], 'reason': reason,
        'turnover': round(turnover, 4), 'cost': round(cost, 6),
        'positions': {p: round(w, 4) for p, w in target_positions.items()},
        'missing_price': missing,
    })
    state['rebalances'] = state['rebalances'][-200:]
    return {'turnover': turnover, 'cost': cost, 'missing': missing,
            'changes': _diff(old, target_positions)}


def _diff(old, new):
    out = []
    for p in sorted(set(old) | set(new)):
        a, b = old.get(p, 0.0), new.get(p, 0.0)
        if abs(a - b) > 1e-6:
            out.append({'pair': p, 'from': round(a, 4), 'to': round(b, 4)})
    return out


def needs_rebalance(state, min_days=21):
    """前回リバランスから min_days 営業日（平日）以上経過したか。

    2026-10-13修正: 旧実装は暦日21日（≒15営業日）で判定しており、
    検証（21営業日ごと）より約1.4倍頻繁にリバランス＝コストが過大だった。
    """
    last = state.get('last_rebalance')
    if not last:
        return True
    try:
        d = date.fromisoformat(last)
    except ValueError:
        return True
    return _weekdays_between(d, _today()) >= min_days


def _weekdays_between(start, end):
    """start の翌日から end までの平日数。"""
    n = 0
    d = start
    while d < end:
        d = d.fromordinal(d.toordinal() + 1)
        if d.weekday() < 5:
            n += 1
    return n


# ------------------------------------------------------------------ 成績集計

def performance(state):
    rets = daily_returns(state)
    nav = state['nav']
    init = state['initial_nav']
    out = {
        'nav': nav, 'initial_nav': init,
        'total_return': nav / init - 1.0 if init else 0.0,
        'days': len(rets),
    }
    if len(rets) >= 2:
        sd = statistics.stdev(rets)
        mean = statistics.mean(rets)
        out['vol_ann'] = sd * math.sqrt(TRADING_DAYS)
        out['sharpe'] = (mean / sd * math.sqrt(TRADING_DAYS)) if sd else 0.0
        out['t'] = mean / (sd / math.sqrt(len(rets))) if sd else 0.0
    navs = [d['nav'] for d in state.get('daily', [])]
    if navs:
        peak = navs[0]
        mdd = 0.0
        for v in navs:
            peak = max(peak, v)
            mdd = max(mdd, (peak - v) / peak if peak else 0.0)
        out['max_dd'] = mdd
    return out


def format_report(state, perf, extra=''):
    lines = []
    lines.append('=' * 62)
    lines.append(f'  ペーパー運用レポート（{state["mode"]}） {_today()}')
    lines.append('=' * 62)
    lines.append(f'NAV: {perf["nav"]:,.0f}円 （開始 {perf["initial_nav"]:,.0f}円 / '
                 f'{perf["total_return"]*100:+.2f}%）')
    if 'sharpe' in perf:
        lines.append(f'運用{perf["days"]}日  Sharpe={perf["sharpe"]:+.2f} '
                     f'(t={perf["t"]:+.2f})  実現ボラ={perf.get("vol_ann",0)*100:.1f}% '
                     f'最大DD={perf.get("max_dd",0)*100:.1f}%')
    else:
        lines.append(f'運用{perf["days"]}日（統計量を出すにはデータ不足）')
    lines.append('')
    lines.append('■ 現在の建玉')
    if not state['positions']:
        lines.append('   （なし）')
    for p, v in sorted(state['positions'].items(), key=lambda kv: -kv[1]['weight']):
        lines.append(f'   {p}: {v["weight"]:+.4f} @ {v["mark"]}')
    if extra:
        lines.append('')
        lines.append(extra)
    lines.append('')
    lines.append('※ ペーパー運用です。発注は行いません。実取引の判断はご自身で。')
    return '\n'.join(lines)
