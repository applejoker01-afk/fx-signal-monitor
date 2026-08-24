"""
cb_meeting_scraper.py
2026-08-25追加: 中央銀行会合日程は最重要イベントなので、ForexFactory経由の
汎用カレンダー（キーワード一致頼み・取りこぼしリスクあり）に頼らず、
主要中銀の公式サイトから直接取得する。

対応済み: FRB(USD), BOE(GBP), BOJ(JPY), ECB(EUR)
未対応（要注意）:
  RBA(AUD)/RBNZ(NZD) … 公式サイトが単純なHTTPリクエストを403でブロックする
                        （Cloudflare等のbot対策とみられる）。cb_rate_scraper.py の
                        RBA金利取得でも同じ403が既に発生しており、既知の制約。
  BOC(CAD)/SNB(CHF)   … 会合日程が載っている正確なURL・HTML構造を未特定。
  それ以外の通貨（手動メンテ対象）は従来通り。

各fetch_*_dates()は失敗時に例外を投げず空リストを返す（呼び出し側で
「取れなかった」と「今後の会合がまだ無い」を区別する必要はなく、
どちらも「calendar_updater側のフォールバックに任せる」で安全に倒せるため）。

戻り値は modules/cb_rate_scraper.reconcile_next_meetings() がそのまま
食えるイベント形式: {"currency": "USD", "date": "YYYY-MM-DDT12:00:00Z",
"name": "Interest Rate Decision", "source": "federalreserve.gov"}
"""

import re
import ssl
import urllib.request
from datetime import datetime, timezone

try:
    import certifi
    _SSL_CONTEXT = ssl.create_default_context(cafile=certifi.where())
except ImportError:
    # certifiが無い環境ではPythonの既定CAストアを使う（動作しない場合もある）
    _SSL_CONTEXT = ssl.create_default_context()

USER_AGENT = "Mozilla/5.0 (compatible; fx-signal-monitor/1.0)"

FOMC_URL = "https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm"
BOE_URL = "https://www.bankofengland.co.uk/monetary-policy/upcoming-mpc-dates"
BOJ_URL = "https://www.boj.or.jp/en/mopo/mpmsche_minu/index.htm"
ECB_URL = "https://www.ecb.europa.eu/press/calendars/mgcgc/html/index.en.html"

MONTHS = {
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6,
    "july": 7, "august": 8, "september": 9, "october": 10, "november": 11, "december": 12,
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "jun": 6, "jul": 7, "aug": 8,
    "sep": 9, "sept": 9, "oct": 10, "nov": 11, "dec": 12,
}


def _http_get(url, timeout=20):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout, context=_SSL_CONTEXT) as resp:
        return resp.read().decode("utf-8", errors="ignore")


def _mk_event(currency, date_obj, source):
    return {
        "currency": currency,
        "date": date_obj.strftime("%Y-%m-%dT12:00:00Z"),
        "name": "Interest Rate Decision",
        "importance": "critical",
        "affects_pairs": [],  # calendar_updater側の対応表を使わないため空のままでよい
        "source": source,
    }


_FOMC_YEAR_HEADER = re.compile(r">(\d{4}) FOMC Meetings<")
_FOMC_MONTH_DATE = re.compile(
    r'fomc-meeting__month[^>]*><strong>([A-Za-z]+)</strong></div>\s*'
    r'<div class="fomc-meeting__date[^>]*>([^<]+)</div>'
)


def fetch_fomc_dates():
    """
    月/日付の見出し(fomc-meeting__month / fomc-meeting__date)から抽出する。

    2026-08-25判明: 当初は声明PDFのファイル名(monetaryYYYYMMDDa*.pdf)から
    日付を取る方式で書いたが、声明PDFは会合が実際に開催された後にしか
    存在しないため、直近の未来の会合（本来一番知りたい情報）が一件も
    取れないという欠陥があった。日付見出し自体は未来の会合もページに
    載っているので、そちらを正とする。日付欄は「27-28」のような範囲
    表記なので、決定発表日である最後の日を採用する。年は
    ">YYYY FOMC Meetings<" の見出しで区切られたセクション単位で判定する
    （ページ内に複数年分のセクションが年代順とは限らない順で並ぶ）。
    """
    try:
        html = _http_get(FOMC_URL)
    except Exception as e:
        print(f"[WARN] FOMC calendar fetch failed: {e}")
        return []

    year_marks = [(m.group(1), m.start()) for m in _FOMC_YEAR_HEADER.finditer(html)]
    year_marks.append((None, len(html)))

    dates = set()
    for i in range(len(year_marks) - 1):
        year, start = year_marks[i]
        _, end = year_marks[i + 1]
        section = html[start:end]
        for month_name, daytext in _FOMC_MONTH_DATE.findall(section):
            month = MONTHS.get(month_name.strip().lower())
            if not month:
                continue
            day_nums = re.findall(r"\d+", daytext)
            if not day_nums:
                continue
            day = int(day_nums[-1])  # 「27-28」のような範囲は決定発表日である最終日を採用
            try:
                dates.add(datetime(int(year), month, day, tzinfo=timezone.utc))
            except ValueError:
                continue

    return [_mk_event("USD", d, "federalreserve.gov") for d in sorted(dates)]


def fetch_boe_dates():
    """
    MPCの'Thursday 5 February'のような表記＋リンク先href内の年(/YYYY/month-YYYY)
    を組み合わせて日付化する。
    """
    try:
        html = _http_get(BOE_URL)
    except Exception as e:
        print(f"[WARN] BOE calendar fetch failed: {e}")
        return []

    events = []
    # 2026-08-25判明: 第2の年（後続テーブル）に href の無い行があると、素朴な
    # `.*?` が同じ<td>セルを越えて次の行のhrefまで読みに行き、日付と無関係な
    # 年を拾ってしまうバグがあった(実測: 12月の次の行が2027年なのに2026年と
    # 誤認識)。`(?:(?!</td>).)*?` でセル境界を越えないよう制限する。
    row_pattern = re.compile(
        r"<td>[A-Za-z]+(?:&nbsp;|\s)+(\d{1,2})\s+([A-Za-z]+)</td>\s*"
        r"<td>(?:(?!</td>).)*?/(\d{4})/",
        re.S,
    )
    for day, month_name, year in row_pattern.findall(html):
        month = MONTHS.get(month_name.strip().lower())
        if not month:
            continue
        try:
            dt = datetime(int(year), month, int(day), tzinfo=timezone.utc)
        except ValueError:
            continue
        events.append(_mk_event("GBP", dt, "bankofengland.co.uk"))

    return events


def fetch_boj_dates():
    """
    スケジュール表の各行1列目（例: "June 15 (Mon.), 16 (Tues.) [PDF 187KB]"）から
    月と最終日（=決定発表日）を抽出する。

    2026-08-25判明: 当初は結果公表PDFのファイル名(kYYMMDDa*.pdf)から取る方式
    だったが、FOMCと同じ理由でPDFは会合後にしか存在せず、未来の会合が
    一件も取れなかった。表の日付テキスト自体は未来分も載っているので
    そちらを使う。年は"Table : YYYY"見出しで区切られたセクション単位。
    """
    try:
        html = _http_get(BOJ_URL)
    except Exception as e:
        print(f"[WARN] BOJ calendar fetch failed: {e}")
        return []

    table_marks = [(m.group(1), m.start()) for m in re.finditer(r"Table\s*:\s*(\d{4})", html)]
    table_marks.append((None, len(html)))

    dates = set()
    for i in range(len(table_marks) - 1):
        year, start = table_marks[i]
        _, end = table_marks[i + 1]
        section = html[start:end]
        for row in re.findall(r"<tr>(.*?)</tr>", section, re.S):
            cells = re.findall(r"<td>(.*?)</td>", row, re.S)
            if not cells:
                continue
            first = re.sub(r"<[^>]+>", " ", cells[0])
            first = re.sub(r"\[.*?\]", " ", first)  # "[PDF 187KB]"のようなファイルサイズ表記を除去
            first = re.sub(r"\s+", " ", first).strip()
            m = re.match(r"([A-Za-z]+)\.?\s+(\d{1,2})", first)
            if not m:
                continue
            month = MONTHS.get(m.group(1).lower())
            if not month:
                continue
            # 年末の会合はまれに行内に翌年表記("Jan. 27 (Wed.), 2027")を含む
            yr_override = re.search(r",\s*(\d{4})\s*$", first)
            use_year = yr_override.group(1) if yr_override else year
            day_nums = re.findall(r"\b\d{1,2}\b", first)
            if not day_nums:
                continue
            day = int(day_nums[-1])
            try:
                dates.add(datetime(int(use_year), month, day, tzinfo=timezone.utc))
            except ValueError:
                continue

    return [_mk_event("JPY", d, "boj.or.jp") for d in sorted(dates)]


def fetch_ecb_dates():
    """
    <dt>DD/MM/YYYY</dt><dd>...</dd> の定義リストから、政策理事会の金融政策会合
    かつ「Day 2」または「press conference」を含む(=決定発表日)、
    「non-monetary policy」を含まないものだけを抽出する。
    """
    try:
        html = _http_get(ECB_URL)
    except Exception as e:
        print(f"[WARN] ECB calendar fetch failed: {e}")
        return []

    events = []
    for date_str, body in re.findall(r"<dt>\s*(\d{2}/\d{2}/\d{4})\s*</dt>\s*<dd>(.*?)</dd>", html, re.S):
        body_lower = body.lower()
        if "monetary policy meeting" not in body_lower:
            continue
        if "non-monetary policy" in body_lower:
            continue
        if "day 2" not in body_lower and "press conference" not in body_lower:
            continue
        try:
            dt = datetime.strptime(date_str, "%d/%m/%Y").replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        events.append(_mk_event("EUR", dt, "ecb.europa.eu"))

    return events


FETCHERS = {
    "USD": fetch_fomc_dates,
    "GBP": fetch_boe_dates,
    "JPY": fetch_boj_dates,
    "EUR": fetch_ecb_dates,
}


def fetch_all_meeting_events():
    """
    対応済み中銀を全部取得し、1本のイベントリストにまとめて返す。
    通貨単位で失敗しても他の通貨には影響しない。
    """
    all_events = []
    errors = []
    for ccy, fetcher in FETCHERS.items():
        try:
            events = fetcher()
            if events:
                all_events.extend(events)
            else:
                errors.append(f"{ccy}: no dates parsed")
        except Exception as e:
            errors.append(f"{ccy}: {e}")

    if errors:
        print(f"[WARN] cb_meeting_scraper partial failures: {errors}")

    return all_events


if __name__ == "__main__":
    for ev in fetch_all_meeting_events():
        print(ev["currency"], ev["date"], ev["source"])
