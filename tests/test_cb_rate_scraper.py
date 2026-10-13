"""中銀金利スクレイパー（RBA F1.1 / BIS / 上書きガード）のテスト（2026-10-13追加）。

実行: python -m pytest tests/ -q
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from modules import cb_rate_scraper as scr  # noqa: E402


# ---------------------------------------------------------------- cb_rate_scraper

# RBA F1（日次）CSV の実際の先頭構造を縮約したもの（2026-10-13取得）
RBA_F1_SAMPLE = (
    "\ufeffF1 INTEREST RATES AND YIELDS - MONEY MARKET\n"
    "Title,Cash Rate Target,Change in the Cash Rate Target,Interbank Overnight Cash Rate\n"
    "Frequency,Daily,as announced,Daily\n"
    "\n"
    "Series ID,FIRMMCRTD,FIRMMCCRT,FIRMMCRID\n"
    "29-Sep-2026,4.35,,4.35\n"
    "30-Sep-2026,4.60,0.25,4.59\n"
    "12-Oct-2026,4.60,,4.60\n"
    "13-Oct-2026,,,\n"
)  # 最終行: 当日分は空欄で先行掲載される


def test_parse_rba_f1_reads_cash_rate_target_by_series_id():
    assert scr.parse_rba_f1_csv(RBA_F1_SAMPLE) == {
        "rate": 4.6, "date": "2026-10-12", "source": "RBA F1 FIRMMCRTD"}


def test_parse_rba_rejects_table_without_target_series():
    """F1.1（月平均, 系列ID FIRMMCRT）を誤って読むと 9月=4.36% になる。日次系列IDが無ければ None。"""
    f11 = ("Title,Cash Rate Target\nSeries ID,FIRMMCRT,FIRMMCRI\n"
           "31/08/2026,4.35,4.35\n30/09/2026,4.36,4.36\n")
    assert scr.parse_rba_f1_csv(f11) is None


def test_parse_bis_csv_latest_per_area():
    text = ("FREQ,REF_AREA,TIME_PERIOD,OBS_VALUE\n"
            "D,AU,2026-09-23,4.35\nD,AU,2026-09-24,4.35\nD,MX,2026-10-06,6.5\n")
    assert scr.parse_bis_cbpol_csv(text) == {
        "AU": {"rate": 4.35, "date": "2026-09-24"}, "MX": {"rate": 6.5, "date": "2026-10-06"}}


def _run_update(tmp_path, monkeypatch, file_payload, fetched):
    path = tmp_path / "cb.json"
    path.write_text(json.dumps(file_payload), encoding="utf-8")
    monkeypatch.setattr(scr, "CB_RATES_FILE", str(path))
    monkeypatch.setattr(scr, "FETCH_FUNCTIONS", {c: (lambda r=r: r) for c, r in fetched.items()})
    scr.update_central_bank_rates()
    return json.loads(path.read_text(encoding="utf-8"))


def test_lagging_source_does_not_override_newer_manual_verification(tmp_path, monkeypatch):
    """BIS の AUD 4.35%（09-24観測）で、10-13 に手動検証した 4.60% を上書きしない。"""
    out = _run_update(tmp_path, monkeypatch,
                      {"stance_last_verified": "2026-07-21",
                       "rates": {"AUD": {"rate": 4.6, "stance_verified": "2026-10-13"}}},
                      {"AUD": {"rate": 4.35, "date": "2026-09-24", "source": "BIS WS_CBPOL"}})
    assert out["rates"]["AUD"]["rate"] == 4.6
    assert "last_auto_update" not in out["rates"]["AUD"]


def test_newer_observation_updates_and_keeps_last_updated(tmp_path, monkeypatch):
    out = _run_update(tmp_path, monkeypatch,
                      {"last_updated": "2026-10-13", "stance_last_verified": "2026-07-21",
                       "rates": {"MXN": {"rate": 9.0, "stance": "ease"}}},
                      {"MXN": {"rate": 6.5, "date": "2026-10-06", "source": "BIS WS_CBPOL"}})
    assert out["rates"]["MXN"]["rate"] == 6.5
    assert out["rates"]["MXN"]["last_auto_update"] == "2026-10-06"
    assert out["rates"]["MXN"]["stance"] == "ease"          # stance は自動変更しない
    assert out["last_updated"] == "2026-10-13"              # last_updated は書き換えない
