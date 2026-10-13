"""
run_carry_paper.py — キャリーポートフォリオのペーパー運用ランナー

**発注は一切行わない。** 実取引の判断は人間が行う。

日次実行を想定:
  1. 金利（FREDキャッシュ）と現在価格を取得
  2. 保有建玉を時価評価（為替変動＋金利差収益）
  3. 前回リバランスから21営業日以上経っていれば、目標建玉を再計算して入れ替え
  4. レポート出力

分散版（ロング・ショート）と長期のみ版（上位N vs JPY）を**同時並行**で走らせ、
実データでどちらが良いかを比較できるようにしている。
（research/carry_jpy_numeraire.py、test 2016-2026、コスト込み）
  - 分散版:     test Sharpe +0.34 / 年率+1.84% / 最大DD  9.4%（全期間26.9%）
  - 長期のみ版: test Sharpe +0.66 / 年率+6.09% / 最大DD 17.6%（全期間59.3%、
                ボラ管理併用で36.1%）。リターンは高いが円安への方向性の賭けを含む。

使い方:
  python run_carry_paper.py              # 日次更新＋レポート
  python run_carry_paper.py --rebalance  # 強制リバランス
  python run_carry_paper.py --status     # 更新せず現状表示のみ
"""
from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from modules import carry_portfolio as cp
from modules import paper_ledger as pl

VARIANTS = {
    'diversified': {
        'file': 'data/paper_portfolio_diversified.json',
        'label': '分散版（上位3ロング / 下位3ショート）',
        'kwargs': dict(mode='diversified', n_long=3, n_short=3),
    },
    'long_only': {
        'file': 'data/paper_portfolio_longonly.json',
        'label': '長期のみ版（上位3 vs JPY）',
        'kwargs': dict(mode='long_only', n_long=3, n_short=0),
    },
}

REPO = os.path.dirname(os.path.abspath(__file__))


def fetch_prices(needed_pairs):
    """現在価格を取得。signal_scanner の実績あるフェッチャを再利用する。"""
    try:
        from signal_scanner import fetch_latest_rates
    except Exception as e:
        print(f'[WARN] 価格取得モジュールの読み込みに失敗: {e}')
        return {}
    try:
        latest = fetch_latest_rates()
    except Exception as e:
        print(f'[WARN] 価格取得に失敗: {e}')
        return {}
    pairs = (latest or {}).get('pairs', {}) or {}
    missing = [p for p in needed_pairs if p not in pairs]
    if missing:
        print(f'[WARN] 価格を取得できないペア: {missing}')
    return pairs


def run_variant(key, rates, prices, force_rebalance=False, status_only=False):
    cfg = VARIANTS[key]
    path = os.path.join(REPO, cfg['file'])
    state = pl.load_state(path)
    if state is None:
        state = pl.new_state(mode=key)
        print(f'[INIT] {cfg["label"]}: 新規台帳を作成')

    print()
    print('#' * 62)
    print(f'# {cfg["label"]}')
    print('#' * 62)

    if status_only:
        print(pl.format_report(state, pl.performance(state)))
        return state

    # 1. 時価評価
    updated, breakdown = pl.mark_to_market(state, prices, rates)
    if updated and breakdown:
        day_ret = sum(b['contrib'] for b in breakdown)
        print(f'[MTM] 本日の損益: {day_ret*100:+.3f}%')
        for b in sorted(breakdown, key=lambda x: -abs(x['contrib'])):
            print(f'      {b["pair"]}: 為替{b["fx_ret"]*100:+.3f}% '
                  f'金利{b["carry"]*100:+.4f}% → 寄与{b["contrib"]*100:+.3f}%')
    elif updated:
        print('[MTM] 建玉なし')
    else:
        print('[MTM] 本日は評価済み（二重計上を防止）')

    # 2. リバランス判定
    if force_rebalance or pl.needs_rebalance(state):
        weights = cp.target_weights(rates, **cfg['kwargs'])
        positions = cp.to_jpy_cross_positions(weights)

        # ボラ管理: 自身のリターン履歴が溜まるまではスケールしない
        # （researchのバックテストと同じウォームアップ挙動）
        rets = pl.daily_returns(state)
        port_vol = cp.realized_vol(rets)
        scale = cp.vol_scale_factor(port_vol) if port_vol is not None else None
        scaled = cp.apply_vol_scale(positions, scale)

        res = pl.rebalance(state, scaled, prices,
                           reason='forced' if force_rebalance else 'monthly')
        print(f'[REBALANCE] 回転率{res["turnover"]:.3f} コスト{res["cost"]*100:.3f}%')
        for ch in res['changes']:
            print(f'      {ch["pair"]}: {ch["from"]:+.4f} → {ch["to"]:+.4f}')
        if res['missing']:
            print(f'      [WARN] 価格未取得で建てられず: {res["missing"]}')
        extra = cp.describe(rates, weights, scaled, scale, port_vol)
    else:
        last = state.get('last_rebalance')
        print(f'[REBALANCE] 不要（前回 {last}、21営業日未満）')
        extra = ''

    pl.save_state(state, path)
    print()
    print(pl.format_report(state, pl.performance(state), extra))
    return state


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--rebalance', action='store_true', help='強制リバランス')
    ap.add_argument('--status', action='store_true', help='更新せず現状表示')
    ap.add_argument('--variant', choices=list(VARIANTS) + ['both'], default='both')
    args = ap.parse_args()

    # 金利は「政策金利（新しい）」を主系列、「FRED IR3TIB（検証で使った系列）」を照合用に使う。
    # FREDは月次＋公表遅延で数ヶ月古くなるため、順位付けの主系列にはしない。
    rates = cp.load_policy_rates()
    fred = cp.rates_as_of(cp.load_rate_history())

    if not rates:
        if not fred:
            print('[ERROR] 金利データがない。research/fetch_g10_history.py で取得してください。')
            return 1
        print('[WARN] 政策金利が読めないためFRED系列で代替する（数ヶ月古い可能性）')
        rates = fred
    else:
        stale = cp.rate_staleness_warnings(rates, max_days=45)
        for w in stale:
            print(f'[WARN] {w}')
        for w in cp.cross_check_rankings(rates, fred):
            print(f'[WARN] {w}')

    for w in cp.near_tie_warnings(rates):
        print(f'[WARN] {w}')

    keys = list(VARIANTS) if args.variant == 'both' else [args.variant]

    prices = {}
    if not args.status:
        needed = set()
        for k in keys:
            w = cp.target_weights(rates, **VARIANTS[k]['kwargs'])
            needed |= set(cp.to_jpy_cross_positions(w))
        for k in keys:
            st = pl.load_state(os.path.join(REPO, VARIANTS[k]['file']))
            if st:
                needed |= set(st.get('positions', {}))
        prices = fetch_prices(sorted(needed))

    for k in keys:
        run_variant(k, rates, prices,
                    force_rebalance=args.rebalance, status_only=args.status)

    print()
    print('=' * 62)
    print('※ このスクリプトは発注しません。実取引の判断はご自身で行ってください。')
    print('=' * 62)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
