"""
再設計フェーズ2: エントリー予測ではなく「エクスポージャー管理」に edge があるか。

フェーズ1の結論: 22年USDJPYで、標準的なシグナル（TSMOM/CARRY/DCARRY）は
どれも買い持ちを超えられない。つまり「いつ入るか」には edge がない。

そこで問いを変える。キャリー（＝JPYクロス買い持ち）の文書化された弱点は
「平時にコツコツ、暴落で一撃」という歪んだリターン分布（2008年、2024年8月）。
ならば edge は方向予測ではなく、**暴落局面でのエクスポージャー削減**にあるかもしれない。

これはエントリー予測とは別の問いで、かつリスク管理としての経済的正当性がある
（ボラティリティのクラスタリングは為替で最も頑健に観測される性質の一つ）。

フェーズ1と同じ規律: 全変種を列挙、コスト込み、train/test分割、買い持ちとの比較。
"""
import csv
import math
import os
import statistics
from bisect import bisect_right
from datetime import date

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TRADING_DAYS = 252
SPREAD_PRICE = 0.002


def load_prices():
    rows = []
    with open(os.path.join(REPO, 'data/usdjpy_daily.csv'), encoding='utf-8') as f:
        for r in csv.DictReader(f):
            try:
                rows.append((date.fromisoformat(r['date']), float(r['price'])))
            except Exception:
                continue
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


class Step:
    def __init__(self, rows):
        self.d = [x for x, _ in rows]
        self.v = [y for _, y in rows]

    def at(self, d):
        i = bisect_right(self.d, d) - 1
        return self.v[i] if i >= 0 else None


def build():
    px = load_prices()
    usd = Step(load_fred('data/fred_cache/usd_dff.csv', 'DFF'))
    jpy = Step(load_fred('data/fred_cache/jpy_ir3tib.csv', 'IR3TIB01JPM156N'))
    out = []
    for d, p in px:
        ru, rj = usd.at(d), jpy.at(d)
        if ru is None or rj is None:
            continue
        out.append({'date': d, 'px': p, 'diff': ru - rj})
    # 日次対数リターンと実現ボラ
    for i, row in enumerate(out):
        row['ret'] = math.log(out[i]['px'] / out[i - 1]['px']) if i else 0.0
    for i, row in enumerate(out):
        w = [out[j]['ret'] for j in range(max(1, i - 19), i + 1)]
        row['vol20'] = statistics.stdev(w) * math.sqrt(TRADING_DAYS) if len(w) > 1 else 0.0
        w60 = [out[j]['ret'] for j in range(max(1, i - 59), i + 1)]
        row['vol60'] = statistics.stdev(w60) * math.sqrt(TRADING_DAYS) if len(w60) > 1 else 0.0
        row['ma200'] = statistics.mean([out[j]['px'] for j in range(max(0, i - 199), i + 1)])
    return out


def evaluate(data, positions, label):
    rets, prev = [], 0.0
    for i in range(len(data) - 1):
        pos = positions[i]
        px0 = data[i]['px']
        r = pos * (data[i + 1]['ret'] + data[i]['diff'] / 100.0 / TRADING_DAYS)
        if pos != prev:
            r -= abs(pos - prev) * (SPREAD_PRICE / px0) / 2.0
        prev = pos
        rets.append(r)
    if len(rets) < 2:
        return None
    sd = statistics.stdev(rets)
    mean = statistics.mean(rets)
    sh = mean / sd * math.sqrt(TRADING_DAYS) if sd else 0
    peak = cum = mdd = 0.0
    for r in rets:
        cum += r
        peak = max(peak, cum)
        mdd = max(mdd, peak - cum)
    ann = math.exp(mean * TRADING_DAYS) - 1
    # 最悪20日
    worst = min(sum(rets[i:i + 20]) for i in range(len(rets) - 20)) if len(rets) > 20 else 0
    expo = statistics.mean([abs(p) for p in positions])
    turns = sum(1 for i in range(1, len(positions)) if positions[i] != positions[i - 1])
    return {'label': label, 'sharpe': sh, 'ann': ann, 'mdd': mdd,
            'calmar': ann / mdd if mdd > 0 else 0, 'worst20': worst,
            'expo': expo, 'turns': turns,
            't': mean / (sd / math.sqrt(len(rets))) if sd else 0}


def fmt(r):
    return (f"{r['label']:32s} Sharpe={r['sharpe']:+5.2f} 年率={r['ann']*100:+6.1f}% "
            f"最大DD={r['mdd']*100:5.1f}% Calmar={r['calmar']:+5.2f} "
            f"最悪20日={r['worst20']*100:+6.1f}% 平均建玉={r['expo']*100:3.0f}% 売買={r['turns']:3d}")


def main():
    data = build()
    n = len(data)
    split = int(n * 0.6)

    variants = {}
    variants['買い持ち（基準）'] = [1.0] * n

    # (1) トレンドフィルタ: 200日MA割れで降りる
    variants['MA200割れで撤退'] = [1.0 if data[i]['px'] > data[i]['ma200'] else 0.0 for i in range(n)]

    # (2) ボラフィルタ: 高ボラ局面で降りる（閾値は中央値でなく固定値=事前指定）
    for th in (0.10, 0.12, 0.15):
        variants[f'ボラ20日>{th*100:.0f}%で撤退'] = [
            1.0 if data[i]['vol20'] < th else 0.0 for i in range(n)]

    # (3) ボラターゲティング: 目標ボラ10%になるよう建玉調整（上限1倍）
    for target in (0.08, 0.10):
        pos = []
        for i in range(n):
            v = data[i]['vol60']
            pos.append(min(1.0, target / v) if v > 0.001 else 0.0)
        variants[f'ボラターゲット{target*100:.0f}%'] = pos

    # (4) 併用: MA200 かつ ボラ
    variants['MA200 かつ ボラ<12%'] = [
        1.0 if (data[i]['px'] > data[i]['ma200'] and data[i]['vol20'] < 0.12) else 0.0
        for i in range(n)]

    print('=' * 108)
    print(f'【train {data[0]["date"]}〜{data[split-1]["date"]}】')
    print('=' * 108)
    tr = [evaluate(data[:split], p[:split], l) for l, p in variants.items()]
    for r in tr:
        print(fmt(r))

    print()
    print('=' * 108)
    print(f'【test {data[split]["date"]}〜{data[-1]["date"]}】')
    print('=' * 108)
    te = [evaluate(data[split:], p[split:], l) for l, p in variants.items()]
    for r in te:
        print(fmt(r))

    print()
    print('=' * 108)
    print('【判定】買い持ちに対する改善（testで見る）')
    print('=' * 108)
    base = te[0]
    for a, b in zip(tr, te):
        if b['label'] == base['label']:
            continue
        ds = b['sharpe'] - base['sharpe']
        dd = base['mdd'] - b['mdd']
        dc = b['calmar'] - base['calmar']
        flip = '  ← train/testで方向が逆' if (a['sharpe'] - tr[0]['sharpe']) * ds < 0 else ''
        print(f"{b['label']:32s} ΔSharpe={ds:+5.2f} ΔmaxDD={dd*100:+5.1f}pt "
              f"ΔCalmar={dc:+5.2f}{flip}")

    print()
    print(f'注: {len(variants)}変種をテスト。多重検定考慮なしに「一番良かったもの」を採用しないこと。')


if __name__ == '__main__':
    main()
