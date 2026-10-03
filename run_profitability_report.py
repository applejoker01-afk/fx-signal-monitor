#!/usr/bin/env python3
"""Reproduce the profitability audit offline; never scan, notify or trade."""
import argparse
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

from modules.profitability import build_report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ledger", type=Path, default=Path("data/closed_trades.jsonl"))
    parser.add_argument("--signals", type=Path, default=Path("docs/last_signals.json"))
    parser.add_argument("--as-of", help="UTC cutoff; defaults to saved signal timestamp")
    parser.add_argument("--output", type=Path, default=Path("docs/profitability_report.json"))
    args = parser.parse_args()
    ledger_bytes = args.ledger.read_bytes()
    signals_bytes = args.signals.read_bytes()
    trades = [json.loads(line) for line in ledger_bytes.decode("utf-8").splitlines() if line.strip()]
    snapshot = json.loads(signals_bytes)
    cutoff = datetime.fromisoformat((args.as_of or snapshot["timestamp"]).replace("Z", "+00:00"))
    if cutoff.tzinfo is None:
        cutoff = cutoff.replace(tzinfo=timezone.utc)
    report = build_report(snapshot.get("results", []), trades, cutoff)
    report["input_sha256"] = {"ledger": hashlib.sha256(ledger_bytes).hexdigest(),
                              "signals": hashlib.sha256(signals_bytes).hexdigest()}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    stats = report["ledger"]
    print(f"valid={stats['valid_trades']}/{stats['input_trades']} excluded={stats['excluded_trades']}")
    print(f"standard mean R (estimated, ex swap)={stats['standard_net_r_estimate']['mean']}")
    print(f"walk-forward={report['walk_forward']['status']}; promotion=HOLD")
    print(args.output.resolve())


if __name__ == "__main__":
    main()
