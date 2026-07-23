"""
position_sizing.py
シミュレーション口座（仮想資金）に基づくポジションサイジング計算。

SBI FXトレード公式仕様（2026-07-20 Web調査で確認）に基づく:
- レバレッジ25倍固定。必要証拠金 = 基準価格 ÷ 25 × 取引数量（複数ポジションは単純合算、
  両建て時のみ例外的に大きい方のみ課金）。
  Source: https://www.sbifxt.co.jp/beginner/step03_5.html
- ロスカット: 証拠金維持率 = (資産評価額 ÷ 取引必要証拠金) × 100 が **50%を下回ると
  全ポジション強制決済**。判定は原則20秒ごと。
  Source: https://www.sbifxt.co.jp/beginner/step03_6.html
- 取引単位: 取引倍率(×100/×1,000/×10,000/×100,000)×取引数量(整数)で実数量を指定する。
  本モジュールは「最小取引単位(通常100、KRWJPYのみ1万)の整数倍」としてこれをモデル化。
  取引倍率×100を選べば本モジュールの推奨単位数をそのまま再現できる。
  Source: https://search.sbisec.co.jp/v2/popwin/guide/tool/fx_trading/04_trade/order_new.html

運用方針（ユーザー指定 2026-07-20）:
- 初期資金: 1万円。積み立てながら資金を注入していく（add_depositで反映）。
- 1トレードあたりリスク: 資金の3%まで。
- レバレッジ: 25倍固定（日本の個人向けFX上限）。
- 残高不足で最小取引単位すら取れない場合はエントリーしない（tradable=Falseを返す）。

証拠金使用上限のデフォルト値（2026-07-20改訂）:
  実際のロスカット閾値(維持率50%)から逆算し、新規建て直後の維持率が約330%
  （＝合計必要証拠金が資産評価額の約30%以内）を保つように max_margin_usage_pct=30.0
  をデフォルトとした。この場合、ロスカット(維持率50%)に到達するには保有ポジション全体の
  含み損が資産評価額の約85%に達する必要があり、ノーマルなFX変動では極めて起こりにくい
  （2024年8月のキャリー崩壊級のショックでも数週間かけて±14%程度）。
  ただしSBI自身が「週末・薄商い時間帯の急変時はロスカットが間に合わず預託額を超える
  損失が生じうる」と明記しているため、複数ポジション同時保有時や新興国通貨（BRL/TRY/
  ZAR/KRW等）保有時はこの理論値より慎重に見るべき。この値は要調整の初期値。
"""

import json
import os
from datetime import datetime, timezone

ACCOUNT_FILE = "data/virtual_account.json"
OPEN_TRADES_FILE = "data/open_trades.json"

LEVERAGE = 25  # 日本の個人向けFX上限レバレッジ

# SBI FXトレード公式のロスカット閾値（証拠金維持率）。これを下回ると全ポジション強制決済。
LOSS_CUT_MAINTENANCE_RATIO = 50.0

# 通貨ペアごとの最小取引単位（FROM通貨建て）。SBI証券公式仕様（2026-07-20時点）。
# KRWJPYのみ1万ウォン単位、それ以外は全て100単位。
MIN_UNIT_DEFAULT = 100
MIN_UNIT_OVERRIDE = {
    "KRWJPY": 10000,
}


def get_min_unit(pair: str) -> int:
    return MIN_UNIT_OVERRIDE.get(pair, MIN_UNIT_DEFAULT)


def default_account() -> dict:
    today = datetime.now(timezone.utc).date().isoformat()
    return {
        "starting_balance": 10000,
        "current_balance": 10000,
        "currency": "JPY",
        "leverage": LEVERAGE,
        "risk_pct_per_trade": 3.0,
        # 新規建て時に「既存の保有ポジション分の証拠金」も合算した上でこの割合(資産評価額比)
        # を超えないようにする。2026-07-20: ロスカット閾値50%から逆算した根拠つきの値。
        # 上記モジュールdocstring参照。
        "max_margin_usage_pct": 30.0,
        "deposit_history": [
            {"date": today, "amount": 10000, "note": "初期資金"}
        ],
        "last_updated": today,
    }


def load_virtual_account() -> dict:
    if not os.path.exists(ACCOUNT_FILE):
        acc = default_account()
        save_virtual_account(acc)
        return acc
    try:
        with open(ACCOUNT_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default_account()


def save_virtual_account(account: dict):
    os.makedirs("data", exist_ok=True)
    with open(ACCOUNT_FILE, "w", encoding="utf-8") as f:
        json.dump(account, f, ensure_ascii=False, indent=2)


def add_deposit(amount: float, note: str = "") -> dict:
    """資金を追加（積み立て）。current_balanceに加算し、履歴に記録する。"""
    account = load_virtual_account()
    account["current_balance"] = account.get("current_balance", 0) + amount
    account.setdefault("deposit_history", []).append({
        "date": datetime.now(timezone.utc).date().isoformat(),
        "amount": amount,
        "note": note or "追加入金",
    })
    account["last_updated"] = datetime.now(timezone.utc).date().isoformat()
    save_virtual_account(account)
    return account


def record_trade_pnl(pnl_jpy: float) -> dict:
    """決済トレードの損益(円)をシミュレーション口座残高に反映する。"""
    account = load_virtual_account()
    account["current_balance"] = account.get("current_balance", 0) + pnl_jpy
    account["last_updated"] = datetime.now(timezone.utc).date().isoformat()
    save_virtual_account(account)
    return account


def from_currency_jpy_rate(from_ccy: str, latest_pairs: dict):
    """
    FROM通貨の対円レートを取得。latest_pairs (fetch_latest_ratesの'pairs') 内の
    {FROM}JPYペアの価格を返す。JPYそのものなら1.0。見つからなければNone。
    """
    if from_ccy == "JPY":
        return 1.0
    return latest_pairs.get(f"{from_ccy}JPY")


def pnl_to_jpy(pair: str, pair_api: dict, pips_price_diff: float,
               latest_pairs: dict):
    """
    決済トレードの生pips(価格差、entry-exit)を円建て損益(1FROM通貨単位あたり)に変換する。
    to_ccy=='JPY'ならpips_price_diffがそのまま円/単位。それ以外はTO/JPYレートで換算。
    変換できない場合はNoneを返す。
    """
    if pair not in pair_api:
        return None
    _, to_ccy = pair_api[pair]
    if to_ccy == "JPY":
        return pips_price_diff
    to_jpy_rate = from_currency_jpy_rate(to_ccy, latest_pairs)
    if to_jpy_rate is None:
        return None
    return pips_price_diff * to_jpy_rate


def _margin_per_unit_jpy(entry_or_current_price: float, to_ccy: str,
                          from_jpy_rate: float, leverage: float) -> float:
    """1FROM通貨単位あたりの必要証拠金(円)。SBI公式式: 基準価格÷レバレッジ。"""
    notional_per_unit_jpy = entry_or_current_price if to_ccy == "JPY" else from_jpy_rate
    return notional_per_unit_jpy / leverage


def load_open_trades_raw() -> dict:
    """trade_trackerに依存せず直接open_trades.jsonを読む（循環import回避用）。

    2026-07-24: スキーマが{pair: trade}(旧)/{pair: [trade,...]}(新)どちらでも
    後方互換で読めるよう、常にリストへ正規化して返す
    （trade_tracker.load_open_trades()と同じ移行ロジック）。
    """
    if not os.path.exists(OPEN_TRADES_FILE):
        return {}
    try:
        with open(OPEN_TRADES_FILE, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except Exception:
        return {}
    return {pair: (v if isinstance(v, list) else [v]) for pair, v in raw.items()}


def _iter_trades(open_trades: dict):
    """{pair: [trade,...]}を(pair, trade)のフラットなイテレータにする。
    後方互換: 値がdict(旧スキーマ)ならそのまま1件として扱う。"""
    for pair, v in open_trades.items():
        trades = v if isinstance(v, list) else [v]
        for trade in trades:
            yield pair, trade


def total_open_margin_jpy(open_trades: dict, pair_api: dict, latest_pairs: dict) -> float:
    """
    現在保有中の全ポジションの必要証拠金(円)を合算する。
    可能な限り現在レートで再計算し（含み損益の変動を反映）、レート取得不可の場合は
    エントリー時点で記録したmargin_required_jpyにフォールバックする。

    2026-07-24: 1ペアにつき最大2ポジション（ピラミッディング）保有可能になったため、
    {pair: [trade,...]}の全トレードを合算する。両建て（同一ペアで買い・売り同時保有）の
    証拠金圧縮はSBI仕様上あり得るが、本システムはピラミッディング時も同一方向のみを
    許容する設計のため考慮不要。
    """
    total = 0.0
    for pair, trade in _iter_trades(open_trades):
        units = trade.get("units")
        if not units:
            continue
        if pair in pair_api:
            from_ccy, to_ccy = pair_api[pair]
            from_jpy_rate = from_currency_jpy_rate(from_ccy, latest_pairs)
            current_price = latest_pairs.get(pair)
            if from_jpy_rate is not None and current_price is not None:
                margin_per_unit = _margin_per_unit_jpy(current_price, to_ccy, from_jpy_rate, LEVERAGE)
                total += units * margin_per_unit
                continue
        # フォールバック: エントリー時点の記録値
        sizing = trade.get("position_sizing") or {}
        total += sizing.get("margin_required_jpy", 0) or 0
    return total


def unrealized_pnl_jpy(open_trades: dict, pair_api: dict, latest_pairs: dict) -> float:
    """保有中全ポジションの含み損益(円)を合算する。レート取得不可のペアは0として扱う。
    2026-07-24: {pair: [trade,...]}の全トレードを合算するよう変更。"""
    total = 0.0
    for pair, trade in _iter_trades(open_trades):
        units = trade.get("units")
        if not units or pair not in pair_api:
            continue
        current_price = latest_pairs.get(pair)
        entry_price = trade.get("entry_price")
        if current_price is None or entry_price is None:
            continue
        is_long = str(trade.get("direction", "")).endswith("LONG")
        price_diff = (current_price - entry_price) if is_long else (entry_price - current_price)
        pnl_per_unit = pnl_to_jpy(pair, pair_api, price_diff, latest_pairs)
        if pnl_per_unit is not None:
            total += units * pnl_per_unit
    return total


def calc_maintenance_ratio(account: dict, open_trades: dict, pair_api: dict,
                            latest_pairs: dict) -> dict:
    """
    現在の証拠金維持率を算出する（SBI公式式: 資産評価額÷取引必要証拠金×100）。
    保有ポジションが無い場合はNoneを返す（維持率は定義されない）。

    Returns:
        {
          "maintenance_ratio": float or None,
          "equity_jpy": float,       # 資産評価額 = 残高 + 含み損益
          "total_margin_jpy": float, # 合計必要証拠金
          "loss_cut_alert": bool,    # 実際のロスカット閾値(50%)に近い/割れているか
        }
    """
    balance = account.get("current_balance", 0)
    total_margin = total_open_margin_jpy(open_trades, pair_api, latest_pairs)
    if total_margin <= 0:
        return {
            "maintenance_ratio": None, "equity_jpy": balance,
            "total_margin_jpy": 0, "loss_cut_alert": False,
        }
    unrealized = unrealized_pnl_jpy(open_trades, pair_api, latest_pairs)
    equity = balance + unrealized
    ratio = (equity / total_margin) * 100
    return {
        "maintenance_ratio": round(ratio, 1),
        "equity_jpy": round(equity, 0),
        "total_margin_jpy": round(total_margin, 0),
        "loss_cut_alert": ratio < LOSS_CUT_MAINTENANCE_RATIO * 1.5,  # 75%を下回ったら早期警告
    }


def calc_position_size(pair: str, entry_price: float, sl_price: float,
                        pair_api: dict, latest_pairs: dict,
                        account: dict = None, open_trades: dict = None,
                        exposure_multiplier: float = 1.0) -> dict:
    """
    仮想口座残高・リスク許容度・SL値幅から推奨ロット数を算出する。

    open_trades を渡すと、既存の保有ポジション分の必要証拠金も合算した上で
    max_margin_usage_pct の上限を判定する（2026-07-20修正: 従来は新規ポジション単体
    でしか判定しておらず、複数ポジション保有時に合計証拠金がロスカット閾値に接近する
    リスクを見落としていた）。省略時はopen_trades.jsonを自動で読み込む。

    exposure_multiplier: 通貨相関リスクによるロット圧縮倍率（2026-07-21追加、
    modules.advanced_analytics.calc_correlated_exposure_multiplier()で算出）。
    1.0未満ならリスク許容額を圧縮し、AUDJPY×NZDJPY同時保有のような
    「別ペアでも同方向の通貨エクスポージャーが積み上がる」組み合わせのロットを抑える。

    Returns:
        {
          "tradable": bool,
          "units": int,               # 推奨取引単位数（FROM通貨建て、最小単位刻み）
          "margin_required_jpy": float,
          "risk_amount_jpy": float,   # リスク許容額（残高×risk_pct）
          "estimated_loss_jpy": float,# 実際にSLヒット時に想定される損失額
          "existing_margin_jpy": float, # 参考: 判定に使った既存ポジションの合計証拠金
          "note": str,
        }
    """
    if account is None:
        account = load_virtual_account()
    if open_trades is None:
        open_trades = load_open_trades_raw()

    balance = account.get("current_balance", 0)
    risk_pct = account.get("risk_pct_per_trade", 3.0)
    max_margin_pct = account.get("max_margin_usage_pct", 30.0)
    leverage = account.get("leverage", LEVERAGE)
    min_unit = get_min_unit(pair)

    if pair not in pair_api:
        return {"tradable": False, "units": 0, "note": f"{pair}: 未対応ペア"}
    if entry_price is None or sl_price is None:
        return {"tradable": False, "units": 0, "note": "価格データ不足"}

    from_ccy, to_ccy = pair_api[pair]
    sl_distance = abs(entry_price - sl_price)
    if sl_distance <= 0:
        return {"tradable": False, "units": 0, "note": "SL距離が不正"}

    from_jpy_rate = from_currency_jpy_rate(from_ccy, latest_pairs)
    if from_jpy_rate is None:
        return {"tradable": False, "units": 0, "note": f"{from_ccy}の対円レート取得不可"}

    # 1単位(FROM通貨1単位)あたりの損失額(円)
    # 例: USDJPYでSL値幅0.936円 → 1USDあたり損失0.936円（TOがJPYなので直接）
    # 例: EURUSDでSL値幅0.003USD → 1EURあたり損失 = 0.003 × USDJPYレート
    if to_ccy == "JPY":
        loss_per_unit_jpy = sl_distance
    else:
        to_jpy_rate = from_currency_jpy_rate(to_ccy, latest_pairs)
        if to_jpy_rate is None:
            return {"tradable": False, "units": 0, "note": f"{to_ccy}の対円レート取得不可"}
        loss_per_unit_jpy = sl_distance * to_jpy_rate

    if loss_per_unit_jpy <= 0:
        return {"tradable": False, "units": 0, "note": "損失単価の計算に失敗"}

    exposure_multiplier = max(0.0, min(1.0, exposure_multiplier))
    risk_amount_jpy = balance * (risk_pct / 100.0) * exposure_multiplier
    raw_units_risk = risk_amount_jpy / loss_per_unit_jpy

    # 証拠金ベースの上限units（既存ポジション分を差し引いた残り予算で判定）
    margin_per_unit_jpy = _margin_per_unit_jpy(entry_price, to_ccy, from_jpy_rate, leverage)
    max_margin_jpy = balance * (max_margin_pct / 100.0)
    existing_margin_jpy = total_open_margin_jpy(open_trades, pair_api, latest_pairs)
    available_margin_jpy = max(0.0, max_margin_jpy - existing_margin_jpy)
    raw_units_margin = (
        available_margin_jpy / margin_per_unit_jpy if margin_per_unit_jpy > 0 else 0
    )

    raw_units = min(raw_units_risk, raw_units_margin)

    # 最小取引単位刻みに切り下げ
    units = int(raw_units // min_unit) * min_unit

    if units < min_unit:
        reason = "リスク許容額" if raw_units_risk < raw_units_margin else "証拠金上限（既存ポジション込み）"
        return {
            "tradable": False,
            "units": 0,
            "risk_amount_jpy": round(risk_amount_jpy, 0),
            "margin_required_jpy": 0,
            "estimated_loss_jpy": 0,
            "existing_margin_jpy": round(existing_margin_jpy, 0),
            "note": (
                f"残高不足: 最小取引単位({min_unit}{from_ccy})でも{reason}を超過"
                f"（リスク許容¥{risk_amount_jpy:.0f} / 証拠金余力¥{available_margin_jpy:.0f}"
                f"[既存保有分¥{existing_margin_jpy:.0f}控除後]）"
            ),
        }

    margin_required_jpy = units * margin_per_unit_jpy
    estimated_loss_jpy = units * loss_per_unit_jpy
    exposure_note = (
        f" ※相関リスクでロット{exposure_multiplier:.0%}に圧縮"
        if exposure_multiplier < 1.0 else ""
    )

    return {
        "tradable": True,
        "units": units,
        "margin_required_jpy": round(margin_required_jpy, 0),
        "risk_amount_jpy": round(risk_amount_jpy, 0),
        "estimated_loss_jpy": round(estimated_loss_jpy, 0),
        "existing_margin_jpy": round(existing_margin_jpy, 0),
        "exposure_multiplier": exposure_multiplier,
        "note": (
            f"{units}{from_ccy}単位（証拠金約¥{margin_required_jpy:.0f}・"
            f"想定損失¥{estimated_loss_jpy:.0f}）{exposure_note}"
        ),
    }
