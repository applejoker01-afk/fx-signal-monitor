"""
反実仮想テスト: SIGNAL_LOST で途中決済した60件を、設計どおり
「SL / TP到達後トレーリング」で最後まで走らせていたらどうなったか。

同一トレード集合に対する対応比較なので、
「TPに届いた取引だけを見る」ような選択バイアスが入らない。

price_series.json は本番スキャナが毎時コミットしたスナップショットの復元で、
本番の決済判定も毎時なので、production挙動をほぼ忠実に再現できる。
"""
import json
import math
import os
import statistics
from datetime import datetime, timedelta

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SERIES = json.load(open(os.path.join(REPO, 'research/results/price_series.json'), encoding='utf-8'))
TRADES = [json.loads(l) for l in open(os.path.join(REPO, 'data/closed_trades.jsonl'), encoding='utf-8') if l.strip()]


def parse(ts):
    return datetime.fromisoformat(ts.replace('Z', '+00:00'))


PRICES = {p: [(parse(ts), px) for ts, px in pts] for p, pts in SERIES.items()}


def simulate(t, horizon_days=None, use_trailing=True):
    """1トレードを設計どおり走らせる。戻り値: (R, 決済理由, 保有時間h)"""
    pair = t['pair']
    pts = PRICES.get(pair)
    if not pts:
        return None
    entry = t['entry_price']
    sl0 = t.get('initial_sl') or t.get('sl')
    tp = t.get('tp') or t.get('tp1')
    if entry is None or sl0 is None or tp is None:
        return None
    width = abs(entry - sl0)
    if width <= 0:
        return None
    is_long = str(t.get('direction', '')).endswith('LONG')
    t0 = parse(t['entry_time'])
    deadline = t0 + timedelta(days=horizon_days) if horizon_days else None
    be_after_tp = t.get('be_target_after_tp')
    trail_dist = t.get('trail_distance')

    sl = sl0
    tp_hit = False
    extreme = entry

    for ts, px in pts:
        if ts <= t0:
            continue
        if deadline and ts > deadline:
            r = ((px - entry) if is_long else (entry - px)) / width
            return (r, 'TIME_STOP', (ts - t0).total_seconds() / 3600)

        if is_long:
            extreme = max(extreme, px)
            if px <= sl:
                r = (sl - entry) / width
                return (r, 'TRAIL_HIT' if tp_hit else 'SL_HIT', (ts - t0).total_seconds() / 3600)
            if not tp_hit and px >= tp:
                tp_hit = True
                if use_trailing and be_after_tp is not None:
                    sl = max(sl, be_after_tp)
                else:
                    return ((tp - entry) / width, 'TP_HIT', (ts - t0).total_seconds() / 3600)
            if tp_hit and use_trailing and trail_dist:
                sl = max(sl, extreme - trail_dist)
        else:
            extreme = min(extreme, px)
            if px >= sl:
                r = (entry - sl) / width
                return (r, 'TRAIL_HIT' if tp_hit else 'SL_HIT', (ts - t0).total_seconds() / 3600)
            if not tp_hit and px <= tp:
                tp_hit = True
                if use_trailing and be_after_tp is not None:
                    sl = min(sl, be_after_tp)
                else:
                    return ((entry - tp) / width, 'TP_HIT', (ts - t0).total_seconds() / 3600)
            if tp_hit and use_trailing and trail_dist:
                sl = min(sl, extreme + trail_dist)

    last_ts, last_px = pts[-1]
    if last_ts <= t0:
        return None
    r = ((last_px - entry) if is_long else (entry - last_px)) / width
    return (r, 'DATA_END', (last_ts - t0).total_seconds() / 3600)


def actual_r(t):
    width = abs(t['entry_price'] - (t.get('initial_sl') or t.get('sl')))
    return t['pips'] / width if width else None


def stats(rs, label):
    n = len(rs)
    if not n:
        return f'{label}: n=0'
    wins = [r for r in rs if r > 0]
    gl = abs(sum(r for r in rs if r < 0))
    gw = sum(wins)
    pf = gw / gl if gl else float('inf')
    mean = statistics.mean(rs)
    sd = statistics.stdev(rs) if n > 1 else 0
    t_stat = mean / (sd / math.sqrt(n)) if sd else 0
    return (f'{label}: n={n:3d} 勝率={len(wins)/n*100:5.1f}% PF={pf:5.2f} '
            f'平均R={mean:+.3f} 合計R={sum(rs):+.2f} t={t_stat:+.2f}')


lost = [t for t in TRADES if t.get('exit_reason') == 'SIGNAL_LOST']
print('=' * 78)
print(f'反実仮想: SIGNAL_LOST {len(lost)}件を設計どおり走らせた場合')
print('=' * 78)

for horizon in [None, 30, 14, 7]:
    pairs_r = []
    reasons = {}
    for t in lost:
        a = actual_r(t)
        s = simulate(t, horizon_days=horizon, use_trailing=True)
        if a is None or s is None:
            continue
        pairs_r.append((a, s[0]))
        reasons[s[1]] = reasons.get(s[1], 0) + 1
    if not pairs_r:
        continue
    act = [a for a, _ in pairs_r]
    cf = [c for _, c in pairs_r]
    diffs = [c - a for a, c in pairs_r]
    n = len(diffs)
    md = statistics.mean(diffs)
    sd = statistics.stdev(diffs) if n > 1 else 0
    t_stat = md / (sd / math.sqrt(n)) if sd else 0
    label = f'{horizon}日上限' if horizon else '上限なし'
    print(f'\n--- {label} ---')
    print(stats(act, '  実際(SIGNAL_LOST)'))
    print(stats(cf, '  反実仮想(設計通り)'))
    print(f'  差分: 平均{md:+.3f}R/件  合計{sum(diffs):+.2f}R  '
          f'対応t検定 t={t_stat:+.2f}  改善した取引={sum(1 for d in diffs if d > 0)}/{n}')
    print(f'  反実仮想の決済内訳: {reasons}')

print()
print('=' * 78)
print('全81件を設計どおり走らせた場合（SIGNAL_LOST廃止のシステム全体影響）')
print('=' * 78)
all_pairs = []
for t in TRADES:
    a = actual_r(t)
    s = simulate(t, horizon_days=30, use_trailing=True)
    if a is None or s is None:
        continue
    all_pairs.append((a, s[0]))
act = [a for a, _ in all_pairs]
cf = [c for _, c in all_pairs]
diffs = [c - a for a, c in all_pairs]
n = len(diffs)
md = statistics.mean(diffs)
sd = statistics.stdev(diffs) if n > 1 else 0
print(stats(act, '  実際       '))
print(stats(cf, '  反実仮想   '))
print(f'  差分: 平均{md:+.3f}R/件 合計{sum(diffs):+.2f}R '
      f't={md/(sd/math.sqrt(n)) if sd else 0:+.2f} 改善={sum(1 for d in diffs if d>0)}/{n}')
