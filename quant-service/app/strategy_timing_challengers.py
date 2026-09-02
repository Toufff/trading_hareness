"""Offline timing-challenger backtest over real recorded intraday rule inputs.

The only entry rule with real observed live-fire evidence is
``entry_setup`` in intraday_signal_rules.py (25 matured samples: -1.98%
average same-day close, -1.77% next close, 36%/24% hit rate - see the
strategy audit). Three real findings from those 25 samples motivate the
challenger variants tested here:

- Triggers with pct_change >= 3% at fire time hit 20%/-4.5%, vs ~50%/flat
  for 1-2%: chasing an already-large move looks like the loss driver.
- All 9:xx-hour triggers were positive (5/5, +2.9% avg); the 10:xx-hour
  triggers were 18/18 negative-tilted (-3.3% avg) - though this is a single
  trading day's worth of samples and must not be trusted without replay.
- Every one of the 25 samples fired through the "legacy" entry_setup path,
  which requires no minute-level (VWAP/volume) confirmation at all, unlike
  every other opt-in research setup in the same file.

This module never edits a live threshold: it calls the exact same
signal_rules() pure function used live, with the entry_* keyword overrides
that function now accepts (added for this purpose, defaulting to the live
values), against real intraday_rule_input_snapshots. It never fetches a
provider or historical price and never fits a threshold - every forward
return is reconstructed purely from the recorded snapshot stream for that
same symbol, matching intraday_rule_input_replay_runner.py's evidence
boundary (provider_access: none, historical_ingestion: none).
"""

from __future__ import annotations

import math
from datetime import date, datetime, timedelta
from statistics import mean, stdev
from typing import Any, Callable

from psycopg.types.json import Json

from .intraday_rule_inputs import intraday_rule_input_hash, intraday_rule_replay_inputs

CHALLENGERS: dict[str, dict[str, Any]] = {
    "baseline": {},
    "c1_tighter_entry_ceiling_3pct": {"entry_max_pct": 3.0},
    "c2_entry_session_windows": {"entry_session_windows": (("09:30", "10:00"), ("14:30", "15:00"))},
    "c3_entry_requires_minute_confirmation": {"entry_requires_minute_confirmation": True},
}

HORIZON_MINUTES = (5, 15, 30)
PRICE_MATCH_TOLERANCE = timedelta(minutes=3)
#: A challenger's cumulative (symbol, day) entry count, across every run
#: ledgered in quant.strategy_experiments, must reach this before its
#: results are anything but descriptive_only.
MINIMUM_EVALUABLE_ENTRIES = 30
BENJAMINI_HOCHBERG_Q = 0.05


def _two_sided_normal_p(t_stat: float | None) -> float | None:
    if t_stat is None or not math.isfinite(t_stat):
        return None
    return math.erfc(abs(t_stat) / math.sqrt(2.0))


def _one_sample_t_stat(values: list[float]) -> float | None:
    if len(values) < 2:
        return None
    spread = stdev(values)
    return (mean(values) / (spread / math.sqrt(len(values)))) if spread else None


def _benjamini_hochberg_reject(p_values: dict[str, float | None], *, q: float = BENJAMINI_HOCHBERG_Q) -> dict[str, bool]:
    valid = sorted((value, key) for key, value in p_values.items() if value is not None and math.isfinite(value))
    count = len(valid)
    reject = {key: False for key in p_values}
    threshold_rank = 0
    for rank, (value, _key) in enumerate(valid, start=1):
        if value <= (rank / count) * q:
            threshold_rank = rank
    for rank, (_value, key) in enumerate(valid, start=1):
        if rank <= threshold_rank:
            reject[key] = True
    return reject


def _symbols_for_date(connection: Any, as_of_date: date, model_version: str) -> list[str]:
    return [row["symbol"] for row in connection.execute(
        """SELECT DISTINCT symbol FROM quant.intraday_rule_input_snapshots
            WHERE (observed_at AT TIME ZONE 'Asia/Shanghai')::date=%s AND model_version=%s
            ORDER BY symbol""",
        (as_of_date, model_version),
    ).fetchall()]


def _load_symbol_snapshots(connection: Any, as_of_date: date, model_version: str, symbol: str, max_rows: int) -> list[dict[str, Any]]:
    return [dict(row) for row in connection.execute(
        """SELECT rule_input_snapshot_id,symbol,observed_at,model_version,input_hash,inputs
             FROM quant.intraday_rule_input_snapshots
            WHERE (observed_at AT TIME ZONE 'Asia/Shanghai')::date=%s AND model_version=%s AND symbol=%s
            ORDER BY observed_at LIMIT %s""",
        (as_of_date, model_version, symbol, max_rows),
    ).fetchall()]


def _price_path(rows: list[dict[str, Any]]) -> list[tuple[datetime, float]]:
    """Reconstruct one symbol's intraday price path purely from its own recorded snapshots."""
    path: list[tuple[datetime, float]] = []
    for row in rows:
        price = (row.get("inputs") or {}).get("quote", {})
        price = price.get("price") if isinstance(price, dict) else None
        if price is None:
            continue
        try:
            price_value = float(price)
        except (TypeError, ValueError):
            continue
        path.append((row["observed_at"], price_value))
    path.sort(key=lambda item: item[0])
    return path


def _forward_return(path: list[tuple[datetime, float]], entry_at: datetime, entry_price: float, minutes: int) -> float | None:
    """Nearest recorded snapshot at/after entry_at+minutes, within a bounded tolerance."""
    target = entry_at + timedelta(minutes=minutes)
    best: tuple[datetime, float] | None = None
    for observed_at, price in path:
        if observed_at < target:
            continue
        if best is None or observed_at < best[0]:
            best = (observed_at, price)
        if observed_at - target > PRICE_MATCH_TOLERANCE:
            break
    if best is None or best[0] - target > PRICE_MATCH_TOLERANCE or entry_price <= 0:
        return None
    return best[1] / entry_price - 1


def run_challenger_backtest(connection: Any, as_of_date: date, *, model_version: str,
                            evaluate_variant: Callable[[dict[str, Any], dict[str, Any]], list[dict[str, Any]]],
                            max_rows: int = 50_000) -> dict[str, Any]:
    """Replay one Shanghai trading day's recorded snapshots through every declared challenger.

    ``evaluate_variant(inputs, overrides)`` must call the real signal_rules()
    with ``overrides`` splatted as keyword arguments; injected by the caller
    (main.py) so this module stays free of the live number()/upside_assessment_fn
    bindings, matching how intraday_rule_input_replay_runner.py's evaluate is injected.

    Processes one symbol's rows at a time (the watchlist is capacity-bounded
    to ~40 symbols, so this is dozens of small queries rather than one huge
    fetch): loading a whole busy day's ~24k snapshots for every symbol at
    once caused an OOM kill on this VM's small fixed memory budget.
    """
    symbols = _symbols_for_date(connection, as_of_date, model_version)
    if not symbols:
        return {"status": "blocked", "as_of_date": str(as_of_date), "reason": "no recorded snapshots for this date/model_version"}
    per_horizon = {f"{minutes}m": {"returns": {}} for minutes in HORIZON_MINUTES}
    # One entry per (symbol, day) per challenger, not once per intraday signal
    # fire: a symbol that fires 5 times in one session is one independent
    # observation of "did this challenger's rule catch this name today", not 5.
    entered_symbols: dict[str, set[str]] = {key: set() for key in CHALLENGERS}
    total_snapshots = 0
    for symbol in symbols:
        rows = _load_symbol_snapshots(connection, as_of_date, model_version, symbol, max_rows)
        if not rows:
            continue
        total_snapshots += len(rows)
        path = _price_path(rows)
        adapted: list[tuple[datetime, dict[str, Any]]] = []
        for row in rows:
            payload = dict(row.get("inputs") or {})
            stored_hash = str(row.get("input_hash") or "")
            if not stored_hash or stored_hash != intraday_rule_input_hash(payload):
                continue
            try:
                inputs = intraday_rule_replay_inputs(payload, expected_model_version=model_version)
            except ValueError:
                continue
            if isinstance(inputs.get("quote"), dict):
                inputs["quote"] = {**inputs["quote"], "_scan_observed_at": row["observed_at"]}
            adapted.append((row["observed_at"], inputs))
        for challenger_key, overrides in CHALLENGERS.items():
            if symbol in entered_symbols[challenger_key]:
                continue
            for observed_at, inputs in adapted:
                if symbol in entered_symbols[challenger_key]:
                    break
                for signal in evaluate_variant(inputs, overrides):
                    if signal.get("signal_type") != "entry":
                        continue
                    price = (inputs.get("quote") or {}).get("price")
                    if price is None:
                        continue
                    entered_symbols[challenger_key].add(symbol)
                    for minutes in HORIZON_MINUTES:
                        key = f"{minutes}m"
                        forward = _forward_return(path, observed_at, float(price), minutes)
                        if forward is not None:
                            per_horizon[key]["returns"].setdefault(challenger_key, []).append(forward)
                    break
    total_entries = {key: len(symbols_seen) for key, symbols_seen in entered_symbols.items()}
    prior_entries = _cumulative_entries(connection)
    cumulative_entries = {key: prior_entries.get(key, 0) + total_entries[key] for key in CHALLENGERS}
    p_values: dict[str, float | None] = {}
    by_challenger_horizon: dict[str, dict[str, dict[str, Any]]] = {key: {} for key in CHALLENGERS}
    for challenger_key in CHALLENGERS:
        for minutes in HORIZON_MINUTES:
            key = f"{minutes}m"
            returns = per_horizon[key]["returns"].get(challenger_key, [])
            matured = len(returns)
            cell = {
                "entries_fired": total_entries[challenger_key], "matured": matured,
                "avg_return": round(mean(returns), 5) if returns else None,
                "hit_rate": round(sum(1 for value in returns if value > 0) / matured, 4) if matured else None,
            }
            t_stat = _one_sample_t_stat(returns)
            cell["t_stat"] = t_stat
            p_value = _two_sided_normal_p(t_stat)
            p_values[f"{challenger_key}:{key}"] = p_value
            by_challenger_horizon[challenger_key][key] = cell
    rejected = _benjamini_hochberg_reject(p_values)
    results: dict[str, Any] = {}
    for challenger_key in CHALLENGERS:
        by_horizon = by_challenger_horizon[challenger_key]
        for key, cell in by_horizon.items():
            cell["benjamini_hochberg_significant"] = bool(rejected.get(f"{challenger_key}:{key}"))
        enough_samples = cumulative_entries[challenger_key] >= MINIMUM_EVALUABLE_ENTRIES
        any_significant = any(cell["benjamini_hochberg_significant"] for cell in by_horizon.values())
        results[challenger_key] = {
            "total_entries": total_entries[challenger_key], "cumulative_entries": cumulative_entries[challenger_key],
            "by_horizon": by_horizon,
            "descriptive_only": not (enough_samples and any_significant),
            "gate_reason": None if enough_samples and any_significant else (
                f"cumulative (symbol,day) entries {cumulative_entries[challenger_key]} < {MINIMUM_EVALUABLE_ENTRIES}"
                if not enough_samples else "no horizon survives Benjamini-Hochberg correction across challengers and horizons"
            ),
        }
    status = "completed" if total_entries["baseline"] > 0 else "insufficient_history"
    metrics = {"challengers": results, "snapshots": total_snapshots, "symbols": len(symbols),
              "sample_gate": {"minimum_cumulative_entries": MINIMUM_EVALUABLE_ENTRIES, "benjamini_hochberg_q": BENJAMINI_HOCHBERG_Q,
                              "unit": "one entry per (symbol, day) per challenger, deduplicated across repeat intraday fires"},
              "descriptive_only": all(cell["descriptive_only"] for cell in results.values()),
              "data_boundary": {"source": "quant.intraday_rule_input_snapshots", "provider_access": "none",
                                "historical_ingestion": "none", "threshold_fitting": "none", "orders": "none",
                                "forward_return_source": "reconstructed from the same day's other recorded snapshots for that symbol only"}}
    connection.execute(
        """INSERT INTO quant.strategy_experiments(strategy_key,universe_key,start_date,end_date,status,parameters,metrics)
           VALUES('intraday_entry_timing_challengers_v1','explicit_watchlist',%s,%s,%s,%s,%s)""",
        (as_of_date, as_of_date, status, Json({"model_version": model_version, "challengers": list(CHALLENGERS)}), Json(metrics)),
    )
    return {"status": status, "as_of_date": str(as_of_date), **metrics}


def _cumulative_entries(connection: Any) -> dict[str, int]:
    """Sum every prior run's (symbol, day)-deduplicated entry counts per challenger.

    Reads back ``total_entries`` from every previously ledgered
    ``quant.strategy_experiments`` row for this strategy, so the 30-sample
    gate reflects accumulated evidence across days, not just today's run
    (which alone almost never reaches 30 distinct names).
    """
    rows = connection.execute(
        "SELECT metrics FROM quant.strategy_experiments WHERE strategy_key='intraday_entry_timing_challengers_v1'",
    ).fetchall()
    totals: dict[str, int] = {key: 0 for key in CHALLENGERS}
    for row in rows:
        metrics = row["metrics"] if isinstance(row, dict) else dict(row).get("metrics")
        if not isinstance(metrics, dict):
            continue
        for challenger_key, cell in (metrics.get("challengers") or {}).items():
            if challenger_key in totals and isinstance(cell, dict) and isinstance(cell.get("total_entries"), int):
                totals[challenger_key] += cell["total_entries"]
    return totals


__all__ = ["CHALLENGERS", "HORIZON_MINUTES", "run_challenger_backtest"]
