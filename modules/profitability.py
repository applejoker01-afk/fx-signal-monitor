"""Cost-aware paper-trade evaluation and portfolio risk budgets.

R always uses the original stop, never a moved/trailing stop. Fees are model
estimates, frozen at order creation. Broker swaps and fills remain unverified;
this module cannot authorize promotion of a strategy or place real orders.
"""

import json
import math
import statistics
from datetime import datetime, timezone
from pathlib import Path


CONFIG_FILE = Path(__file__).resolve().parents[1] / "profitability_config.json"


def load_config():
    with CONFIG_FILE.open(encoding="utf-8") as handle:
        config = json.load(handle)
    if config.get("selection_mode") != "shadow":
        raise ValueError("Profitability selection is shadow-only until independently validated")
    for key in ("slippage_pips_per_side", "commission_pips_round_trip",
                "adverse_spread_multiplier", "adverse_slippage_multiplier",
                "max_total_open_risk_pct", "max_currency_direction_risk_pct"):
        value = config[key]
        if not finite(value) or value < 0:
            raise ValueError(f"Invalid profitability configuration: {key}")
    for key in ("min_cohort_trades", "min_walk_forward_train_trades",
                "walk_forward_folds", "min_walk_forward_test_trades"):
        if not isinstance(config[key], int) or config[key] < 2:
            raise ValueError(f"Invalid profitability configuration: {key}")
    if min(config["adverse_spread_multiplier"], config["adverse_slippage_multiplier"]) < 1:
        raise ValueError("Adverse costs cannot be cheaper than standard costs")
    return config


def finite(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def side(direction):
    direction = direction or ""
    if not isinstance(direction, str):
        return 0
    if direction.endswith("LONG"):
        return 1
    if direction.endswith("SHORT"):
        return -1
    return 0


def pip_size(pair):
    return 0.01 if pair.upper().endswith("JPY") else 0.0001


def validate_geometry(direction, entry, stop, target=None, require_target=False):
    sign = side(direction)
    if not sign:
        return "売買方向が不正"
    if any(not finite(v) or v <= 0 for v in (entry, stop)):
        return "エントリー/初期SLが不正"
    if sign * (entry - stop) <= 0:
        return "初期SLがエントリーの損失側にない"
    if require_target and target is None:
        return "TPが欠損"
    if target is not None and (not finite(target) or target <= 0 or sign * (target - entry) <= 0):
        return "TPがエントリーの利益側にない"
    return None


def cost_snapshot(pair, spread_pips=None, now=None, config=None):
    config = config or load_config()
    if spread_pips is None:
        from modules.spread_monitor import get_dynamic_spread_pips
        spread_pips = get_dynamic_spread_pips(pair, utc_now=now)
    if not finite(spread_pips) or spread_pips < 0:
        raise ValueError("Invalid spread estimate")
    slip = config["slippage_pips_per_side"]
    fee = config["commission_pips_round_trip"]
    return {
        "basis": "model_estimate_excluding_swap",
        "captured_at": (now or datetime.now(timezone.utc)).isoformat(),
        "spread_pips": spread_pips,
        "slippage_pips_per_side": slip,
        "commission_pips_round_trip": fee,
        "standard_price_cost": (spread_pips + 2 * slip + fee) * pip_size(pair),
        "adverse_price_cost": (spread_pips * config["adverse_spread_multiplier"]
                               + 2 * slip * config["adverse_slippage_multiplier"] + fee) * pip_size(pair),
        "broker_costs_verified": config["broker_costs_verified"],
    }


def audit_trade(trade):
    errors = []
    if not isinstance(trade.get("pair"), str) or len(trade["pair"]) != 6:
        return ["通貨ペアが不正"]
    reason = ("初期SL欠損（移動SLから復元不可）" if trade.get("initial_sl") is None else
              validate_geometry(trade.get("direction"), trade.get("entry_price"), trade.get("initial_sl")))
    if reason:
        errors.append(reason)
    if not finite(trade.get("exit_price")) or trade["exit_price"] <= 0:
        errors.append("決済価格が不正")
    if errors:
        return errors
    gross = side(trade["direction"]) * (trade["exit_price"] - trade["entry_price"])
    tolerance = 0.00051 if trade["pair"].endswith("JPY") else 0.0000011
    if finite(trade.get("pips")) and abs(trade["pips"] - gross) > tolerance:
        errors.append("記録価格差と約定/決済価格が不一致")
    result = trade.get("result")
    if (result == "LOSS" and gross > tolerance) or (result == "WIN" and gross < -tolerance):
        errors.append("勝敗と価格差の符号が不一致")
    return errors


def partition_trades(trades):
    valid, rejected = [], []
    for trade in trades:
        errors = audit_trade(trade)
        if errors:
            rejected.append({"pair": trade.get("pair"), "entry_time": trade.get("entry_time"),
                             "errors": errors})
        else:
            valid.append(trade)
    return valid, rejected


def trade_metrics(trade, config=None):
    if audit_trade(trade):
        return None
    risk = abs(trade["entry_price"] - trade["initial_sl"])
    gross = side(trade["direction"]) * (trade["exit_price"] - trade["entry_price"])
    costs = trade.get("execution_costs")
    # Legacy rows lack a contemporaneous spread. Report scenarios, not measured fees.
    if costs is None:
        from modules.spread_monitor import SPREAD_PIPS_BASE
        costs = cost_snapshot(trade["pair"], SPREAD_PIPS_BASE.get(trade["pair"], 5.0), config=config)
    for key in ("standard_price_cost", "adverse_price_cost"):
        if not finite(costs.get(key)) or costs[key] < 0:
            raise ValueError("Invalid recorded execution costs")
    return {
        "gross_r": gross / risk,
        "standard_net_r_estimate": (gross - costs["standard_price_cost"]) / risk,
        "adverse_net_r_estimate": (gross - costs["adverse_price_cost"]) / risk,
        "standard_price_cost": costs["standard_price_cost"],
        "costs_frozen_at_entry": "execution_costs" in trade,
    }


def summarize(trades, config=None):
    valid, rejected = partition_trades(trades)
    metrics = [trade_metrics(t, config) for t in valid]
    result = {"input_trades": len(trades), "valid_trades": len(valid),
              "excluded_trades": len(rejected), "excluded": rejected,
              "missing_initial_sl": sum(t.get("initial_sl") is None for t in trades),
              "costs_frozen_at_entry": sum(m["costs_frozen_at_entry"] for m in metrics),
              "basis": "paper_model_estimate_excluding_swap"}
    for key in ("gross_r", "standard_net_r_estimate", "adverse_net_r_estimate"):
        values = [m[key] for m in metrics]
        gain = sum(max(0, x) for x in values)
        loss = -sum(min(0, x) for x in values)
        result[key] = {
            "mean": statistics.mean(values) if values else None,
            "sum": sum(values) if values else None,
            "profit_factor": gain / loss if loss else None,
            "win_rate_pct": 100 * sum(x > 0 for x in values) / len(values) if values else None,
        }
    return result


def _dt(value):
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except (AttributeError, TypeError, ValueError):
        return None


def walk_forward(trades, config=None):
    """Expanding, purged chronological evaluation of a pre-fixed cohort selector.

    Each test fold uses only trades settled strictly before its first entry.
    Cohorts are pair+direction; accept in SHADOW only when BOTH training cost
    scenarios have positive means and n>=min_cohort_trades. No thresholds fitted.
    These are legacy paper outcomes, not a comparison of new entry/exit rules.
    """
    config = config or load_config()
    valid, _ = partition_trades(trades)
    timed = [t for t in valid if _dt(t.get("entry_time")) and _dt(t.get("exit_time"))
             and _dt(t["exit_time"]) >= _dt(t["entry_time"])]
    timed.sort(key=lambda t: (_dt(t["entry_time"]), t["pair"]))
    start = config["min_walk_forward_train_trades"]
    folds_n = config["walk_forward_folds"]
    remaining = len(timed) - start
    if remaining < folds_n * config["min_walk_forward_test_trades"]:
        return {"status": "HOLD", "reason": "時系列OOSに必要な決済件数が不足", "folds": []}
    folds = []
    for i in range(folds_n):
        lo = start + remaining * i // folds_n
        hi = start + remaining * (i + 1) // folds_n
        boundary = _dt(timed[lo]["entry_time"])
        train = [t for t in timed[:lo] if _dt(t["exit_time"]) < boundary]
        # A tied entry timestamp cannot belong to both train and test.
        test = timed[lo:hi]
        groups = {}
        for trade in train:
            groups.setdefault((trade["pair"], side(trade["direction"])), []).append(trade)
        accepted = set()
        for group, rows in groups.items():
            if len(train) < config["min_walk_forward_train_trades"]:
                continue
            if len(rows) < config["min_cohort_trades"]:
                continue
            stats = summarize(rows, config)
            if all(stats[k]["mean"] > 0 for k in ("standard_net_r_estimate", "adverse_net_r_estimate")):
                accepted.add(group)
        selected = [t for t in test if (t["pair"], side(t["direction"])) in accepted]
        folds.append({"test_start": boundary.isoformat(), "train_trades": len(train),
                      "train_latest_exit": max((_dt(t["exit_time"]) for t in train), default=None).isoformat() if train else None,
                      "baseline": summarize(test, config), "shadow_selection": summarize(selected, config)})
    return {"status": "HOLD", "reason": "推定コスト・スワップ/約定未照合・独立再検証前。自動採用不可",
            "folds": folds, "selection_mode": "shadow", "promotion_eligible": False}


def build_report(results, closed_trades, now, config=None):
    config = config or load_config()
    # Future/missing settlement times must not leak into candidate rankings.
    historical = [t for t in closed_trades if _dt(t.get("exit_time")) and _dt(t["exit_time"]) < now]
    valid, _ = partition_trades(historical)
    candidates = []
    for result in results:
        if result.get("stars", 0) < 4 or not side(result.get("direction")):
            continue
        pair = result["pair"]
        staged = result.get("staged_tp") or {}
        geometry = validate_geometry(result["direction"], result.get("price"), staged.get("sl"), staged.get("tp"), require_target=True)
        cohort = [t for t in valid if t["pair"] == pair and side(t["direction"]) == side(result["direction"])]
        stats = summarize(cohort, config)
        risk = abs(result["price"] - staged["sl"]) if not geometry else None
        costs = cost_snapshot(pair, staged.get("spread_pips_dynamic", staged.get("spread_pips")), now, config)
        row = {"pair": pair, "direction": result["direction"], "stars": result["stars"],
               "cost_r_estimate": costs["standard_price_cost"] / risk if risk else None,
               "cohort_trades": len(cohort), "standard_mean_r": stats["standard_net_r_estimate"]["mean"],
               "adverse_mean_r": stats["adverse_net_r_estimate"]["mean"],
               "status": "INVALID_ORDER" if geometry else "HOLD",
               "reason": geometry or "推定コスト/OOS検証前。利益改善は未実証",
               "promotion_eligible": False}
        result["profitability"] = row.copy()
        candidates.append(row)
    candidates.sort(key=lambda row: (-row["stars"], row["cost_r_estimate"] if row["cost_r_estimate"] is not None else math.inf, row["pair"]))
    return {"generated_at": now.isoformat(), "version": config["version"],
            "selection_mode": "shadow", "promotion_eligible": False,
            "ledger": summarize(historical, config), "walk_forward": walk_forward(historical, config),
            "candidates": candidates,
            "limitations": ["ペーパー取引。実口座との照合未完了", "スプレッド/手数料/滑りは推定。スワップ未計上",
                            "旧台帳の初期SL欠損/不整合を除外。過去記録は上書きしない",
                            "新ルールの利益改善、多重検定調整、独立OOS検証は未確認"]}


def portfolio_budget(pair, direction, open_trades, pair_api, latest_pairs, balance, config=None):
    """Conservative stop-loss cash budget. Opposing positions never cancel risk.

    Currency-direction buckets prevent several JPY shorts from each consuming
    the full per-trade risk allowance. Profitable stops consume zero principal
    risk but are NOT treated as guaranteed cash available for another trade.
    """
    config = config or load_config()
    sign = side(direction)
    if not sign or not finite(balance) or balance <= 0 or pair not in pair_api:
        return {"available_jpy": 0.0, "reason": "リスク予算の入力が不正"}
    total = 0.0
    buckets = {}
    for existing_pair, items in open_trades.items():
        if isinstance(items, dict):
            items = [items]
        for trade in items:
            units, entry, stop = trade.get("units"), trade.get("entry_price"), trade.get("sl")
            existing_sign = side(trade.get("direction"))
            if existing_pair not in pair_api or not existing_sign or any(not finite(v) or v <= 0 for v in (units, entry, stop)):
                return {"available_jpy": 0.0, "reason": "既存ポジションの数量/SLが不明"}
            base, quote = pair_api[existing_pair]
            conversion = 1.0 if quote == "JPY" else latest_pairs.get(quote + "JPY")
            if not finite(conversion) or conversion <= 0:
                return {"available_jpy": 0.0, "reason": "既存ポジションの円換算レート不明"}
            cost = (trade.get("execution_costs") or {}).get("standard_price_cost", 0.0)
            if not finite(cost) or cost < 0:
                return {"available_jpy": 0.0, "reason": "既存ポジションのコスト不正"}
            risk = max(0.0, existing_sign * (entry - stop) + cost) * units * conversion
            total += risk
            for bucket in ((base, existing_sign), (quote, -existing_sign)):
                buckets[bucket] = buckets.get(bucket, 0.0) + risk
    total_cap = balance * config["max_total_open_risk_pct"] / 100
    currency_cap = balance * config["max_currency_direction_risk_pct"] / 100
    base, quote = pair_api[pair]
    available = min(total_cap - total, *(currency_cap - buckets.get(bucket, 0.0)
                    for bucket in ((base, sign), (quote, -sign))))
    return {"available_jpy": max(0.0, available), "existing_risk_jpy": total,
            "total_cap_jpy": total_cap, "currency_cap_jpy": currency_cap,
            "reason": "合計損切りリスク/通貨方向の集中リスク上限"}
