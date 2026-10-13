"""
横断キャリー検証用に G10 の長期履歴を FRED から取得してローカルキャッシュする。

取得先は fred.stlouisfed.org のみ（公開データ・APIキー不要）。
本番の modules/rate_fetcher.py が既に使っている先と同じ。読み取りのみ。

- 為替: DEX系（対USD日次、1971年〜）
- 金利: IR3TIB01系（3ヶ月インターバンク、月次）
"""
import os
import time
import urllib.request

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CACHE = os.path.join(REPO, 'data', 'fred_cache')

# (系列ID, 保存名, 建て方) quote='USD_PER' は 1通貨=何USD、'PER_USD' は 1USD=何通貨
FX = [
    ('DEXJPUS', 'fx_jpy.csv', 'PER_USD'),
    ('DEXUSEU', 'fx_eur.csv', 'USD_PER'),
    ('DEXUSUK', 'fx_gbp.csv', 'USD_PER'),
    ('DEXUSAL', 'fx_aud.csv', 'USD_PER'),
    ('DEXUSNZ', 'fx_nzd.csv', 'USD_PER'),
    ('DEXCAUS', 'fx_cad.csv', 'PER_USD'),
    ('DEXSZUS', 'fx_chf.csv', 'PER_USD'),
    ('DEXNOUS', 'fx_nok.csv', 'PER_USD'),
    ('DEXSDUS', 'fx_sek.csv', 'PER_USD'),
]

RATES = [
    ('IR3TIB01USM156N', 'r_usd.csv'),
    ('IR3TIB01JPM156N', 'r_jpy.csv'),
    ('IR3TIB01EZM156N', 'r_eur.csv'),
    ('IR3TIB01GBM156N', 'r_gbp.csv'),
    ('IR3TIB01AUM156N', 'r_aud.csv'),
    ('IR3TIB01NZM156N', 'r_nzd.csv'),
    ('IR3TIB01CAM156N', 'r_cad.csv'),
    ('IR3TIB01CHM156N', 'r_chf.csv'),
    ('IR3TIB01NOM156N', 'r_nok.csv'),
    ('IR3TIB01SEM156N', 'r_sek.csv'),
]


def fetch(series_id, filename):
    path = os.path.join(CACHE, filename)
    if os.path.exists(path) and os.path.getsize(path) > 200:
        return 'cached'
    url = f'https://fred.stlouisfed.org/graph/fredgraph.csv?id={series_id}'
    req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
    with urllib.request.urlopen(req, timeout=60) as r:
        data = r.read()
    if len(data) < 200:
        raise RuntimeError(f'too small: {len(data)} bytes')
    with open(path, 'wb') as f:
        f.write(data)
    return f'{len(data)//1024}KB'


def main():
    os.makedirs(CACHE, exist_ok=True)
    for sid, fn, _ in FX:
        try:
            print(f'{sid:18s} -> {fn:14s} {fetch(sid, fn)}')
        except Exception as e:
            print(f'{sid:18s} -> FAILED {type(e).__name__} {str(e)[:60]}')
        time.sleep(0.5)
    for sid, fn in RATES:
        try:
            print(f'{sid:18s} -> {fn:14s} {fetch(sid, fn)}')
        except Exception as e:
            print(f'{sid:18s} -> FAILED {type(e).__name__} {str(e)[:60]}')
        time.sleep(0.5)


if __name__ == '__main__':
    main()
