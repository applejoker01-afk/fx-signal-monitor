"""
シミュレーション口座(data/virtual_account.json)へ入金を記録するCLIツール。

積み立てながら資金を注入していく運用のため、入金の都度これを実行して残高を反映する。

使い方:
    python scripts/add_deposit.py 10000
    python scripts/add_deposit.py 10000 --note "8月分積立"
"""
import argparse
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from modules.position_sizing import add_deposit, load_virtual_account


def main():
    parser = argparse.ArgumentParser(description="シミュレーション口座への入金を記録")
    parser.add_argument("amount", type=float, help="入金額（円）")
    parser.add_argument("--note", type=str, default="", help="メモ（任意）")
    args = parser.parse_args()

    before = load_virtual_account()
    print(f"入金前残高: ¥{before.get('current_balance', 0):,.0f}")

    after = add_deposit(args.amount, args.note)
    print(f"入金額: ¥{args.amount:,.0f}")
    print(f"入金後残高: ¥{after['current_balance']:,.0f}")


if __name__ == "__main__":
    main()
