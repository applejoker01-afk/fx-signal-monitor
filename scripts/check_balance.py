"""
シミュレーション口座の残高・保有ポジション・証拠金維持率をその場で確認するCLIツール。

使い方:
    python scripts/check_balance.py          # ライブレート取得あり（含み損益・維持率まで表示）
    python scripts/check_balance.py --offline # ネットワーク接続なしで口座情報のみ表示
"""
import argparse
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from modules.position_sizing import (
    load_virtual_account, calc_maintenance_ratio, total_open_margin_jpy,
    unrealized_pnl_jpy,
)
from modules.trade_tracker import load_open_trades
from modules.pending_orders import load_pending_orders


def fmt_yen(v):
    return f"¥{v:,.0f}"


def main():
    parser = argparse.ArgumentParser(description="シミュレーション口座の状態を表示")
    parser.add_argument("--offline", action="store_true",
                         help="ライブレート取得をスキップ（含み損益・維持率は表示しない）")
    args = parser.parse_args()

    account = load_virtual_account()
    open_trades = load_open_trades()
    pending = load_pending_orders()

    print("=" * 50)
    print("💰 シミュレーション口座")
    print("=" * 50)
    print(f"現在残高:     {fmt_yen(account.get('current_balance', 0))}")
    print(f"初期資金:     {fmt_yen(account.get('starting_balance', 0))}")
    total_deposited = sum(d.get("amount", 0) for d in account.get("deposit_history", []))
    print(f"累計入金額:   {fmt_yen(total_deposited)}")
    net_pnl = account.get("current_balance", 0) - total_deposited
    print(f"通算損益:     {fmt_yen(net_pnl)}（{'+' if net_pnl >= 0 else ''}{net_pnl/total_deposited*100 if total_deposited else 0:.1f}%）")
    print(f"レバレッジ:   {account.get('leverage', 25)}倍")
    print(f"リスク許容度: {account.get('risk_pct_per_trade', 3.0)}%/トレード")
    print(f"証拠金使用上限: {account.get('max_margin_usage_pct', 30.0)}%（既存ポジション込み合計）")
    print(f"最終更新:     {account.get('last_updated', '?')}")

    # 2026-07-24: {pair: [trade,...]}のピラミッディング対応
    flat_trades = [
        (pair, t) for pair, trades in open_trades.items()
        for t in (trades if isinstance(trades, list) else [trades])
    ]
    print()
    print(f"📋 保有中ポジション: {len(flat_trades)}件")
    no_units_count = 0
    for pair, t in flat_trades:
        units = t.get("units")
        entry = t.get("entry_price", "?")
        direction = t.get("direction", "?")
        source = " [指値約定]" if t.get("entry_source") == "pending_limit_order" else ""
        seq = t.get("pyramid_seq")
        pair_label = f"{pair}#{seq}" if seq and seq > 1 else pair
        if units is None:
            no_units_count += 1
            print(f"  {pair_label} {direction} 単位数未記録(★) @ {entry}{source}")
        else:
            print(f"  {pair_label} {direction} {units}単位 @ {entry}{source}")
    if no_units_count:
        print(f"  (★ {no_units_count}件はポジションサイジング機能の導入前に建てたポジションのため、"
              f"証拠金維持率の計算には含まれません)")

    print()
    print(f"📌 指値待機中: {len(pending)}件")
    for pair, o in pending.items():
        print(f"  {pair} {o.get('direction','?')} 指値{o.get('limit_price','?')} "
              f"(有効期限 {o.get('valid_until','?')})")

    if args.offline or not open_trades:
        if not open_trades:
            print("\n(保有ポジションが無いため証拠金維持率は計算対象外)")
        return

    print()
    print("-" * 50)
    print("ライブレート取得中...")
    try:
        import signal_scanner as ss
        latest = ss.fetch_latest_rates()
        pairs = latest["pairs"]

        existing_margin = total_open_margin_jpy(open_trades, ss.PAIR_API, pairs)
        unreal = unrealized_pnl_jpy(open_trades, ss.PAIR_API, pairs)
        mr = calc_maintenance_ratio(account, open_trades, ss.PAIR_API, pairs)

        print()
        print("=" * 50)
        print("📊 現在の証拠金状況（ライブレート反映）")
        print("=" * 50)
        print(f"含み損益:     {fmt_yen(unreal)}")
        print(f"資産評価額:   {fmt_yen(account.get('current_balance', 0) + unreal)}")
        print(f"合計必要証拠金: {fmt_yen(existing_margin)}")
        ratio = mr.get("maintenance_ratio")
        if ratio is not None:
            alert = "🚨 危険水準" if mr.get("loss_cut_alert") else "✅ 安全域"
            print(f"証拠金維持率: {ratio:.0f}%  {alert}（ロスカット基準50%）")
        elif existing_margin == 0 and open_trades:
            print("(保有ポジションはあるが単位数未記録のため証拠金維持率は計算不可)")
    except Exception as e:
        print(f"[WARN] ライブレート取得失敗: {e}")
        print("(口座情報のみ表示。--offlineを付けるとこの試行自体をスキップできます)")


if __name__ == "__main__":
    main()
