"""
drl_collector.py
DRL（深層強化学習）学習データ収集モジュール（2026-06-11 研究A反映）

fx-signal-monitor のシグナルスキャン結果から
強化学習エージェント訓練用の状態空間ベクトルを自動収集する。

収集データ:
  - TA/FA スコア（0-100）
  - ATR 比率（ボラティリティレジーム）
  - 金利差・モメンタム
  - Chandelier Exit 距離（過去22日高値/安値との相対位置）
  - 通貨強弱スコア
  - シグナル方向・星数（アクションラベル）

出力: data/drl_training_data.jsonl
  各行 = 1スキャン時点の1通貨ペアの状態ベクトル（JSON Lines形式）

将来の使い方:
  from modules.drl_collector import load_training_data
  df = load_training_data(days_back=180)
  # → PPO/A3C 訓練用 DataFrame

参照: wiki/concepts/deep-reinforcement-learning-fx.md
"""

import json
import os
from datetime import datetime, timezone, timedelta

DRL_DATA_FILE = "data/drl_training_data.jsonl"
MAX_ROWS = 100_000   # ファイルサイズ上限（行数）


def build_state_vector(result: dict, cb_rates: dict = None) -> dict:
    """
    1ペアのシグナル評価結果から状態空間ベクトルを構築。

    Args:
        result: signal_scanner.evaluate_full() の戻り値
        cb_rates: 中央銀行金利情報（momentum取得用）

    Returns:
        状態ベクトル dict（正規化済み）
    """
    pair = result.get("pair", "")
    price = result.get("price", 0) or 0
    ta_score = result.get("ta_score", 50) or 50
    fa_score = result.get("fa_score", 50) or 50
    atr = result.get("atr") or 0
    stars = result.get("stars", 0) or 0
    direction = result.get("direction", "NO_TRADE")
    regime = result.get("volatility_regime", {}) or {}
    staged = result.get("staged_tp", {}) or {}
    carry = result.get("carry_score", {}) or {}
    sr = result.get("support_resistance", {}) or {}
    strength = result.get("currency_strength", {}) or {}

    # --- 正規化された特徴量 ---
    # TA/FAスコア [0,1]
    ta_norm = ta_score / 100.0
    fa_norm = fa_score / 100.0

    # ATR比率（ボラティリティレジーム）
    atr_ratio = regime.get("atr_ratio", 1.0) or 1.0
    atr_ratio_norm = min(atr_ratio / 3.0, 1.0)  # 3倍で飽和

    # 金利差 [-1, 1] で正規化（±10%が実質範囲）
    rate_diff = result.get("fa_rate_diff") or 0
    rate_diff_norm = max(-1.0, min(1.0, rate_diff / 10.0))

    # 金利サイクルモメンタム数値化
    momentum_map = {
        "accelerating": 1.0, "peak": 0.5, "stable": 0.0,
        "trough": -0.5, "decelerating": -1.0,
    }
    if cb_rates and pair:
        # ペアの from/to 通貨のモメンタムを取得
        from_ccy = result.get("pair", "???")[:3]
        to_ccy = result.get("pair", "???")[3:]
        mom_from = cb_rates.get(from_ccy, {}).get("rate_momentum", "stable")
        mom_to = cb_rates.get(to_ccy, {}).get("rate_momentum", "stable")
        momentum_from_norm = momentum_map.get(mom_from, 0.0)
        momentum_to_norm = momentum_map.get(mom_to, 0.0)
    else:
        momentum_from_norm = 0.0
        momentum_to_norm = 0.0

    # Chandelier Exit 距離 （SL と価格の差 / ATR で正規化）
    sl = staged.get("sl", 0) or 0
    chandelier_active = staged.get("chandelier_sl_active", False)
    if atr > 0 and price > 0 and sl > 0:
        sl_distance_atr = abs(price - sl) / atr  # ATR 単位での SL 距離
        sl_distance_norm = min(sl_distance_atr / 5.0, 1.0)  # 5×ATRで飽和
    else:
        sl_distance_norm = 0.6  # デフォルト（3.0/5.0）

    # キャリートレードスコア [0, 1]
    carry_score = carry.get("carry_score", 50) or 50
    carry_norm = carry_score / 100.0

    # サポレジ近接度（近いほど 1.0）
    sr_resistance = sr.get("nearest_resistance")
    sr_support = sr.get("nearest_support")
    if sr_resistance and price > 0:
        resistance_dist_norm = 1.0 - min(abs(sr_resistance - price) / price, 0.05) / 0.05
    else:
        resistance_dist_norm = 0.0
    if sr_support and price > 0:
        support_dist_norm = 1.0 - min(abs(sr_support - price) / price, 0.05) / 0.05
    else:
        support_dist_norm = 0.0

    # アクション（DRLのラベル）
    # 0=HOLD/NO_TRADE, 1=LONG, 2=SHORT, 3=LIGHT_LONG, 4=LIGHT_SHORT
    action_map = {
        "LONG": 1, "SHORT": 2,
        "LIGHT_LONG": 3, "LIGHT_SHORT": 4,
        "NO_TRADE": 0, "WAIT_EVENT": 0,
    }
    action = action_map.get(direction, 0)

    return {
        # 識別子
        "pair": pair,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        # 正規化特徴量
        "ta_norm": round(ta_norm, 4),
        "fa_norm": round(fa_norm, 4),
        "atr_ratio_norm": round(atr_ratio_norm, 4),
        "rate_diff_norm": round(rate_diff_norm, 4),
        "momentum_from_norm": round(momentum_from_norm, 4),
        "momentum_to_norm": round(momentum_to_norm, 4),
        "sl_distance_norm": round(sl_distance_norm, 4),
        "carry_norm": round(carry_norm, 4),
        "resistance_dist_norm": round(resistance_dist_norm, 4),
        "support_dist_norm": round(support_dist_norm, 4),
        "chandelier_sl_active": int(chandelier_active),
        # 生値（検証用）
        "ta_score": round(ta_score, 1),
        "fa_score": round(fa_score, 1),
        "stars": stars,
        "direction": direction,
        "action": action,
        "price": round(price, 5) if price else 0,
        "atr": round(atr, 5) if atr else 0,
        "regime": regime.get("regime", "normal"),
    }


def save_state_vector(state: dict):
    """
    状態ベクトルを drl_training_data.jsonl に追記。
    100,000行を超えたら古い行から削除（ローリングバッファ）。
    """
    os.makedirs("data", exist_ok=True)
    try:
        with open(DRL_DATA_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(state, ensure_ascii=False) + "\n")
    except Exception as e:
        print(f"[WARN] DRL state save failed: {e}")

    # ファイルサイズ管理（100,000行超なら古い行を削除）
    _prune_drl_data()


def _prune_drl_data():
    """100,000行を超えたら古い行から削除する（先入れ先出し）"""
    if not os.path.exists(DRL_DATA_FILE):
        return
    try:
        with open(DRL_DATA_FILE, "r", encoding="utf-8") as f:
            lines = f.readlines()
        if len(lines) > MAX_ROWS:
            # 古い行を削除（後半 MAX_ROWS 行を保持）
            keep = lines[-MAX_ROWS:]
            with open(DRL_DATA_FILE, "w", encoding="utf-8") as f:
                f.writelines(keep)
    except Exception:
        pass


def collect_scan_results(results: list, cb_rates: dict = None):
    """
    スキャン結果の全ペアの状態ベクトルを一括保存。
    signal_scanner.py のメインループから呼ぶ。

    Args:
        results: evaluate_full() の戻り値リスト（全ペア分）
        cb_rates: 中央銀行金利情報

    Returns:
        保存した件数
    """
    saved = 0
    for r in results:
        # ★2以上のシグナルのみ収集（ノイズ削減）
        if r.get("stars", 0) >= 2:
            state = build_state_vector(r, cb_rates=cb_rates)
            save_state_vector(state)
            saved += 1
    return saved


def load_training_data(days_back: int = 180) -> list:
    """
    訓練データを読み込む。

    Args:
        days_back: 過去何日分を読み込むか（デフォルト180日）

    Returns:
        状態ベクトルのリスト（dicts）

    使い方:
        data = load_training_data(days_back=180)
        import pandas as pd
        df = pd.DataFrame(data)
        # → PPO/A3C の環境クラスの observation を構成
    """
    if not os.path.exists(DRL_DATA_FILE):
        return []

    cutoff = (datetime.now(timezone.utc) - timedelta(days=days_back)).isoformat()
    result = []
    try:
        with open(DRL_DATA_FILE, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                    if row.get("timestamp", "") >= cutoff:
                        result.append(row)
                except Exception:
                    continue
    except Exception as e:
        print(f"[WARN] DRL data load failed: {e}")
    return result


def get_drl_stats() -> dict:
    """
    収集データの統計サマリー。Discord通知や週次レポートに使用。

    Returns:
        {
          "total_rows": 1234,
          "days_collected": 45,
          "pairs": 22,
          "action_distribution": {0: 800, 1: 200, 2: 150, ...},
          "file_size_kb": 512,
        }
    """
    if not os.path.exists(DRL_DATA_FILE):
        return {"total_rows": 0, "days_collected": 0}

    rows = []
    try:
        with open(DRL_DATA_FILE, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        rows.append(json.loads(line))
                    except Exception:
                        pass
    except Exception:
        return {"total_rows": 0}

    if not rows:
        return {"total_rows": 0}

    timestamps = [r.get("timestamp", "") for r in rows if r.get("timestamp")]
    pairs = set(r.get("pair", "") for r in rows)
    action_dist = {}
    for r in rows:
        a = r.get("action", 0)
        action_dist[a] = action_dist.get(a, 0) + 1

    days = 0
    if timestamps:
        oldest = min(timestamps)
        try:
            dt = datetime.fromisoformat(oldest)
            days = (datetime.now(timezone.utc) - dt).days
        except Exception:
            pass

    file_size_kb = round(os.path.getsize(DRL_DATA_FILE) / 1024, 1)

    return {
        "total_rows": len(rows),
        "days_collected": days,
        "pairs": len(pairs),
        "action_distribution": action_dist,
        "file_size_kb": file_size_kb,
    }
