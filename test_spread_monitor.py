#!/usr/bin/env python3
"""
test_spread_monitor.py
動的スプレッドモニタリングの単体テスト

テスト項目:
  1. spread_monitor モジュールのセッション/VIX 補正ロジック
  2. calc_staged_tp の spread_pips パラメータ統合
  3. run_advanced_analytics の動的スプレッド注入
  4. 時刻・VIX による spread_atr_ratio / rr_effective の変化確認
"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from datetime import datetime, timezone
from modules.spread_monitor import (
    get_session_multiplier,
    get_vix_multiplier,
    get_dynamic_spread_pips,
    get_dynamic_spread_metadata,
    SPREAD_PIPS_BASE,
)
from modules.advanced_analytics import calc_staged_tp, detect_volatility_regime

PASS = "[PASS]"
FAIL = "[FAIL]"
results = []


def check(label: str, condition: bool, detail: str = ""):
    icon = PASS if condition else FAIL
    msg = f"  {icon} {label}"
    if detail:
        msg += f"  ({detail})"
    print(msg)
    results.append(condition)
    return condition


# ============================================================
# 1. セッション乗数テスト
# ============================================================
print("\n=== 1. セッション乗数 ===")

# London/NY overlap は最タイト
m = get_session_multiplier(14, "GBPJPY")
check("London/NY overlap(14UTC) GBPJPY <= 0.85", m <= 0.85, f"mult={m}")

# Asian/Tokyo: JPYペアはタイト、非JPYはワイド
m_jpy = get_session_multiplier(3, "USDJPY")
m_eur = get_session_multiplier(3, "EURUSD")
check("Asian(3UTC) JPY < Non-JPY", m_jpy < m_eur, f"USDJPY={m_jpy}, EURUSD={m_eur}")
check("Asian(3UTC) USDJPY <= 1.0 (Tokyo tight)", m_jpy <= 1.0, f"mult={m_jpy}")
check("Asian(3UTC) EURUSD >= 1.3 (non-JPY wide)", m_eur >= 1.3, f"mult={m_eur}")

# Off-hours はワイド
m_off = get_session_multiplier(22, "GBPUSD")
check("Off-hours(22UTC) GBPUSD >= 1.5", m_off >= 1.5, f"mult={m_off}")

# London open はタイト
m_lon = get_session_multiplier(9, "EURUSD")
check("London(9UTC) EURUSD <= 0.90", m_lon <= 0.90, f"mult={m_lon}")


# ============================================================
# 2. VIX 乗数テスト
# ============================================================
print("\n=== 2. VIX 乗数 ===")

check("VIX=None → 1.0",       get_vix_multiplier(None) == 1.0)
check("VIX=12 (超低) < 1.0",  get_vix_multiplier(12) < 1.0, f"mult={get_vix_multiplier(12)}")
check("VIX=18 (平常) = 1.0",  get_vix_multiplier(18) == 1.0, f"mult={get_vix_multiplier(18)}")
check("VIX=22 > 1.0",         get_vix_multiplier(22) > 1.0, f"mult={get_vix_multiplier(22)}")
check("VIX=27 >= 1.5",        get_vix_multiplier(27) >= 1.5, f"mult={get_vix_multiplier(27)}")
check("VIX=35 (panic) = 2.0", get_vix_multiplier(35) == 2.0, f"mult={get_vix_multiplier(35)}")


# ============================================================
# 3. 動的スプレッド計算テスト
# ============================================================
print("\n=== 3. 動的スプレッド (pips) ===")

# London/NY overlap + 低VIX → 最タイト
dt_tight = datetime(2026, 6, 25, 14, 30, tzinfo=timezone.utc)  # 14UTC
pips_tight = get_dynamic_spread_pips("GBPJPY", vix=14, utc_now=dt_tight)
check("GBPJPY London/NY low-VIX < base(2.0)", pips_tight < 2.0, f"pips={pips_tight}")

# Asian + パニックVIX → 最ワイド
dt_asian = datetime(2026, 6, 25, 3, 0, tzinfo=timezone.utc)  # 3UTC
pips_wide = get_dynamic_spread_pips("EURUSD", vix=35, utc_now=dt_asian)
base_eur = SPREAD_PIPS_BASE["EURUSD"]
check("EURUSD Asian panic-VIX >> base", pips_wide > base_eur * 2, f"pips={pips_wide}, base={base_eur}")

# JPY ペアは Asian でタイト、非 JPY は Asian でワイド
pips_jpy_asia = get_dynamic_spread_pips("USDJPY", vix=18, utc_now=dt_asian)
pips_eur_asia = get_dynamic_spread_pips("EURUSD", vix=18, utc_now=dt_asian)
base_usd = SPREAD_PIPS_BASE["USDJPY"]
base_eur = SPREAD_PIPS_BASE["EURUSD"]
ratio_jpy = pips_jpy_asia / base_usd
ratio_eur = pips_eur_asia / base_eur
check("Asian session: JPY ratio < EUR ratio", ratio_jpy < ratio_eur,
      f"JPY ratio={ratio_jpy:.2f}, EUR ratio={ratio_eur:.2f}")


# ============================================================
# 4. メタデータテスト
# ============================================================
print("\n=== 4. spread_metadata ===")

meta = get_dynamic_spread_metadata("GBPJPY", vix=28.0, utc_now=dt_asian)
check("metadata pair == GBPJPY", meta["pair"] == "GBPJPY")
check("metadata has session_label", bool(meta.get("session_label")))
check("metadata vix_mult >= 1.5 (VIX=28)", meta["vix_mult"] >= 1.5, f"mult={meta['vix_mult']}")
check("metadata dynamic_pips > base_pips (Asian+high VIX)",
      meta["dynamic_pips"] > meta["base_pips"],
      f"dynamic={meta['dynamic_pips']}, base={meta['base_pips']}")
print(f"    GBPJPY Asian VIX28: {meta}")


# ============================================================
# 5. calc_staged_tp の spread_pips パラメータ統合テスト
# ============================================================
print("\n=== 5. calc_staged_tp 統合 ===")

price = 195.0
atr   = 1.2
prices_dummy = [195.0 + i * 0.05 for i in range(30)]
regime = detect_volatility_regime(prices_dummy, atr)

# 静的スプレッド（パラメータなし）
staged_static = calc_staged_tp(price, "LONG", atr, regime, prices=prices_dummy, pair="GBPJPY")

# 動的スプレッド: London/NY low-VIX（タイト）
tight_pips = get_dynamic_spread_pips("GBPJPY", vix=14, utc_now=dt_tight)
staged_tight = calc_staged_tp(price, "LONG", atr, regime, prices=prices_dummy,
                               pair="GBPJPY", spread_pips=tight_pips)

# 動的スプレッド: Asian panic-VIX（ワイド）
wide_pips = get_dynamic_spread_pips("GBPJPY", vix=35, utc_now=dt_asian)
staged_wide = calc_staged_tp(price, "LONG", atr, regime, prices=prices_dummy,
                              pair="GBPJPY", spread_pips=wide_pips)

check("staged_tight spread_pips_dynamic == tight_pips",
      staged_tight.get("spread_pips_dynamic") == tight_pips,
      f"expected={tight_pips}, got={staged_tight.get('spread_pips_dynamic')}")
check("staged_wide spread_pips_dynamic == wide_pips",
      staged_wide.get("spread_pips_dynamic") == wide_pips,
      f"expected={wide_pips}, got={staged_wide.get('spread_pips_dynamic')}")
check("spread_is_dynamic=True when pips provided",
      staged_tight.get("spread_is_dynamic") is True)
check("spread_is_dynamic=False for static",
      staged_static.get("spread_is_dynamic") is False)

# Wide spread → spread_atr_ratio 大 → rr_effective 低
ratio_tight = staged_tight.get("spread_atr_ratio", 0)
ratio_wide  = staged_wide.get("spread_atr_ratio", 0)
rr_tight    = staged_tight.get("rr_effective", 0)
rr_wide     = staged_wide.get("rr_effective", 0)
check("spread_atr_ratio: tight < wide", ratio_tight < ratio_wide,
      f"tight={ratio_tight:.3f}, wide={ratio_wide:.3f}")
check("rr_effective: tight > wide", rr_tight > rr_wide,
      f"tight={rr_tight}, wide={rr_wide}")

print(f"\n  スプレッド比較 (GBPJPY ATR={atr}):")
print(f"    静的      : spread={staged_static.get('spread_pips'):.1f}pips, "
      f"ratio={staged_static.get('spread_atr_ratio'):.3f}, rr={staged_static.get('rr_effective')}")
print(f"    動的タイト: spread={staged_tight.get('spread_pips'):.1f}pips, "
      f"ratio={ratio_tight:.3f}, rr={rr_tight}  [session={tight_pips}pips]")
print(f"    動的ワイド: spread={staged_wide.get('spread_pips'):.1f}pips, "
      f"ratio={ratio_wide:.3f}, rr={rr_wide}  [session={wide_pips}pips]")


# ============================================================
# 6. セッション対比テーブル（全主要ペア・主要セッション）
# ============================================================
print("\n=== 6. セッション別スプレッド対比テーブル ===")
print(f"{'ペア':10} {'Base':>6} {'London':>8} {'LN/NY':>7} {'Asian':>7} {'Off':>7} {'VIX35+':>9}")
print("-" * 65)
pairs_to_show = ["USDJPY", "GBPJPY", "AUDJPY", "EURUSD", "GBPUSD", "EURAUD"]
for p in pairs_to_show:
    base = SPREAD_PIPS_BASE.get(p, 3.0)
    lon  = get_dynamic_spread_pips(p, vix=18, utc_now=datetime(2026,1,1,9,0,tzinfo=timezone.utc))
    ov   = get_dynamic_spread_pips(p, vix=18, utc_now=datetime(2026,1,1,14,0,tzinfo=timezone.utc))
    asi  = get_dynamic_spread_pips(p, vix=18, utc_now=datetime(2026,1,1,3,0,tzinfo=timezone.utc))
    off  = get_dynamic_spread_pips(p, vix=18, utc_now=datetime(2026,1,1,22,0,tzinfo=timezone.utc))
    panic= get_dynamic_spread_pips(p, vix=35, utc_now=datetime(2026,1,1,3,0,tzinfo=timezone.utc))
    print(f"{p:10} {base:>6.1f} {lon:>8.2f} {ov:>7.2f} {asi:>7.2f} {off:>7.2f} {panic:>9.2f}")
print("  (VIX=18 を基準、右端は Asian+VIX35 パニックシナリオ)")


# ============================================================
# 結果サマリー
# ============================================================
print("\n" + "=" * 55)
total = len(results)
passed = sum(results)
failed = total - passed
print(f"結果: {passed}/{total} PASS  |  {failed} FAIL")
if failed == 0:
    print("全テスト PASS - 動的スプレッド統合OK")
else:
    print(f"{failed} 件のテストが FAIL しています。上記のエラーを確認してください。")
print("=" * 55)

sys.exit(0 if failed == 0 else 1)
