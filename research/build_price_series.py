"""
git履歴の docs/last_signals.json から全ペアの価格時系列を復元する。

本番スキャナが毎時コミットしているスナップショットなので、
「本番システムが実際に観測した価格」そのものを再現できる。
外部APIに依存せず、かつ当時のデータ改訂の影響も受けない。

出力: research/results/price_series.json  {pair: [[iso_ts, price], ...]}
"""
import json
import os
import subprocess
from collections import defaultdict

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(REPO, 'research', 'results', 'price_series.json')


def git(*args):
    return subprocess.run(['git'] + list(args), cwd=REPO, capture_output=True,
                          encoding='utf-8', errors='replace')


def main():
    log = git('log', '--format=%H', '--', 'docs/last_signals.json').stdout.split()
    print(f'commits touching last_signals.json: {len(log)}')

    series = defaultdict(dict)  # pair -> {ts: price}
    ok = 0
    for i, sha in enumerate(log):
        raw = git('show', f'{sha}:docs/last_signals.json').stdout
        if not raw:
            continue
        try:
            d = json.loads(raw)
        except Exception:
            continue
        ts = d.get('timestamp')
        if not ts:
            continue
        for r in d.get('results', []) or []:
            p = r.get('pair')
            px = r.get('price')
            if p and px:
                series[p][ts] = px
        ok += 1
        if (i + 1) % 100 == 0:
            print(f'  parsed {i+1}/{len(log)}')

    out = {p: sorted(m.items()) for p, m in series.items()}
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, 'w', encoding='utf-8') as f:
        json.dump(out, f)

    print(f'snapshots parsed: {ok}')
    print(f'pairs: {len(out)}')
    for p in sorted(out)[:5]:
        pts = out[p]
        print(f'  {p}: n={len(pts)} {pts[0][0][:10]} -> {pts[-1][0][:10]}')


if __name__ == '__main__':
    main()
