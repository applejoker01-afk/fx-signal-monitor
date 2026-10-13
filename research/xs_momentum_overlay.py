"""
EXP-008: 横断モメンタムのキャリーへの重ね合わせ（2026-10-13 事前登録）

ルールは research/EXPERIMENT_REGISTRY.md の EXP-008 に実行前に固定した。
データ・母集団・リターン定義・train/test分割は EXP-007b（carry_jpy_numeraire.py）と同一。

実行: python research/xs_momentum_overlay.py
"""
import math
import os
import statistics
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import crosssectional_carry as cc  # noqa: E402
from carry_jpy_numeraire import to_jpy_frame, excess  # noqa: E402

LOOKBACK, SKIP = 252, 21   # 12-1 モメンタム
N = 3                      # 上位3ロング / 下位3ショート
REBAL = 21


def cum_excess(rows):
    """各通貨の累積超過リターン系列（JPY=0）。cum[i][c] = 0..i までの合計。"""
    cum = [{c: 0.0 for c in rows[0]['px']}]
    for i in range(len(rows) - 1):
        er = excess(rows, i)
        prev = cum[-1]
        cum.append({c: prev.get(c, 0.0) + er.get(c, 0.0) for c in set(prev) | set(er)})
    return cum


def momentum_at(cum, i):
    if i - LOOKBACK < 0:
        return None
    a, b = cum[i - LOOKBACK], cum[i - SKIP]
    return {c: b[c] - a[c] for c in b if c in a}


def weights_from_ranking(ranked):
    if len(ranked) < 2 * N:
        return {}
    w = {}
    for c in ranked[:N]:
        w[c] = w.get(c, 0) + 1.0 / N
    for c in ranked[-N:]:
        w[c] = w.get(c, 0) - 1.0 / N
    return w


def universe(rec):
    # JPY建てフレームでは JPY 自身は px に無いが金利 r に含まれる（ショート候補）
    return sorted(rec['r'].keys())


def make_carry():
    def f(i, rows, cum):
        rec = rows[i]
        return weights_from_ranking(sorted(universe(rec), key=lambda c: -rec['r'][c]))
    return f


def make_mom():
    def f(i, rows, cum):
        m = momentum_at(cum, i)
        if m is None:
            return {}
        m = dict(m, JPY=0.0)
        pool = [c for c in universe(rows[i]) if c in m]
        return weights_from_ranking(sorted(pool, key=lambda c: -m[c]))
    return f


def make_composite():
    def f(i, rows, cum):
        m = momentum_at(cum, i)
        if m is None:
            return {}
        m = dict(m, JPY=0.0)
        rec = rows[i]
        pool = [c for c in universe(rec) if c in m]
        carry_rank = {c: k for k, c in enumerate(sorted(pool, key=lambda c: -rec['r'][c]))}
        mom_rank = {c: k for k, c in enumerate(sorted(pool, key=lambda c: -m[c]))}
        ranked = sorted(pool, key=lambda c: (carry_rank[c] + mom_rank[c], carry_rank[c]))
        return weights_from_ranking(ranked)
    return f


def run(rows, cum, wfn, start, end, cost_bps):
    """[start, end) 区間を運用。モメンタム計測のため cum は全期間のものを使う（過去参照のみ）。"""
    rets, w_prev, w = [], {}, {}
    for k, i in enumerate(range(start, end - 1)):
        cost = 0.0
        if k % REBAL == 0:
            w = wfn(i, rows, cum)
            cost = sum(abs(w.get(c, 0) - w_prev.get(c, 0))
                       for c in set(w) | set(w_prev)) * cost_bps / 10000
            w_prev = w
        er = excess(rows, i)
        rets.append(sum(w.get(c, 0) * er.get(c, 0) for c in w if c != 'JPY') - cost)
    sd, mean = statistics.stdev(rets), statistics.mean(rets)
    peak = cumr = mdd = 0.0
    for x in rets:
        cumr += x
        peak = max(peak, cumr)
        mdd = max(mdd, peak - cumr)
    worst = min(sum(rets[j:j + 60]) for j in range(len(rets) - 60))
    return {'sharpe': mean / sd * math.sqrt(cc.TD) if sd else 0.0,
            't': mean / (sd / math.sqrt(len(rets))) if sd else 0.0,
            'ann': math.exp(mean * cc.TD) - 1, 'mdd': mdd, 'worst60': worst,
            'rets': rets}


def corr(a, b):
    n = min(len(a), len(b))
    return statistics.correlation(a[:n], b[:n])


def main():
    rows = to_jpy_frame(cc.build())
    cum = cum_excess(rows)
    split = int(len(rows) * 0.6)
    # train はモメンタムの計測期間（LOOKBACK）を確保した地点から開始。比較対象も同じ区間に揃える
    periods = {'train': (LOOKBACK, split), 'test': (split, len(rows))}
    print(f"通貨: {universe(rows[-1])}")
    print(f"train {rows[LOOKBACK]['date']}〜{rows[split-1]['date']} / "
          f"test {rows[split]['date']}〜{rows[-1]['date']}\n")

    strategies = [('carry 3/3 (baseline)', make_carry()),
                  ('composite carry+mom 3/3 (主仮説)', make_composite()),
                  ('参考: momentum 3/3', make_mom())]
    res = {}
    for cost in (1.0, 2.0, 5.0):
        print('=' * 100)
        print(f'コスト 片道{cost}bp')
        for pname, (s, e) in periods.items():
            for label, fn in strategies:
                r = run(rows, cum, fn, s, e, cost)
                res[(cost, pname, label)] = r
                print(f"  [{pname:5s}] {label:32s} Sharpe={r['sharpe']:+5.2f} t={r['t']:+5.2f} "
                      f"年率={r['ann']*100:+6.2f}% 最大DD={r['mdd']*100:5.1f}% 最悪60日={r['worst60']*100:+6.1f}%")
        print()

    B, C = 'carry 3/3 (baseline)', 'composite carry+mom 3/3 (主仮説)'
    d_test = res[(2.0, 'test', C)]['sharpe'] - res[(2.0, 'test', B)]['sharpe']
    d_train = res[(2.0, 'train', C)]['sharpe'] - res[(2.0, 'train', B)]['sharpe']
    dd_ratio = res[(2.0, 'test', C)]['mdd'] / res[(2.0, 'test', B)]['mdd']
    d_test5 = res[(5.0, 'test', C)]['sharpe'] - res[(5.0, 'test', B)]['sharpe']
    gates = [('① test Sharpe差>0', d_test > 0, f'{d_test:+.3f}'),
             ('② train Sharpe差>0', d_train > 0, f'{d_train:+.3f}'),
             ('③ test最大DD比<=1.2', dd_ratio <= 1.2, f'{dd_ratio:.2f}'),
             ('④ 5bpでもtest差>0', d_test5 > 0, f'{d_test5:+.3f}')]
    print('=' * 100)
    print('判定ゲート（2bp基準）')
    for name, ok, v in gates:
        print(f"  {name:22s} {v:>8s}  {'PASS' if ok else 'FAIL'}")
    print(f"\n  status = {'review' if all(ok for _, ok, _ in gates) else 'rejected'}")
    print(f"\n  参考: test期 日次リターン相関 carry vs momentum = "
          f"{corr(res[(2.0, 'test', B)]['rets'], res[(2.0, 'test', '参考: momentum 3/3')]['rets']):+.2f}")


if __name__ == '__main__':
    main()
