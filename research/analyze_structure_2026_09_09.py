"""
2026-09-09 構造診断: 実運用81トレードの再集計。

過去の集計はペア横断で raw pips を合計していたが、GBPJPY の 1pip と
KRWJPY の 1pip は経済的価値が全く違うため比較不能。ここでは
  R = 実現pips / 初期SL幅pips
というリスク正規化した単位で評価し直す。R は「1トレードで取ったリスクの何倍
取れたか」なので、ペア・ボラティリティ・ロットに依存せず横断合算できる。
"""
import json
import math
import statistics
from collections import defaultdict

TRADES = []
with open('data/closed_trades.jsonl', encoding='utf-8') as f:
    for line in f:
        line = line.strip()
        if line:
            TRADES.append(json.loads(line))


def sl_width(t):
    """初期SL幅（価格単位）。initial_sl があればそれを優先。"""
    entry = t.get('entry_price')
    sl = t.get('initial_sl') or t.get('sl')
    if entry is None or sl is None:
        return None
    w = abs(entry - sl)
    return w if w > 0 else None


def r_multiple(t):
    w = sl_width(t)
    if w is None:
        return None
    pips = t.get('pips')
    if pips is None:
        return None
    return pips / w


def summarize(name, rows):
    rs = [r_multiple(t) for t in rows]
    rs = [r for r in rs if r is not None]
    if not rs:
        return f"{name}: n=0"
    n = len(rs)
    wins = [r for r in rs if r > 0]
    losses = [r for r in rs if r < 0]
    gross_win = sum(wins)
    gross_loss = abs(sum(losses))
    pf = (gross_win / gross_loss) if gross_loss > 0 else float('inf')
    mean = statistics.mean(rs)
    sd = statistics.stdev(rs) if n > 1 else 0.0
    # 期待値がゼロと区別できるか（1標本t検定）
    t_stat = (mean / (sd / math.sqrt(n))) if sd > 0 and n > 1 else 0.0
    return (f"{name}: n={n:3d} 勝率={len(wins)/n*100:5.1f}% "
            f"PF={pf:5.2f} 平均R={mean:+.3f} SD={sd:.3f} t={t_stat:+.2f}")


print("=" * 78)
print("【1】全体")
print("=" * 78)
print(summarize("ALL", TRADES))

# 累積Rとドローダウン
rs_seq = [(t.get('exit_time', ''), r_multiple(t)) for t in TRADES]
rs_seq = [(d, r) for d, r in rs_seq if r is not None]
rs_seq.sort()
cum = 0.0
peak = 0.0
maxdd = 0.0
curve = []
for d, r in rs_seq:
    cum += r
    peak = max(peak, cum)
    maxdd = max(maxdd, peak - cum)
    curve.append(cum)
print(f"累積R={cum:+.2f}  最大DD={maxdd:.2f}R  最終/最大DD比={cum/maxdd if maxdd else float('nan'):+.2f}")

print()
print("=" * 78)
print("【2】方向別（構造的LONGバイアスの確認）")
print("=" * 78)
by_dir = defaultdict(list)
for t in TRADES:
    by_dir[t.get('direction', '?')].append(t)
for k in sorted(by_dir):
    print(summarize(f"{k:12s}", by_dir[k]))

print()
print("=" * 78)
print("【3】★別（スコアが予測力を持つか＝単調性の検証）")
print("=" * 78)
by_star = defaultdict(list)
for t in TRADES:
    by_star[t.get('initial_stars', '?')].append(t)
for k in sorted(by_star, key=lambda x: str(x)):
    print(summarize(f"★{k}", by_star[k]))

print()
print("=" * 78)
print("【4】決済理由別（出口設計の妥当性）")
print("=" * 78)
by_exit = defaultdict(list)
for t in TRADES:
    by_exit[t.get('exit_reason', '?')].append(t)
for k in sorted(by_exit, key=lambda x: -len(by_exit[x])):
    print(summarize(f"{k:14s}", by_exit[k]))

print()
print("=" * 78)
print("【5】ta_score 帯別（逆U字仮説の再検証）")
print("=" * 78)
scored = [t for t in TRADES if t.get('ta_score') is not None and r_multiple(t) is not None]
scored.sort(key=lambda t: t['ta_score'])
q = max(1, len(scored) // 4)
for i, label in enumerate(['Q1(低)', 'Q2', 'Q3', 'Q4(高)']):
    chunk = scored[i * q:(i + 1) * q] if i < 3 else scored[3 * q:]
    if chunk:
        lo = chunk[0]['ta_score']
        hi = chunk[-1]['ta_score']
        print(summarize(f"{label} ta={lo:.0f}-{hi:.0f}", chunk))

print()
print("=" * 78)
print("【6】ペア別（n>=3のみ）")
print("=" * 78)
by_pair = defaultdict(list)
for t in TRADES:
    by_pair[t.get('pair', '?')].append(t)
rows = [(p, ts) for p, ts in by_pair.items() if len(ts) >= 3]
rows.sort(key=lambda x: -len(x[1]))
for p, ts in rows:
    print(summarize(f"{p:8s}", ts))
print(f"\n（n<3のペア: {sum(1 for p, ts in by_pair.items() if len(ts) < 3)}ペア）")

print()
print("=" * 78)
print("【7】保有期間")
print("=" * 78)
holds = [t.get('hold_hours') for t in TRADES if t.get('hold_hours') is not None]
if holds:
    print(f"中央値={statistics.median(holds):.1f}h 平均={statistics.mean(holds):.1f}h "
          f"最短={min(holds):.1f}h 最長={max(holds):.1f}h")
    short_hold = [t for t in TRADES if (t.get('hold_hours') or 0) < 24]
    print(summarize("24h未満で決着", short_hold))
    long_hold = [t for t in TRADES if (t.get('hold_hours') or 0) >= 24]
    print(summarize("24h以上保有  ", long_hold))

print()
print("=" * 78)
print("【8】実現損益(JPY)ベース ※position_sizingが記録されている分のみ")
print("=" * 78)
pnl = [t.get('pnl_jpy') for t in TRADES if t.get('pnl_jpy') is not None]
if pnl:
    print(f"n={len(pnl)} 合計={sum(pnl):+,.0f}円 平均={statistics.mean(pnl):+,.0f}円 "
          f"最大益={max(pnl):+,.0f} 最大損={min(pnl):+,.0f}")
