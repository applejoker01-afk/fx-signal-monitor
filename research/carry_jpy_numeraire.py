"""
検証と実装の整合を取るための再検証。

問題: これまでの検証は USD を基準通貨（numeraire）として順位付けから除外していた。
      しかし実装では USDJPY を建てられるため USD も順位対象に含まれる。
      「検証した挙動＝運用する挙動」を保証するには、検証側を実装に合わせる必要がある。

修正: JPY建てで評価し、USD も順位対象に含める。
      通貨cのリターン = Δlog(JPY建て価格_c) + (r_c - r_JPY)/252
      JPY自身のリターン = 0

なお、ウェイト総和がゼロのポートフォリオでは基準通貨の項が相殺されるため、
USDを除外したままなら numeraire を変えても結果は理論上同一。
本当に効くのは「USDを順位対象に加えたこと」の1点だけである。
"""
import math
import os
import statistics
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from crosssectional_carry import build, TD, COST_BPS


def to_jpy_frame(rows):
    """USD建て価格からJPY建てへ変換し、USDを通貨として追加する。"""
    out = []
    for rec in rows:
        if 'JPY' not in rec['px'] or rec['px']['JPY'] <= 0:
            continue
        usd_per_jpy = rec['px']['JPY']
        px, r = {}, {}
        for c, v in rec['px'].items():
            if c == 'JPY':
                continue
            px[c] = v / usd_per_jpy          # JPY建て価格
            r[c] = rec['r'][c]
        px['USD'] = 1.0 / usd_per_jpy         # USDJPY
        r['USD'] = rec['r_usd']
        r['JPY'] = rec['r']['JPY']
        out.append({'date': rec['date'], 'px': px, 'r': r})
    return out


def excess(rows, i):
    """i→i+1 の、JPY調達で各通貨を保有したときのリターン。"""
    a, b = rows[i], rows[i + 1]
    days = max(1, (b['date'] - a['date']).days)
    out = {}
    for c in a['px']:
        if c not in b['px'] or a['px'][c] <= 0:
            continue
        fx = math.log(b['px'][c] / a['px'][c])
        carry = (a['r'][c] - a['r']['JPY']) / 100.0 * days / 365.0
        out[c] = fx + carry
    return out


def run(rows, wfn, label, rebal=21, vol_scale=None):
    rets, w_prev, hist = [], {}, []
    for i in range(len(rows) - 1):
        if i % rebal == 0:
            w = wfn(rows[i])
            if vol_scale and len(hist) >= vol_scale[1]:
                tgt, lb = vol_scale
                rv = statistics.stdev(hist[-lb:]) * math.sqrt(TD)
                if rv > 1e-6:
                    w = {c: v * min(1.0, tgt / rv) for c, v in w.items()}
            cost = sum(abs(w.get(c, 0) - w_prev.get(c, 0))
                       for c in set(w) | set(w_prev)) * COST_BPS / 10000
            w_prev = w
        else:
            w, cost = w_prev, 0.0
        er = excess(rows, i)
        r = sum(w.get(c, 0) * er.get(c, 0) for c in w if c != 'JPY') - cost
        rets.append(r)
        hist.append(r)
    if len(rets) < 2:
        return None
    sd, mean = statistics.stdev(rets), statistics.mean(rets)
    peak = cum = mdd = 0.0
    for x in rets:
        cum += x
        peak = max(peak, cum)
        mdd = max(mdd, peak - cum)
    worst = min(sum(rets[i:i + 60]) for i in range(len(rets) - 60)) if len(rets) > 60 else 0
    ann = math.exp(mean * TD) - 1
    return {'label': label, 'sharpe': mean / sd * math.sqrt(TD) if sd else 0,
            'ann': ann, 'mdd': mdd, 'worst60': worst,
            't': mean / (sd / math.sqrt(len(rets))) if sd else 0,
            'calmar': ann / mdd if mdd else 0}


def make_div(n_long, n_short, universe=None):
    """実装(modules/carry_portfolio.target_weights mode='diversified')と同じ論理。"""
    def f(rec):
        pool = [c for c in rec['r'] if universe is None or c in universe]
        ranked = sorted(pool, key=lambda c: -rec['r'][c])
        if len(ranked) < max(n_long, n_short) + 1:
            return {}
        w = {}
        for c in ranked[:n_long]:
            w[c] = w.get(c, 0) + 1.0 / n_long
        for c in ranked[-n_short:]:
            w[c] = w.get(c, 0) - 1.0 / n_short
        return w
    return f


def make_long_only(n, universe=None):
    """実装 mode='long_only' と同じ論理。上位nをロング、対価は全額JPY。"""
    def f(rec):
        pool = [c for c in rec['r'] if c != 'JPY' and (universe is None or c in universe)]
        ranked = sorted(pool, key=lambda c: -rec['r'][c])[:n]
        if not ranked:
            return {}
        w = {c: 1.0 / n for c in ranked}
        w['JPY'] = -sum(w.values())
        return w
    return f


def fmt(r):
    return (f"{r['label']:38s} Sharpe={r['sharpe']:+5.2f} t={r['t']:+5.2f} "
            f"年率={r['ann']*100:+6.2f}% 最大DD={r['mdd']*100:5.1f}% "
            f"Calmar={r['calmar']:+5.2f} 最悪60日={r['worst60']*100:+6.1f}%")


def main():
    rows = to_jpy_frame(build())
    split = int(len(rows) * 0.6)
    print(f'JPY建て・USD含む。通貨: {sorted(rows[-1]["px"])}')
    print(f'train {rows[0]["date"]}〜{rows[split-1]["date"]} / '
          f'test {rows[split]["date"]}〜{rows[-1]["date"]}\n')

    no_usd = {'AUD', 'CAD', 'CHF', 'EUR', 'GBP', 'JPY', 'NOK', 'NZD', 'SEK'}
    variants = [
        ('【旧検証と同じ】3/3 USD除外', make_div(3, 3, no_usd), None),
        ('【実装と同じ】3/3 USD含む', make_div(3, 3), None),
        ('【実装と同じ】3/3 USD含む+ボラ8%', make_div(3, 3), (0.08, 60)),
        ('【実装と同じ】長期のみ 上位3 USD含む', make_long_only(3), None),
        ('【実装と同じ】長期のみ 上位3 +ボラ8%', make_long_only(3), (0.08, 60)),
        ('参考 2/2 USD含む', make_div(2, 2), None),
        ('参考 4/4 USD含む', make_div(4, 4), None),
    ]

    for name, data in [('train', rows[:split]), ('test（判定）', rows[split:]), ('全期間', rows)]:
        print('=' * 112)
        print(f'【{name}】')
        print('=' * 112)
        for label, fn, vs in variants:
            r = run(data, fn, label, vol_scale=vs)
            if r:
                print(fmt(r))
        print()


if __name__ == '__main__':
    main()
