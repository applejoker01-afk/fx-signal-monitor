"""
横断（クロスセクショナル）キャリーの検証。

学術的に文書化されている為替アノマリーは、単一ペアの方向当てではなく
「高金利通貨ロング／低金利通貨ショート」のポートフォリオ（Lustig-Verdelhan等）。
現行システムが実装しているのは単一ペアの方向予測であり、これとは別物。

定義:
  各通貨 i を USD 調達で保有したときの超過リターン
    ret_i = Δlog(USD建て価格_i) + (r_i - r_usd)/252
  （USD自身は ret=0）
  毎月末に金利で順位付けし、上位N本ロング・下位N本ショートの等ウェイト。

規律（フェーズ1・2と同じ）:
  全変種を列挙、コスト込み、ドリフト比較、train/test分割、多重検定を明記。
"""
import csv
import math
import os
import statistics
from bisect import bisect_right
from datetime import date

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CACHE = os.path.join(REPO, 'data', 'fred_cache')
TD = 252
COST_BPS = 2.0  # 片道2bp（G10スポットの現実的な水準）

CCY = {
    'JPY': ('fx_jpy.csv', 'PER_USD', 'r_jpy.csv'),
    'EUR': ('fx_eur.csv', 'USD_PER', 'r_eur.csv'),
    'GBP': ('fx_gbp.csv', 'USD_PER', 'r_gbp.csv'),
    'AUD': ('fx_aud.csv', 'USD_PER', 'r_aud.csv'),
    'NZD': ('fx_nzd.csv', 'USD_PER', 'r_nzd.csv'),
    'CAD': ('fx_cad.csv', 'PER_USD', 'r_cad.csv'),
    'CHF': ('fx_chf.csv', 'PER_USD', 'r_chf.csv'),
    'NOK': ('fx_nok.csv', 'PER_USD', 'r_nok.csv'),
    'SEK': ('fx_sek.csv', 'PER_USD', 'r_sek.csv'),
}


def read_fred(fn):
    path = os.path.join(CACHE, fn)
    if not os.path.exists(path):
        return None
    rows = []
    with open(path, encoding='utf-8') as f:
        rd = csv.reader(f)
        header = next(rd, None)
        for r in rd:
            if len(r) < 2:
                continue
            v = r[1].strip()
            if not v or v == '.':
                continue
            try:
                rows.append((date.fromisoformat(r[0].strip()), float(v)))
            except Exception:
                continue
    rows.sort()
    return rows


class Step:
    def __init__(self, rows):
        self.d = [a for a, _ in rows]
        self.v = [b for _, b in rows]

    def at(self, d):
        i = bisect_right(self.d, d) - 1
        return self.v[i] if i >= 0 else None


def build():
    usd_rate = read_fred('r_usd.csv')
    if not usd_rate:
        raise SystemExit('r_usd.csv が無い。fetch_g10_history.py を先に実行')
    r_usd = Step(usd_rate)

    px, rates = {}, {}
    for c, (fxf, conv, rf) in CCY.items():
        fx = read_fred(fxf)
        rr = read_fred(rf)
        if not fx or not rr:
            print(f'  [skip] {c}: データ欠落')
            continue
        px[c] = {d: (1.0 / v if conv == 'PER_USD' else v) for d, v in fx if v > 0}
        rates[c] = Step(rr)

    all_dates = sorted(set().union(*[set(v) for v in px.values()]))
    print(f'通貨: {sorted(px)}  期間: {all_dates[0]} 〜 {all_dates[-1]}  営業日={len(all_dates)}')

    rows = []
    for d in all_dates:
        rec = {'date': d, 'r_usd': r_usd.at(d), 'px': {}, 'r': {}}
        if rec['r_usd'] is None:
            continue
        for c in px:
            p = px[c].get(d)
            rr = rates[c].at(d)
            if p is not None and rr is not None:
                rec['px'][c] = p
                rec['r'][c] = rr
        if len(rec['px']) >= 6:
            rows.append(rec)
    return rows


def excess_returns(rows, i):
    """i日目→i+1日目の、各通貨のUSD調達超過リターン。"""
    out = {}
    a, b = rows[i], rows[i + 1]
    dt_days = max(1, (b['date'] - a['date']).days)
    for c in a['px']:
        if c not in b['px']:
            continue
        fx = math.log(b['px'][c] / a['px'][c])
        carry = (a['r'][c] - a['r_usd']) / 100.0 * dt_days / 365.0
        out[c] = fx + carry
    return out


def run(rows, weight_fn, label, rebalance_days=21):
    rets, w_prev, turn = [], {}, 0
    for i in range(len(rows) - 1):
        if i % rebalance_days == 0:
            w = weight_fn(rows[i])
            turn += sum(abs(w.get(c, 0) - w_prev.get(c, 0)) for c in set(w) | set(w_prev))
            cost = sum(abs(w.get(c, 0) - w_prev.get(c, 0)) for c in set(w) | set(w_prev)) * COST_BPS / 10000
            w_prev = w
        else:
            w = w_prev
            cost = 0.0
        er = excess_returns(rows, i)
        r = sum(w.get(c, 0) * er.get(c, 0) for c in w) - cost
        rets.append(r)
    if len(rets) < 2:
        return None
    sd = statistics.stdev(rets)
    mean = statistics.mean(rets)
    sh = mean / sd * math.sqrt(TD) if sd else 0
    peak = cum = mdd = 0.0
    for r in rets:
        cum += r
        peak = max(peak, cum)
        mdd = max(mdd, peak - cum)
    return {'label': label, 'sharpe': sh, 'ann': math.exp(mean * TD) - 1,
            'mdd': mdd, 't': mean / (sd / math.sqrt(len(rets))) if sd else 0,
            'turn': turn, 'n': len(rets)}


def fmt(r):
    return (f"{r['label']:30s} Sharpe={r['sharpe']:+5.2f} t={r['t']:+5.2f} "
            f"年率={r['ann']*100:+6.2f}% 最大DD={r['mdd']*100:5.1f}%")


def make_carry(n_long, n_short):
    def f(rec):
        ranked = sorted(rec['r'], key=lambda c: -rec['r'][c])
        w = {}
        for c in ranked[:n_long]:
            w[c] = 1.0 / n_long
        for c in ranked[-n_short:]:
            w[c] = w.get(c, 0) - 1.0 / n_short
        return w
    return f


def equal_long(rec):
    cs = list(rec['px'])
    return {c: 1.0 / len(cs) for c in cs}


def main():
    rows = build()
    split = int(len(rows) * 0.6)
    print(f'train: {rows[0]["date"]}〜{rows[split-1]["date"]}  '
          f'test: {rows[split]["date"]}〜{rows[-1]["date"]}\n')

    variants = {
        'ドリフト基準: 全通貨等ウェイトLong': equal_long,
        'キャリー 上位3/下位3': make_carry(3, 3),
        'キャリー 上位2/下位2': make_carry(2, 2),
        'キャリー 上位1/下位1': make_carry(1, 1),
        'キャリー 上位4/下位4': make_carry(4, 4),
    }

    for name, sub, s, e in [('train', rows[:split], 0, split), ('test', rows[split:], split, len(rows))]:
        print('=' * 88)
        print(f'【{name}】')
        print('=' * 88)
        for label, fn in variants.items():
            r = run(sub, fn, label)
            if r:
                print(fmt(r))
        print()

    print('=' * 88)
    print('【全期間・参考】')
    print('=' * 88)
    for label, fn in variants.items():
        r = run(rows, fn, label)
        if r:
            print(fmt(r))
    print(f'\n注: {len(variants)}変種。コスト片道{COST_BPS}bp、月次リバランス。')


if __name__ == '__main__':
    main()
