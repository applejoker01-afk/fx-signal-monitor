"""Deterministic, cost-aware M15 day-trading research engine.

This module deliberately produces paper-monitor candidates only.  A short Yahoo
history is useful for rejecting weak rules, but it is not sufficient evidence to
claim a durable trading edge.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from statistics import mean, pstdev
from typing import Any, Iterable


PIP_SIZE = {"JPY": 0.01, "DEFAULT": 0.0001}
# Conservative round-trip assumptions.  They are configuration defaults, not a
# substitute for the broker's observed bid/ask and slippage data.
ROUND_TRIP_COST_PIPS = {
    "USDJPY": 1.4, "EURUSD": 1.2, "GBPJPY": 2.6, "AUDJPY": 2.2,
    "EURJPY": 2.0, "AUDUSD": 1.5, "GBPUSD": 1.8, "USDCAD": 1.8,
    "USDCHF": 1.8, "NZDJPY": 2.4,
}


@dataclass(frozen=True)
class Signal:
    index: int
    direction: str
    entry: float
    stop: float
    target: float
    strategy_key: str
    session: str


def pip_size(pair: str) -> float:
    return PIP_SIZE["JPY"] if pair.endswith("JPY") else PIP_SIZE["DEFAULT"]


def ema(values: list[float], period: int) -> float | None:
    if len(values) < period:
        return None
    value = mean(values[:period])
    multiplier = 2 / (period + 1)
    for price in values[period:]:
        value = price * multiplier + value * (1 - multiplier)
    return value


def atr(bars: list[dict[str, Any]], period: int = 14) -> float | None:
    if len(bars) < period + 1:
        return None
    true_ranges = []
    for current, previous in zip(bars[1:], bars):
        true_ranges.append(max(
            current["high"] - current["low"],
            abs(current["high"] - previous["close"]),
            abs(current["low"] - previous["close"]),
        ))
    return mean(true_ranges[-period:])


def adx_proxy(bars: list[dict[str, Any]], period: int = 14) -> float | None:
    """A transparent directional-index proxy suitable for a regime gate."""
    if len(bars) < period + 1:
        return None
    plus_dm: list[float] = []
    minus_dm: list[float] = []
    tr: list[float] = []
    for current, previous in zip(bars[-period:], bars[-period - 1:-1]):
        up = current["high"] - previous["high"]
        down = previous["low"] - current["low"]
        plus_dm.append(up if up > down and up > 0 else 0.0)
        minus_dm.append(down if down > up and down > 0 else 0.0)
        tr.append(max(current["high"] - current["low"],
                      abs(current["high"] - previous["close"]),
                      abs(current["low"] - previous["close"])))
    denominator = sum(tr)
    if denominator == 0:
        return None
    plus_di = 100 * sum(plus_dm) / denominator
    minus_di = 100 * sum(minus_dm) / denominator
    return 100 * abs(plus_di - minus_di) / (plus_di + minus_di) if plus_di + minus_di else 0.0


def session_name(hour: int | None) -> str:
    if hour is None:
        return "unknown"
    if 0 <= hour < 7:
        return "tokyo"
    if 7 <= hour < 12:
        return "london"
    if 12 <= hour < 17:
        return "overlap"
    if 17 <= hour < 21:
        return "new_york"
    return "illiquid"


def _trend_breakout(bars: list[dict[str, Any]], index: int) -> Signal | None:
    window = bars[:index + 1]
    if len(window) < 130:
        return None
    closes = [bar["close"] for bar in window]
    fast = ema(closes, 20)
    slow = ema(closes, 60)
    prior_fast = ema(closes[:-4], 20)
    current_atr = atr(window)
    prior_atr = atr(window[:-4])
    if None in (fast, slow, prior_fast, current_atr, prior_atr):
        return None
    current = window[-1]
    range_high = max(bar["high"] for bar in window[-21:-1])
    range_low = min(bar["low"] for bar in window[-21:-1])
    expanded = current_atr >= prior_atr * 1.05
    long_ok = fast > slow and fast > prior_fast and current["close"] > range_high and expanded
    short_ok = fast < slow and fast < prior_fast and current["close"] < range_low and expanded
    if not (long_ok or short_ok):
        return None
    direction = "LONG" if long_ok else "SHORT"
    if direction == "LONG":
        stop = min(range_high, min(bar["low"] for bar in window[-4:])) - current_atr * 0.25
        risk = current["close"] - stop
        target = current["close"] + risk * 1.5
    else:
        stop = max(range_low, max(bar["high"] for bar in window[-4:])) + current_atr * 0.25
        risk = stop - current["close"]
        target = current["close"] - risk * 1.5
    if risk <= current_atr * 0.45:
        return None
    return Signal(index, direction, current["close"], stop, target, "trend_breakout",
                  session_name(current.get("hour")))


def _range_reversion(bars: list[dict[str, Any]], index: int) -> Signal | None:
    window = bars[:index + 1]
    if len(window) < 40:
        return None
    current = window[-1]
    current_atr = atr(window)
    strength = adx_proxy(window)
    closes = [bar["close"] for bar in window]
    basis = mean(closes[-21:-1])
    deviation = pstdev(closes[-21:-1])
    if None in (current_atr, strength) or deviation == 0 or strength >= 18:
        return None
    upper, lower = basis + 2 * deviation, basis - 2 * deviation
    if current["low"] <= lower and current["close"] > lower:
        stop = min(current["low"], lower) - current_atr * 0.25
        target = basis
        direction = "LONG"
    elif current["high"] >= upper and current["close"] < upper:
        stop = max(current["high"], upper) + current_atr * 0.25
        target = basis
        direction = "SHORT"
    else:
        return None
    risk = abs(current["close"] - stop)
    reward = abs(target - current["close"])
    if risk == 0 or reward / risk < 1.1:
        return None
    return Signal(index, direction, current["close"], stop, target, "range_reversion",
                  session_name(current.get("hour")))


DETECTORS = {"trend_breakout": _trend_breakout, "range_reversion": _range_reversion}


def detect_signals(bars: list[dict[str, Any]], strategy_key: str) -> list[Signal]:
    detector = DETECTORS[strategy_key]
    signals = []
    for index in range(130, len(bars) - 1):
        # 21:00-00:00 UTC is excluded across pairs.  This is a predeclared
        # liquidity rule, not an optimisation of the observed returns.
        if session_name(bars[index].get("hour")) == "illiquid":
            continue
        signal = detector(bars, index)
        if signal is not None:
            signals.append(signal)
    return signals


def simulate(bars: list[dict[str, Any]], signals: Iterable[Signal], pair: str,
             max_hold_bars: int = 16) -> list[dict[str, Any]]:
    """Fill at the next bar's open and apply half the round-trip cost on each side.

    When a single OHLC bar touches both stop and target, stop is used.  This is
    intentionally pessimistic because the intrabar path is unknowable here.
    """
    half_cost = ROUND_TRIP_COST_PIPS.get(pair, 2.0) * pip_size(pair) / 2
    results: list[dict[str, Any]] = []
    next_free_index = 0
    for signal in signals:
        entry_index = signal.index + 1
        if entry_index < next_free_index or entry_index >= len(bars):
            continue
        raw_entry = bars[entry_index]["open"]
        entry = raw_entry + half_cost if signal.direction == "LONG" else raw_entry - half_cost
        risk = abs(entry - signal.stop)
        if risk <= half_cost * 2:
            continue
        target = entry + risk * 1.5 if signal.strategy_key == "trend_breakout" and signal.direction == "LONG" else (
            entry - risk * 1.5 if signal.strategy_key == "trend_breakout" else signal.target)
        exit_index = min(entry_index + max_hold_bars, len(bars) - 1)
        outcome = "TIME"
        exit_price = bars[exit_index]["close"]
        for candidate_index in range(entry_index, exit_index + 1):
            candidate = bars[candidate_index]
            if signal.direction == "LONG":
                hit_stop, hit_target = candidate["low"] <= signal.stop, candidate["high"] >= target
                if hit_stop or hit_target:
                    outcome = "LOSS" if hit_stop else "WIN"
                    exit_price = signal.stop if hit_stop else target
                    exit_index = candidate_index
                    break
            else:
                hit_stop, hit_target = candidate["high"] >= signal.stop, candidate["low"] <= target
                if hit_stop or hit_target:
                    outcome = "LOSS" if hit_stop else "WIN"
                    exit_price = signal.stop if hit_stop else target
                    exit_index = candidate_index
                    break
        exit_price = exit_price - half_cost if signal.direction == "LONG" else exit_price + half_cost
        gross = (exit_price - entry) if signal.direction == "LONG" else (entry - exit_price)
        pips = gross / pip_size(pair)
        results.append({"strategy_key": signal.strategy_key, "session": signal.session,
                        "outcome": outcome, "pips": pips, "entry_index": entry_index,
                        "exit_index": exit_index})
        next_free_index = exit_index + 1
    return results


def summarize(results: list[dict[str, Any]]) -> dict[str, Any]:
    if not results:
        return {"trades": 0, "net_pf": 0.0, "net_expectancy_pips": 0.0,
                "win_rate": 0.0, "max_drawdown_pips": 0.0, "by_session": {}}
    wins = [row["pips"] for row in results if row["pips"] > 0]
    losses = [row["pips"] for row in results if row["pips"] <= 0]
    gross_loss = abs(sum(losses))
    profit_factor = sum(wins) / gross_loss if gross_loss else 99.0
    equity, peak, drawdown = 0.0, 0.0, 0.0
    by_session: dict[str, list[float]] = {}
    for row in results:
        equity += row["pips"]
        peak = max(peak, equity)
        drawdown = min(drawdown, equity - peak)
        by_session.setdefault(row["session"], []).append(row["pips"])
    return {
        "trades": len(results),
        "net_pf": round(profit_factor, 2),
        "net_expectancy_pips": round(mean(row["pips"] for row in results), 2),
        "win_rate": round(100 * len(wins) / len(results), 1),
        "max_drawdown_pips": round(drawdown, 1),
        "by_session": {name: {"trades": len(values), "expectancy_pips": round(mean(values), 2)}
                       for name, values in by_session.items()},
    }


def prepare_bars(price_data: dict[str, list[Any]]) -> list[dict[str, Any]]:
    bars = []
    timestamps = price_data.get("timestamps", [])
    for index, close in enumerate(price_data["closes"]):
        timestamp = timestamps[index] if index < len(timestamps) else None
        hour = datetime.fromtimestamp(timestamp, timezone.utc).hour if timestamp else None
        bars.append({"open": price_data.get("opens", price_data["closes"])[index],
                     "high": price_data["highs"][index], "low": price_data["lows"][index],
                     "close": close, "hour": hour, "timestamp": timestamp})
    return bars


def evaluate_pair(price_data: dict[str, list[Any]], pair: str) -> dict[str, Any]:
    bars = prepare_bars(price_data)
    midpoint = len(bars) * 2 // 3
    strategies: dict[str, dict[str, Any]] = {}
    for key in DETECTORS:
        signals = detect_signals(bars, key)
        all_results = simulate(bars, signals, pair)
        oos_results = [result for result in all_results if result["entry_index"] >= midpoint]
        metrics = summarize(all_results)
        oos_metrics = summarize(oos_results)
        # A candidate must survive costs in both the full and unseen final third.
        eligible = (metrics["trades"] >= 30 and oos_metrics["trades"] >= 10
                    and metrics["net_pf"] >= 1.05 and metrics["net_expectancy_pips"] > 0
                    and oos_metrics["net_expectancy_pips"] > 0)
        strategies[key] = {"metrics": metrics, "oos_metrics": oos_metrics,
                           "eligible": eligible}
    return {"engine_version": "daytrade-v2", "execution_mode": "paper_only",
            "cost_model": "next_open + configured round-trip cost; stop wins intrabar ties",
            "strategies": strategies}
