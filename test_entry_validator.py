#!/usr/bin/env python3
"""
test_entry_validator.py
エントリー有効性チェックモジュールの単体テスト（2026-06-25）

テスト項目:
  1. 基本判定 (ENTER / LIMIT / SKIP)
  2. max_entry_exec の数値正確性
  3. RR 計算
  4. validate_entry_for_result ヘルパー
  5. format_entry_block / format_entry_block_short テキスト生成
"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from modules.entry_validator import (
    validate_entry,
    validate_entry_for_result,
    format_entry_block,
    format_entry_block_short,
    MIN_RR_FLOOR,
)

PASS = "[PASS]"
FAIL = "[FAIL]"
results = []


def check(label, condition, detail=""):
    icon = PASS if condition else FAIL
    msg = f"  {icon} {label}"
    if detail:
        msg += f"  ({detail})"
    print(msg)
    results.append(condition)
    return condition


# ============================================================
# 1. ステータス判定テスト
# ============================================================
print("\n=== 1. ステータス判定 ===")

ev_gbpjpy = validate_entry(195.00, 193.80, 1.2, "GBPJPY", "LONG",
                            spread_pips=2.0, tp=196.50)
check("GBPJPY LONG (spread=2p, budget広) -> ENTER",
      ev_gbpjpy["status"] == "ENTER", f"status={ev_gbpjpy['status']}")

ev_usdjpy = validate_entry(155.50, 156.30, 0.6, "USDJPY", "SHORT",
                            spread_pips=0.3, tp=154.20)
check("USDJPY SHORT (spread=0.3p) -> ENTER",
      ev_usdjpy["status"] == "ENTER", f"status={ev_usdjpy['status']}")

ev_try = validate_entry(4.50, 4.20, 0.15, "TRYJPY", "LONG",
                         spread_pips=30.0, tp=4.90)
check("TRYJPY LONG (spread=30p / SL_mid=30p = 100%) -> SKIP",
      ev_try["status"] == "SKIP", f"status={ev_try['status']}")

ev_eur_tight = validate_entry(1.08500, 1.08300, 0.0050, "EURUSD", "LONG",
                               spread_pips=0.5, tp=1.08650)
check("EURUSD LONG (budget < 2*spread) -> LIMIT",
      ev_eur_tight["status"] == "LIMIT", f"status={ev_eur_tight['status']}")

ev_aud = validate_entry(0.65000, 0.65600, 0.0040, "AUDUSD", "SHORT",
                         spread_pips=1.0, tp=0.64200)
check("AUDUSD SHORT (normal) -> ENTER",
      ev_aud["status"] == "ENTER", f"status={ev_aud['status']}")

ev_zar = validate_entry(7.50, 7.30, 0.10, "ZARJPY", "LONG",
                         spread_pips=10.0, tp=7.80)
check("ZARJPY LONG (spread=10p / SL_mid=20p = 50%) -> SKIP",
      ev_zar["status"] == "SKIP", f"status={ev_zar['status']}")


# ============================================================
# 2. max_entry_exec の数値正確性
# ============================================================
print("\n=== 2. max_entry_exec の計算精度 ===")

# 手計算: max_exec = (tp + MIN_RR * sl) / (1 + MIN_RR)
# GBPJPY: (196.50 + 0.70*193.80) / 1.70 = (196.50+135.66)/1.70 = 195.388...
expected_max = round((196.50 + MIN_RR_FLOOR * 193.80) / (1 + MIN_RR_FLOOR), 3)
check(f"GBPJPY max_entry_exec == {expected_max}",
      abs(ev_gbpjpy["max_entry_exec"] - expected_max) < 0.001,
      f"got={ev_gbpjpy['max_entry_exec']}")

# USDJPY SHORT: max = (154.20 + 0.70*156.30) / 1.70 = (154.20+109.41)/1.70 = 155.065
expected_max_s = round((154.20 + MIN_RR_FLOOR * 156.30) / (1 + MIN_RR_FLOOR), 3)
check(f"USDJPY max_entry_exec == {expected_max_s}",
      abs(ev_usdjpy["max_entry_exec"] - expected_max_s) < 0.001,
      f"got={ev_usdjpy['max_entry_exec']}")


# ============================================================
# 3. RR 計算検証
# ============================================================
print("\n=== 3. RR 計算 ===")

# GBPJPY: signal_exec = 195.00 + 0.01 = 195.01
# rr_signal = (196.50 - 195.01) / (195.01 - 193.80) = 1.49/1.21 = 1.231...
check("GBPJPY rr_at_signal > 1.0",
      ev_gbpjpy["rr_at_signal"] is not None and ev_gbpjpy["rr_at_signal"] > 1.0,
      f"rr={ev_gbpjpy['rr_at_signal']}")
check("GBPJPY rr_at_max_entry == MIN_RR_FLOOR",
      ev_gbpjpy["rr_at_max_entry"] is not None and
      abs(ev_gbpjpy["rr_at_max_entry"] - MIN_RR_FLOOR) < 0.02,
      f"rr_max={ev_gbpjpy['rr_at_max_entry']} (floor={MIN_RR_FLOOR})")

# TP なし: rr は None になる
ev_no_tp = validate_entry(195.00, 193.80, 1.2, "GBPJPY", "LONG",
                           spread_pips=2.0, tp=None)
check("TP=None -> rr_at_signal is None",
      ev_no_tp["rr_at_signal"] is None)


# ============================================================
# 4. validate_entry_for_result ヘルパー
# ============================================================
print("\n=== 4. validate_entry_for_result ===")

# staged_tp あり
dummy_r = {
    "pair": "EURUSD", "price": 1.08500, "direction": "LONG", "atr": 0.0060,
    "staged_tp": {
        "sl": 1.07900, "tp": 1.09200, "tp1": 1.09200,
        "spread_pips_dynamic": 0.5, "spread_pips": 0.5,
    }
}
ev_r = validate_entry_for_result(dummy_r)
check("validate_entry_for_result returns dict with status",
      bool(ev_r) and "status" in ev_r, f"status={ev_r.get('status')}")
check("EURUSD result -> ENTER (SL広め)",
      ev_r["status"] == "ENTER", f"status={ev_r['status']}")

# staged_tp なし
dummy_no_staged = {"pair": "GBPJPY", "price": 195.0, "direction": "LONG", "atr": 1.2}
ev_empty = validate_entry_for_result(dummy_no_staged)
check("staged_tp なし -> {} を返す",
      ev_empty == {}, f"got={ev_empty}")


# ============================================================
# 5. テキスト生成（None/例外が出ないこと）
# ============================================================
print("\n=== 5. テキスト生成 ===")

block = format_entry_block(ev_gbpjpy, "GBPJPY")
check("format_entry_block: ENTER ブロック生成",
      bool(block) and "195.388" in block, detail=f"len={len(block)}")

block_skip = format_entry_block(ev_try, "TRYJPY")
check("format_entry_block: SKIP ブロック生成",
      bool(block_skip), detail=f"len={len(block_skip)}")

short = format_entry_block_short(ev_gbpjpy, "GBPJPY")
check("format_entry_block_short: 1行生成",
      bool(short) and len(short) < 300, detail=f"len={len(short)}")

short_skip = format_entry_block_short(ev_try, "TRYJPY")
check("format_entry_block_short: SKIP 1行",
      bool(short_skip), detail=short_skip[:60])

# 空 dict でエラーが出ないこと
check("format_entry_block({}): 空文字を返す",
      format_entry_block({}) == "")
check("format_entry_block_short({}): 空文字を返す",
      format_entry_block_short({}) == "")


# ============================================================
# 結果サマリー
# ============================================================
print("\n" + "=" * 55)
total = len(results)
passed = sum(results)
failed = total - passed
print(f"結果: {passed}/{total} PASS  |  {failed} FAIL")
if failed == 0:
    print("全テスト PASS - entry_validator OK")
else:
    print(f"{failed} 件のテストが FAIL しています。")
print("=" * 55)

sys.exit(0 if failed == 0 else 1)
