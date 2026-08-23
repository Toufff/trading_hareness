"""Local-only factor experiment, backtest record and data-snapshot writes.

These operations consume the existing canonical database only.  They do not
fetch market providers, start historical ingestion, adjust strategy thresholds
or promote analyst weights.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable


@dataclass(frozen=True)
class ResearchExperimentDependencies:
    database: Any
    china_today: Callable[[], date]
    as_utc: Callable[[datetime], datetime]
    http_exception: type[Exception]
    evaluate_factor_set: Callable[..., list[dict[str, Any]]]
    run_multi_factor_strategy: Callable[..., dict[str, Any]]
    json_value: Callable[[Any], Any]


def research_window(
    connection: Any,
    universe_key: str,
    start_date: date | None,
    end_date: date | None,
    *,
    http_exception: type[Exception],
) -> tuple[date, date]:
    """Bound an experiment to persisted point-in-time universe membership."""
    bounds = connection.execute(
        """SELECT min(b.trading_date) earliest,max(b.trading_date) latest FROM quant.canonical_bars_daily b
           JOIN quant.universe_membership_history membership ON membership.symbol=b.symbol
            AND membership.universe_key=%s AND membership.effective_from<=b.trading_date
            AND (membership.effective_to IS NULL OR membership.effective_to>=b.trading_date)""",
        (universe_key,),
    ).fetchone()
    if not bounds or not bounds["latest"]:
        raise http_exception(status_code=422, detail="universe has no canonical daily bars")
    end = min(end_date or bounds["latest"], bounds["latest"])
    start = start_date or max(bounds["earliest"], end - timedelta(days=730))
    if start >= end:
        raise http_exception(status_code=422, detail="research window must contain at least two dates")
    return start, end


def evaluate_factors(payload: Any, deps: ResearchExperimentDependencies) -> dict[str, Any]:
    with deps.database.transaction() as connection:
        start, end = research_window(
            connection, payload.universe_key, payload.start_date, payload.end_date,
            http_exception=deps.http_exception,
        )
        rows = connection.execute(
            "SELECT factor_key FROM quant.factor_registry WHERE implementation='native_sql' AND status<>'disabled' ORDER BY factor_key"
        ).fetchall()
        enabled = {str(row["factor_key"]) for row in rows}
        requested = payload.factor_keys or sorted(enabled)
        unknown = sorted(set(requested) - enabled)
        if unknown:
            raise deps.http_exception(status_code=422, detail=f"unknown or disabled factors: {', '.join(unknown)}")
        try:
            evaluated = deps.evaluate_factor_set(connection, requested, payload.universe_key, start, end, payload.horizon_days)
        except ValueError as error:
            raise deps.http_exception(status_code=422, detail=str(error)) from error
        results = []
        for result in evaluated:
            factor_key = str(result["factor_key"])
            row = connection.execute(
                """INSERT INTO quant.factor_evaluations(factor_key,universe_key,start_date,end_date,horizon_days,engine,status,observations,
                    cross_section_days,metrics,artifact) VALUES(%s,%s,%s,%s,%s,'native_factor_sql_v2',%s,%s,%s,%s,%s) RETURNING evaluation_id""",
                (factor_key, payload.universe_key, start, end, payload.horizon_days, result["status"], result["observations"],
                 result["cross_section_days"], deps.json_value(result["metrics"]), deps.json_value(result["artifact"])),
            ).fetchone()
            result["evaluation_id"] = str(row["evaluation_id"])
            results.append(result)
    return {"universe_key": payload.universe_key, "start_date": str(start), "end_date": str(end), "results": results}


def backtest_strategy(payload: Any, deps: ResearchExperimentDependencies) -> dict[str, Any]:
    with deps.database.transaction() as connection:
        registry = connection.execute(
            "SELECT strategy_key,configuration FROM quant.strategy_registry WHERE strategy_key=%s AND status<>'disabled'",
            (payload.strategy_key,),
        ).fetchone()
        if not registry:
            raise deps.http_exception(status_code=404, detail="strategy is not available")
        start, end = research_window(
            connection, payload.universe_key, payload.start_date, payload.end_date,
            http_exception=deps.http_exception,
        )
        parameters = {
            **dict(registry["configuration"]), "rebalance_days": payload.rebalance_days,
            "hold_days": payload.hold_days, "top_n": payload.top_n,
            "total_cost_bps": payload.total_cost_bps, "factors": payload.factors,
        }
        try:
            result = deps.run_multi_factor_strategy(connection, payload.universe_key, start, end, parameters)
        except ValueError as error:
            raise deps.http_exception(status_code=422, detail=str(error)) from error
        row = connection.execute(
            """INSERT INTO quant.strategy_experiments(strategy_key,universe_key,start_date,end_date,status,parameters,metrics,equity_curve,trades)
               VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING strategy_experiment_id""",
            (payload.strategy_key, payload.universe_key, start, end, result["status"], deps.json_value(result["parameters"]),
             deps.json_value(result["metrics"]), deps.json_value(result["equity_curve"]), deps.json_value(result["trades"])),
        ).fetchone()
    result["strategy_experiment_id"] = str(row["strategy_experiment_id"])
    return result


def build_snapshot(payload: Any, deps: ResearchExperimentDependencies) -> dict[str, Any]:
    """Persist a data-quality manifest; a blocked manifest is never "ready"."""
    as_of = payload.as_of_date or deps.china_today()
    cutoff = deps.as_utc(payload.knowledge_cutoff) if payload.knowledge_cutoff else datetime.now(timezone.utc)
    with deps.database.transaction() as connection:
        manifest = connection.execute(
            """SELECT (SELECT count(*)::int FROM quant.canonical_bars_daily WHERE trading_date<=%s) bars,
                      (SELECT count(*)::int FROM quant.remote_reports WHERE remote_updated_at<=%s) remote_reports,
                      (SELECT count(*)::int FROM quant.canonical_bars_daily WHERE symbol='000300.SH' AND trading_date<=%s) benchmark_bars,
                      (SELECT count(DISTINCT symbol)::int FROM quant.canonical_bars_daily
                        WHERE trading_date=%s
                          AND symbol ~ '^(?:(?:60[0135]|68[0-9])[0-9]{3}\\.SH|(?:000|001|002|003|300|301|302)[0-9]{3}\\.SZ|[489][0-9]{5}\\.BJ)$') equity_symbols,
                      (SELECT count(DISTINCT basic.symbol)::int
                         FROM quant.canonical_bars_daily bar
                         JOIN quant.daily_fundamentals basic
                           ON basic.symbol=bar.symbol AND basic.trading_date=bar.trading_date
                        WHERE bar.trading_date=%s
                          AND bar.symbol ~ '^(?:(?:60[0135]|68[0-9])[0-9]{3}\\.SH|(?:000|001|002|003|300|301|302)[0-9]{3}\\.SZ|[489][0-9]{5}\\.BJ)$') fundamental_symbols,
                      (SELECT count(DISTINCT limits.symbol)::int
                         FROM quant.canonical_bars_daily bar
                         JOIN quant.daily_trade_limits limits
                           ON limits.symbol=bar.symbol AND limits.trading_date=bar.trading_date
                        WHERE bar.trading_date=%s
                          AND bar.symbol ~ '^(?:(?:60[0135]|68[0-9])[0-9]{3}\\.SH|(?:000|001|002|003|300|301|302)[0-9]{3}\\.SZ|[489][0-9]{5}\\.BJ)$') limit_symbols,
                      (SELECT is_open FROM quant.market_trade_calendar WHERE exchange='SSE' AND calendar_date=%s) exchange_open,
                      (SELECT count(*)::int FROM quant.data_quality_issues WHERE resolved_at IS NULL AND severity IN ('error','blocking')) blocking_issues""",
            (as_of, cutoff, as_of, as_of, as_of, as_of, as_of),
        ).fetchone()
        complete_equity_controls = (
            manifest["equity_symbols"] > 0
            and manifest["fundamental_symbols"] >= manifest["equity_symbols"]
            and manifest["limit_symbols"] >= manifest["equity_symbols"]
        )
        status = "ready" if not manifest["blocking_issues"] and manifest["benchmark_bars"] and manifest["exchange_open"] and complete_equity_controls else "blocked"
        snapshot_key = hashlib.sha256(f"{as_of}:{cutoff.isoformat()}:{manifest['bars']}:{manifest['remote_reports']}".encode()).hexdigest()
        connection.execute(
            """INSERT INTO quant.data_snapshots(snapshot_key,as_of_date,knowledge_cutoff,status,manifest,content_sha256,finalized_at)
               VALUES(%s,%s,%s,%s,%s,%s,CASE WHEN %s='ready' THEN now() ELSE null END)
               ON CONFLICT(snapshot_key) DO NOTHING""",
            (snapshot_key, as_of, cutoff, status, deps.json_value(manifest), snapshot_key, status),
        )
    return {"snapshot_key": snapshot_key, "as_of_date": str(as_of), "knowledge_cutoff": cutoff, "status": status, "manifest": manifest}


__all__ = ["ResearchExperimentDependencies", "backtest_strategy", "build_snapshot", "evaluate_factors", "research_window"]
