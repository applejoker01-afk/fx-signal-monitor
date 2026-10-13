"""
再設計フェーズ1: エッジ源の候補を22年USDJPY日足で総当たり検証する。

過去の失敗（ノイズへのフィッティング）を繰り返さないための強制ルール:
  1. 変種を事前に全列挙し、結果を全部出力する（良かったものだけ報告しない）
  2. ドリフト基準（買い持ち）と必ず比較する — 円安トレンドに乗っただけでないか
  3. 取引コストを必ず含める
  4. train(前半) / test(後半) に分割し、testは一度しか見ない
  5. スワップ（金利差収益）を損益に含める — キャリーの収益源そのもの

候補は「データを漁って見つけたパターン」ではなく、
学術的に文書化された為替のリターン源だけに限定する:
  - TSMOM: 時系列モメンタム（Moskowitz-Ooi-Pedersen 2012）
  - CARRY: 金利差水準（キャリートレード）
  - DCARRY: 金利差の「変化」（金融政策の方向性）— 現行FAの水準固定問題への対案
  - 組み合わせ
"""
import csv
import json
import math
import os
import statistics
from bisect import bisect_right
from datetime import date

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TRADING_DAYS = 252
SPREAD_PRICE = 0.002  # USDJPY 0.2pip = 0.002円（本番SPREAD_PIPSと同値）


def load_prices():
    rows = []
    with open(os.path.join(REPO, 'data/usdjpy_daily.csv'), encoding='utf-8') as f:
        for r in csv.DictReader(f):
            try:
                d = date.fromisoformat(r['date'])
                p = float(r['price'])
            except Exception:
                continue
            rows.append((d, p))
    rows.sort()
    return rows


def load_fred(path, col):
    rows = []
    with open(os.path.join(REPO, path), encoding='utf-8') as f:
        for r in csv.DictReader(f):
            v = r.get(col, '').strip()
            if not v or v == '.':
                continue
            try:
                rows.append((date.fromisoformat(r['observation_date']), float(v)))
            except Exception:
                continue
    rows.sort()
    return rows


class StepSeries:
    """日次に前方補完して参照する時系列（金利のように階段状に変わるもの用）。"""

    def __init__(self, rows):
        self.dates = [d for d, _ in rows]
        self.vals = [v for _, v in rows]

    def at(self, d):
        i = bisect_right(self.dates, d) - 1
        return self.vals[i] if i >= 0 else None


def build():
    prices = load_prices()
    usd = StepSeries(load_fred('data/fred_cache/usd_dff.csv', 'DFF'))
    jpy = StepSeries(load_fred('data/fred_cache/jpy_ir3tib.csv', 'IR3TIB01JPM156N'))
    out = []
    for d, p in prices:
        ru, rj = usd.at(d), jpy.at(d)
        if ru is None or rj is None:
            continue
        out.append({'date': d, 'px': p, 'diff': ru - rj})
    return out


def sharpe(rets):
    if len(rets) < 2:
        return 0.0
    sd = statistics.stdev(rets)
    if sd == 0:
        return 0.0
    return statistics.mean(rets) / sd * math.sqrt(TRADING_DAYS)


def tstat(rets):
    if len(rets) < 2:
        return 0.0
    sd = statistics.stdev(rets)
    if sd == 0:
        return 0.0
    return statistics.mean(rets) / (sd / math.sqrt(len(rets)))


def run(data, positions, label, include_carry=True):
    """positions[i] = i日目の終値時点で取るポジション(-1/0/+1)。翌日リターンに適用。"""
    rets = []
    prev_pos = 0.0
    for i in range(len(data) - 1):
        pos = positions[i]
        px0, px1 = data[i]['px'], data[i + 1]['px']
        if px0 <= 0:
            continue
        fx = math.log(px1 / px0)
        carry = (data[i]['diff'] / 100.0 / TRADING_DAYS) if include_carry else 0.0
        r = pos * (fx + carry)
        if pos != prev_pos:
            r -= abs(pos - prev_pos) * (SPREAD_PRICE / px0) / 2.0
        prev_pos = pos
        rets.append(r)
    total = math.exp(sum(rets)) - 1
    peak = cum = mdd = 0.0
    for r in rets:
        cum += r
        peak = max(peak, cum)
        mdd = max(mdd, peak - cum)
    turnover = sum(1 for i in range(1, len(positions)) if positions[i] != positions[i - 1])
    return {
        'label': label, 'n': len(rets), 'sharpe': sharpe(rets), 't': tstat(rets),
        'total': total, 'mdd': mdd, 'turnover': turnover,
        'ann': math.exp(statistics.mean(rets) * TRADING_DAYS) - 1 if rets else 0,
    }


def fmt(r):
    return (f"{r['label']:34s} Sharpe={r['sharpe']:+5.2f} t={r['t']:+5.2f} "
            f"年率={r['ann']*100:+6.1f}% 累積={r['total']*100:+8.1f}% "
            f"最大DD={r['mdd']*100:5.1f}% 売買回数={r['turnover']:4d}")


def main():
    data = build()
    print(f'データ: {data[0]["date"]} 〜 {data[-1]["date"]}  n={len(data)}営業日')
    print(f'金利差(USD-JPY): 最小{min(d["diff"] for d in data):+.2f}% '
          f'最大{max(d["diff"] for d in data):+.2f}% '
          f'負の期間={sum(1 for d in data if d["diff"] < 0)}日')

    split = int(len(data) * 0.6)
    print(f'train: {data[0]["date"]}〜{data[split-1]["date"]}  '
          f'test: {data[split]["date"]}〜{data[-1]["date"]}')

    # ---- 候補を事前に全列挙 ----
    variants = {}
    variants['BASELINE 買い持ち(ドリフト基準)'] = [1] * len(data)
    variants['BASELINE 常にショート'] = [-1] * len(data)

    for L in (21, 63, 126, 252):
        pos = []
        for i in range(len(data)):
            if i < L:
                pos.append(0)
            else:
                pos.append(1 if data[i]['px'] > data[i - L]['px'] else -1)
        variants[f'TSMOM {L}日'] = pos

    variants['CARRY 金利差の符号'] = [1 if d['diff'] > 0 else -1 for d in data]

    for L in (63, 126, 252):
        pos = []
        for i in range(len(data)):
            if i < L:
                pos.append(0)
            else:
                dd = data[i]['diff'] - data[i - L]['diff']
                pos.append(1 if dd > 0 else (-1 if dd < 0 else 0))
        variants[f'DCARRY 金利差変化 {L}日'] = pos

    for L in (63, 252):
        mom = [0] * len(data)
        for i in range(len(data)):
            if i >= L:
                mom[i] = 1 if data[i]['px'] > data[i - L]['px'] else -1
        car = [1 if d['diff'] > 0 else -1 for d in data]
        variants[f'TSMOM{L}×CARRY 一致時のみ'] = [
            m if m == c else 0 for m, c in zip(mom, car)]

    print('\n' + '=' * 100)
    print('【全期間】※ここは探索用。判断はtestで行う')
    print('=' * 100)
    for label, pos in variants.items():
        print(fmt(run(data, pos, label)))

    print('\n' + '=' * 100)
    print('【train（前半60%）】')
    print('=' * 100)
    tr = data[:split]
    tr_res = [run(tr, pos[:split], label) for label, pos in variants.items()]
    for r in tr_res:
        print(fmt(r))

    print('\n' + '=' * 100)
    print('【test（後半40%・一度だけ見る）】')
    print('=' * 100)
    te = data[split:]
    te_res = [run(te, pos[split:], label) for label, pos in variants.items()]
    for r in te_res:
        print(fmt(r))

    print('\n' + '=' * 100)
    print('【train→test の一貫性】※trainで良くtestで崩れる=フィッティング')
    print('=' * 100)
    base_te = next(r for r in te_res if r['label'].startswith('BASELINE 買い持ち'))
    for a, b in zip(tr_res, te_res):
        excess = b['sharpe'] - base_te['sharpe']
        mark = '  ← train/testで符号反転' if a['sharpe'] * b['sharpe'] < 0 else ''
        print(f"{a['label']:34s} train Sharpe={a['sharpe']:+5.2f} → "
              f"test Sharpe={b['sharpe']:+5.2f}  買い持ち超過={excess:+5.2f}{mark}")

    print('\n注: 変種を{}個テストしているため、多重検定の補正が必要。'.format(len(variants)))
    print('   有意水準5%なら、偶然に1つ以上が「有意」に見える確率は '
          f'{(1 - 0.95 ** len(variants)) * 100:.0f}%。')


if __name__ == '__main__':
    main()
