"""
決定的検証: (1) 金利ランキングは「常に円ショート」に価値を足しているか
            (2) ボラ連動のエクスポージャー管理はキャリーのDDを改善するか

(1) が重要な理由: 現行システムはJPYクロスを構造的に常に買う＝実質「円ショート」。
    もし単純な円ショートと横断キャリーが同等なら、ランキング機構は不要ということになる。
(2) フェーズ2で唯一一貫した効果（DD低減）をキャリーに適用できるかの確認。
"""
import math
import os
import statistics
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from crosssectional_carry import build, excess_returns, make_carry, equal_long, TD, COST_BPS


def run(rows, weight_fn, label, rebalance_days=21, vol_scale=None):
    """vol_scale=(target_vol, lookback) を渡すと建玉をボラ連動でスケールする。"""
    rets, w_prev, hist = [], {}, []
    for i in range(len(rows) - 1):
        if i % rebalance_days == 0:
            w = weight_fn(rows[i])
            if vol_scale and len(hist) >= vol_scale[1]:
                tgt, lb = vol_scale
                rv = statistics.stdev(hist[-lb:]) * math.sqrt(TD)
                k = min(1.0, tgt / rv) if rv > 1e-6 else 1.0
                w = {c: v * k for c, v in w.items()}
            cost = sum(abs(w.get(c, 0) - w_prev.get(c, 0))
                       for c in set(w) | set(w_prev)) * COST_BPS / 10000
            w_prev = w
        else:
            w, cost = w_prev, 0.0
        er = excess_returns(rows, i)
        r = sum(w.get(c, 0) * er.get(c, 0) for c in w) - cost
        rets.append(r)
        hist.append(r)
    if len(rets) < 2:
        return None
    sd, mean = statistics.stdev(rets), statistics.mean(rets)
    peak = cum = mdd = 0.0
    for r in rets:
        cum += r
        peak = max(peak, cum)
        mdd = max(mdd, peak - cum)
    worst = min(sum(rets[i:i + 60]) for i in range(len(rets) - 60)) if len(rets) > 60 else 0
    return {'label': label, 'sharpe': mean / sd * math.sqrt(TD) if sd else 0,
            'ann': math.exp(mean * TD) - 1, 'mdd': mdd, 'worst60': worst,
            't': mean / (sd / math.sqrt(len(rets))) if sd else 0,
            'calmar': (math.exp(mean * TD) - 1) / mdd if mdd else 0}


def short_jpy(rec):
    """現行システムの実態: 円ショート、対価は他通貨等ウェイトロング。"""
    others = [c for c in rec['px'] if c != 'JPY']
    if 'JPY' not in rec['px'] or not others:
        return {}
    w = {c: 1.0 / len(others) for c in others}
    w['JPY'] = -1.0
    return w


def lowest_funded(rec):
    """最低金利通貨だけをショート、他は等ウェイトロング（円に限定しない版）。"""
    lo = min(rec['r'], key=lambda c: rec['r'][c])
    others = [c for c in rec['px'] if c != lo]
    w = {c: 1.0 / len(others) for c in others}
    w[lo] = -1.0
    return w


def fmt(r):
    return (f"{r['label']:34s} Sharpe={r['sharpe']:+5.2f} t={r['t']:+5.2f} "
            f"年率={r['ann']*100:+6.2f}% 最大DD={r['mdd']*100:5.1f}% "
            f"Calmar={r['calmar']:+5.2f} 最悪60日={r['worst60']*100:+6.1f}%")


def main():
    rows = build()
    split = int(len(rows) * 0.6)

    variants = [
        ('ドリフト基準: 全通貨Long', equal_long, None),
        ('現行相当: 常に円ショート', short_jpy, None),
        ('最低金利通貨をショート', lowest_funded, None),
        ('横断キャリー 3/3', make_carry(3, 3), None),
        ('横断キャリー 3/3 +ボラ管理10%', make_carry(3, 3), (0.10, 60)),
        ('横断キャリー 3/3 +ボラ管理8%', make_carry(3, 3), (0.08, 60)),
        ('横断キャリー 2/2 +ボラ管理10%', make_carry(2, 2), (0.10, 60)),
    ]

    for name, data in [('train', rows[:split]), ('test（判定はここ）', rows[split:])]:
        print('=' * 104)
        print(f'【{name}】')
        print('=' * 104)
        for label, fn, vs in variants:
            r = run(data, fn, label, vol_scale=vs)
            if r:
                print(fmt(r))
        print()

    print('=' * 104)
    print('【全期間・参考】')
    print('=' * 104)
    for label, fn, vs in variants:
        r = run(rows, fn, label, vol_scale=vs)
        if r:
            print(fmt(r))
    print(f'\n注: 累計変種数が増えている（フェーズ1:12 + フェーズ2:8 + キャリー:5 + 本検証:7 = 32）。')
    print('   多重検定として、単発のt値2.0前後は有意とみなさないこと。')


if __name__ == '__main__':
    main()


def topn_vs_jpy(n):
    """実装可能版: 高金利上位n通貨をJPYクロスでロング（=円ショート分散）。
    SBIで実際に発注できる形（AUDJPY/NZDJPY等のロングのみ）に対応する。"""
    def f(rec):
        cands = [c for c in rec['r'] if c != 'JPY' and c in rec['px']]
        if 'JPY' not in rec['px'] or not cands:
            return {}
        top = sorted(cands, key=lambda c: -rec['r'][c])[:n]
        w = {c: 1.0 / n for c in top}
        w['JPY'] = -1.0
        return w
    return f


def extra():
    rows = build()
    split = int(len(rows) * 0.6)
    vs = [
        ('実装可能: 高金利上位1 vs JPY', topn_vs_jpy(1), None),
        ('実装可能: 高金利上位2 vs JPY', topn_vs_jpy(2), None),
        ('実装可能: 高金利上位3 vs JPY', topn_vs_jpy(3), None),
        ('実装可能: 上位3 vs JPY +ボラ管理10%', topn_vs_jpy(3), (0.10, 60)),
        ('実装可能: 上位3 vs JPY +ボラ管理8%', topn_vs_jpy(3), (0.08, 60)),
        ('参考: 全通貨 vs JPY（円ショート）', short_jpy, None),
        ('参考: 横断キャリー3/3', make_carry(3, 3), None),
    ]
    for name, data in [('train', rows[:split]), ('test（判定）', rows[split:]), ('全期間', rows)]:
        print('=' * 104)
        print(f'【{name}】')
        print('=' * 104)
        for label, fn, v in vs:
            r = run(data, fn, label, vol_scale=v)
            if r:
                print(fmt(r))
        print()
