# Weekly experiment guardrails

This repository includes a scheduled Claude Code research workflow. Its purpose is
to generate reproducible research artifacts, not to modify the trading system.

## Hard boundaries

- Do not edit `signal_scanner.py`, `modules/`, `flow_layer/`, `data/`,
  `.github/`, `config.yaml`, position sizing, thresholds, or notification code.
- A scheduled experiment may edit **only** files under `research/`.
- Do not change live trading decisions, stars, entry/exit thresholds, risk
  percentages, or confidence multipliers.
- Do not merge pull requests, modify GitHub secrets, or change workflow
  permissions.

## Research standard

- Work on one registered hypothesis per run. Do not tune several variants and
  select the best result after the fact.
- Preserve the exact rules, data cutoff, costs, in-sample/out-of-sample split,
  and baseline in a dated result note.
- Compare returns in a common unit: R-multiples or JPY P&L. Never aggregate raw
  price differences across currency pairs.
- Include spread, commission, and slippage scenarios. Treat any data source
  timestamp limitation as a limitation, not as a passed test.
- A result is not eligible for production until it improves the baseline in
  cost-inclusive rolling out-of-sample tests and survives the stated
  multiple-testing adjustment.
