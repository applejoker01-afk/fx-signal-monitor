"""
cot_analysis.py
CFTC COT（Commitment of Traders）ポジショニングデータから、
投機筋の円ショート（キャリートレード）の「巻き戻し」を検知する。

背景（2026-08-11）: FA/TAゲートが金利差ロジックのみで構成されており、
日銀の低金利が続く限りSHORTが構造的にほぼ発生しない問題が判明した。
data/cot_jpy.csv は2005年から蓄積されているが、コード内のどこからも
参照されていない未使用データだった。

投機筋の円ネットポジション（net = spec_long - spec_short）が急激に
増加（＝円ショートを急速に手仕舞い）している時は、金利差では説明できない
「ポジション巻き戻し」による円高圧力が発生している可能性が高い。これを
FAスコアへのペナルティとして反映し、JPYクロスのSHORTを条件付きで
解禁できるか検証する。

先読みバイアス対策: report_date（集計基準日）ではなく release_date
（実際に公表された日）を使い、backtestの各日付時点で「その日に実際に
入手可能だった直近のCOTレポート」だけを参照する。
"""

import csv
import statistics
from datetime import datetime, date

COT_JPY_FILE = "data/cot_jpy.csv"


def load_cot_jpy(path: str = COT_JPY_FILE) -> list:
    """
    data/cot_jpy.csv を読み込み、日付でソートしたレコードのリストを返す。
    各レコード: {"report_date": date, "release_date": date, "net": int, ...}
    """
    rows = []
    with open(path, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for r in reader:
            try:
                rows.append({
                    "report_date": datetime.strptime(r["report_date"], "%Y-%m-%d").date(),
                    "release_date": datetime.strptime(r["release_date"], "%Y-%m-%d").date(),
                    "spec_long": int(r["spec_long"]),
                    "spec_short": int(r["spec_short"]),
                    "open_interest": int(r["open_interest"]),
                    "net": int(r["net"]),
                })
            except (ValueError, KeyError):
                continue
    rows.sort(key=lambda r: r["report_date"])
    return rows


def get_unwind_signal(as_of: date, cot_data: list, lookback_weeks: int = 52,
                       z_threshold: float = 1.5, crowd_threshold: int = -80000) -> dict:
    """
    as_of時点で実際に入手可能だった直近のCOTレポートから、
    「円ショートの巻き戻しが進行中か」を判定する。

    ロジック:
      1. as_of以前にrelease_dateが来ているレコードのみを対象にする（先読み防止）。
      2. 直近レコードの net の週次変化(delta)を、過去lookback_weeks分のdeltaの
         標準偏差で正規化したz-scoreを計算する。
      3. 「1週間前の時点でnetが十分マイナス（円ショートが積み上がっていた=crowded）」
         かつ「今週deltaが大きくプラス（急速に円ショートを手仕舞い）」の両方が
         揃った時だけ unwind_active = True とする。
         → 単なるノイズ的な増減と、実際の"混雑解消"イベントを区別するため。

    Returns:
        {
          "available": True/False,
          "net": 直近net,
          "prev_net": 1週間前net,
          "delta": 週次変化,
          "z_score": 正規化した変化幅,
          "was_crowded": bool（手仕舞い前の時点で十分ショートに偏っていたか）,
          "unwind_active": bool,
          "report_date": ..., "release_date": ...,
        }
    """
    usable = [r for r in cot_data if r["release_date"] <= as_of]
    if len(usable) < 2:
        return {"available": False, "unwind_active": False}

    usable = usable[-(lookback_weeks + 1):] if len(usable) > lookback_weeks + 1 else usable
    deltas = [usable[i]["net"] - usable[i - 1]["net"] for i in range(1, len(usable))]
    if len(deltas) < 5:
        return {"available": False, "unwind_active": False}

    latest = usable[-1]
    prev = usable[-2]
    delta = latest["net"] - prev["net"]

    hist_deltas = deltas[:-1] if len(deltas) > 1 else deltas
    std = statistics.pstdev(hist_deltas) if len(hist_deltas) >= 2 else 0
    z = (delta / std) if std > 0 else 0.0

    was_crowded = prev["net"] <= crowd_threshold
    unwind_active = was_crowded and z >= z_threshold and delta > 0

    return {
        "available": True,
        "net": latest["net"],
        "prev_net": prev["net"],
        "delta": delta,
        "z_score": round(z, 2),
        "was_crowded": was_crowded,
        "unwind_active": unwind_active,
        "report_date": str(latest["report_date"]),
        "release_date": str(latest["release_date"]),
    }


def apply_cot_unwind_adjustment(fa_result: dict, unwind_signal: dict, penalty: float = 25.0) -> dict:
    """
    JPYクロスのFAスコアに、COT巻き戻しシグナルによるペナルティを適用する。
    unwind_active=Trueの時のみ、buy方向のスコアを penalty 分だけ引き下げる
    （円ショート筋の急速な手仕舞い＝円高圧力＝JPYクロスの買い根拠を弱める）。

    fa_resultはcompute_fa_score()の返り値をそのまま渡す想定。
    破壊的変更を避けるため、コピーを返す。
    """
    adjusted = dict(fa_result)
    if not unwind_signal.get("unwind_active"):
        adjusted["cot_adjustment"] = 0.0
        adjusted["cot_note"] = ""
        return adjusted

    original_score = adjusted["score"]
    new_score = max(0, min(100, original_score - penalty))
    adjusted["score"] = round(new_score, 1)
    adjusted["cot_adjustment"] = -penalty
    adjusted["cot_note"] = (
        f"COT巻き戻し検知(z={unwind_signal['z_score']}, "
        f"net {unwind_signal['prev_net']:+d}→{unwind_signal['net']:+d})"
    )

    if new_score >= 60:
        adjusted["direction"] = "buy"
    elif new_score <= 40:
        adjusted["direction"] = "sell"
    else:
        adjusted["direction"] = "neutral"

    return adjusted
