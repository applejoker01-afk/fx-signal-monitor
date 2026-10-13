"""rate_fetcher の FAスコア対称性・stance陳腐化検知のテスト（2026-10-13追加）。

実行: python -m pytest tests/test_rate_fetcher.py -q
"""
import itertools
import json
import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from modules import rate_fetcher  # noqa: E402
from modules.rate_fetcher import compute_fa_score  # noqa: E402

STANCES = ["tighten", "neutral", "pause", "ease"]
MOMENTA = ["accelerating", "decelerating", "peak", "trough", "stable"]


def _score(a, b, cb):
    return compute_fa_score("X", {"X": (a, b)}, cb)["score"]


def test_fa_score_is_mirror_symmetric_for_all_stance_combinations():
    """score(A/B) + score(B/A) == 100 が全stance・金利差・モメンタムの組合せで成立する。
    旧実装は34ペア中11ペアで最大±10点ずれていた。"""
    for sa, sb, ma, mb, (ra, rb) in itertools.product(
        STANCES, STANCES, MOMENTA, MOMENTA,
        [(4.0, 1.0), (1.0, 4.0), (2.0, 2.0), (2.1, 2.0)],
    ):
        cb = {
            "AAA": {"rate": ra, "stance": sa, "rate_momentum": ma},
            "BBB": {"rate": rb, "stance": sb, "rate_momentum": mb},
        }
        fwd, rev = _score("AAA", "BBB", cb), _score("BBB", "AAA", cb)
        # 0/100 クランプにかかる極端ケースは対象外
        if 0 < fwd < 100 and 0 < rev < 100:
            assert abs(fwd + rev - 100) < 1e-6, (sa, sb, ma, mb, ra, rb, fwd, rev)


def test_quote_currency_tightening_lowers_score():
    """TO通貨の利上げ（例: AUDNZDでNZD利上げ）は FROM買い方向のスコアを下げる。
    旧実装では rate_diff>0 のとき TO側stanceが無視されていた。"""
    base = {"AUD": {"rate": 4.6, "stance": "neutral"}, "NZD": {"rate": 2.75, "stance": "neutral"}}
    hawk = {"AUD": {"rate": 4.6, "stance": "neutral"}, "NZD": {"rate": 2.75, "stance": "tighten"}}
    assert _score("AUD", "NZD", hawk) < _score("AUD", "NZD", base)


def test_stance_counts_when_rate_diff_is_exactly_zero():
    """金利差ちょうど0（GBPUSD等）でも stance の違いが反映される。"""
    cb = {"GBP": {"rate": 3.75, "stance": "tighten"}, "USD": {"rate": 3.75, "stance": "ease"}}
    assert _score("GBP", "USD", cb) == 65.0


def _write_cb(tmp_path, payload):
    p = tmp_path / "cb.json"
    p.write_text(json.dumps(payload), encoding="utf-8")
    return str(p)


def test_staleness_ignores_auto_bumped_last_updated(tmp_path, monkeypatch):
    """last_updated が今日でも、stance_last_verified が古ければ警告する。
    旧実装は日次スクレイパーが last_updated を毎日書き換えるため永久に無警告だった。"""
    today = datetime.now(timezone.utc).date()
    old = (today - timedelta(days=60)).isoformat()
    path = _write_cb(tmp_path, {
        "last_updated": today.isoformat(),
        "stance_last_verified": old,
        "rates": {"MXN": {"rate": 9.0, "stance": "ease"},
                  "USD": {"rate": 4.0, "stance": "tighten", "stance_verified": today.isoformat()}},
    })
    monkeypatch.setattr(rate_fetcher, "CENTRAL_BANK_FILE", path)
    w = rate_fetcher.check_central_bank_data_staleness(max_age_days=45)
    assert len(w) == 1 and w[0]["ccy"] == "ALL"
    assert "MXN" in w[0]["message"] and "USD" not in w[0]["message"]


def test_staleness_flags_rate_auto_change_after_last_verification(tmp_path, monkeypatch):
    """自動取得で金利が変わった日 > stance検証日 の通貨は、見直されるまで警告し続ける。"""
    today = datetime.now(timezone.utc).date()
    path = _write_cb(tmp_path, {
        "stance_last_verified": (today - timedelta(days=20)).isoformat(),
        "rates": {
            "EUR": {"rate": 2.65, "stance": "neutral", "cb_name": "ECB",
                    "last_auto_update": (today - timedelta(days=5)).isoformat()},
            "GBP": {"rate": 3.75, "stance": "neutral",
                    "last_auto_update": (today - timedelta(days=30)).isoformat()},
        },
    })
    monkeypatch.setattr(rate_fetcher, "CENTRAL_BANK_FILE", path)
    w = rate_fetcher.check_central_bank_data_staleness(max_age_days=45)
    assert [x["ccy"] for x in w] == ["EUR"]


def test_legacy_file_without_verified_field_falls_back_to_last_updated(tmp_path, monkeypatch):
    old = (datetime.now(timezone.utc).date() - timedelta(days=50)).isoformat()
    path = _write_cb(tmp_path, {"last_updated": old, "rates": {"JPY": {"rate": 1.25, "stance": "tighten"}}})
    monkeypatch.setattr(rate_fetcher, "CENTRAL_BANK_FILE", path)
    w = rate_fetcher.check_central_bank_data_staleness(max_age_days=45)
    assert len(w) == 1 and "JPY" in w[0]["message"]
