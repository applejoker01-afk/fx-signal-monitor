"""キャリー・ペーパー台帳のテスト（2026-10-13追加）。

実行: python -m pytest tests/ -q
"""
import os
import sys
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from modules import paper_ledger as pl  # noqa: E402

RATES = {"AUD": {"rate": 4.6}, "JPY": {"rate": 1.25}}


# ---------------------------------------------------------------- paper_ledger

def test_carry_accrues_by_calendar_days_like_research():
    """金利収益は経過暦日/365（research の excess() と同じ）。金曜→月曜は3日分。"""
    st = pl.new_state()
    st["positions"] = {"AUDJPY": {"weight": 1.0, "mark": 100.0, "mark_date": "2026-10-09"}}
    st["last_mark_date"] = "2026-10-09"
    pl.mark_to_market(st, {"AUDJPY": 100.0}, RATES, as_of=date(2026, 10, 12))
    expected = (4.6 - 1.25) / 100 * 3 / 365
    assert abs(st["daily"][-1]["ret"] - expected) < 1e-12


def test_skipped_price_day_is_caught_up_next_time():
    st = pl.new_state()
    st["positions"] = {"AUDJPY": {"weight": 1.0, "mark": 100.0, "mark_date": "2026-10-12"}}
    st["last_mark_date"] = "2026-10-12"
    pl.mark_to_market(st, {}, RATES, as_of=date(2026, 10, 13))       # 価格取得失敗
    pl.mark_to_market(st, {"AUDJPY": 100.0}, RATES, as_of=date(2026, 10, 14))
    assert abs(st["daily"][-1]["ret"] - (4.6 - 1.25) / 100 * 2 / 365) < 1e-12


def test_no_double_counting_same_day():
    st = pl.new_state()
    st["positions"] = {"AUDJPY": {"weight": 1.0, "mark": 100.0, "mark_date": "2026-10-12"}}
    assert pl.mark_to_market(st, {"AUDJPY": 101.0}, RATES, as_of=date(2026, 10, 13))[0]
    assert not pl.mark_to_market(st, {"AUDJPY": 102.0}, RATES, as_of=date(2026, 10, 13))[0]


def test_rebalance_every_21_weekdays_not_calendar_days(monkeypatch):
    st = {"last_rebalance": "2026-10-01"}
    monkeypatch.setattr(pl, "_today", lambda: date(2026, 10, 22))   # 暦日21日 = 平日15日
    assert not pl.needs_rebalance(st)
    monkeypatch.setattr(pl, "_today", lambda: date(2026, 10, 30))   # 平日21日
    assert pl.needs_rebalance(st)
