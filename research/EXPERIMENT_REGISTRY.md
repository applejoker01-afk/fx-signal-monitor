# Signal experiment registry

This is a pre-registration queue for the scheduled research workflow. A result
must be recorded here before another variant of the same idea is tested.

| ID | Hypothesis | Fixed candidate rules | Status | Required evidence before production review |
|---|---|---|---|---|
| EXP-001 | RSI reversal plus MACD confirmation improves entry quality | RSI recovers above 40 after dipping below 40 within 10 bars; MACD line crosses above signal for longs, inverse for shorts | queued | Cost-inclusive rolling OOS improvement versus current baseline; multiple-testing adjustment |
| EXP-002 | Bollinger squeeze followed by directional break identifies tradable expansion | 20-period, 2-sigma bandwidth below its trailing 20th percentile; close breaks the matching band with trend confirmation | queued | Same as EXP-001, evaluated separately by volatility regime |
| EXP-003 | 50/200 moving-average regime filter reduces trend-following false positives | Long only when 50DMA is above 200DMA and price is above both; inverse condition for shorts | queued | Same as EXP-001, with a no-filter baseline |
| EXP-004 | Bearish TA/FA agreement produces viable short signals | Mirror the existing long score thresholds only where technical and fundamental directions agree | queued | Long/short-separated, cost-inclusive rolling OOS improvement |
| EXP-005 | Fixed initial ATR stop improves cost-inclusive R versus initial Chandelier narrowing | [Protocol fixed before run](initial_stop_protocol_20261001.md); 10 fixed pairs, daily TA component test, 2022–2025 annual OOS; no tuning | rejected | [Result](results/2026-10-01-initial-stop-backtest.md): losses reduced but candidate negative in all four test years; full production replay still required |

## Status vocabulary

- `queued`: rules are written but no run has been recorded.
- `running`: one pre-registered configuration is under evaluation.
- `hold`: data, cost model, or sample size is insufficient.
- `rejected`: failed its pre-registered gate; do not retune without a new ID.
- `review`: passed research gates and may be considered manually for a separate implementation PR.
