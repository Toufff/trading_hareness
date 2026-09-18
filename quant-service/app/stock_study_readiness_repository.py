"""Bounded local evidence readiness for one on-demand stock study window."""

from __future__ import annotations

from datetime import date
from typing import Any

from .adjustment_factor_maintenance import (
    REAL_FACTOR_PREDICATE_SQL,
    pending_and_retired_dates_between,
)


#: Symbol-scoped adjustment coverage for one study window.
#:
#: The count deliberately excludes placeholders (it asks for a REAL cumulative
#: factor, i.e. a tushare row whose declared semantics are absent or
#: cumulative), because the repair runbook only ANNOTATES the placeholder rows
#: and never deletes them: a bare ``count(*) > 0`` would report ``ready``
#: forever on exactly the symbols and dates that are still damaged.
#:
#: ``uncovered_dates`` is the settled exchange sessions of THIS symbol that have
#: no such factor, which is what turns the market-wide maintenance work list
#: into a per-symbol verdict.
ADJUSTMENT_WINDOW_SQL = f"""WITH settled AS (
       SELECT bar.trading_date FROM quant.canonical_bars_daily bar
        WHERE bar.symbol=%s AND bar.trading_date BETWEEN %s AND %s
          AND bar.quality_status IN ('fresh','partial')
          AND EXISTS (SELECT 1 FROM quant.market_trade_calendar calendar
                       WHERE calendar.calendar_date=bar.trading_date AND calendar.is_open)
   ), factored AS (
       SELECT DISTINCT factor.trading_date
         FROM quant.daily_adjustment_factors factor
        WHERE factor.symbol=%s AND factor.trading_date BETWEEN %s AND %s
          AND {REAL_FACTOR_PREDICATE_SQL}
   ) SELECT (SELECT count(*) FROM factored)::int AS rows,
            (SELECT max(trading_date) FROM factored) AS latest_date,
            (SELECT count(*) FROM settled)::int AS settled_sessions,
            coalesce((SELECT array_agg(trading_date ORDER BY trading_date) FROM settled
                       WHERE trading_date NOT IN (SELECT trading_date FROM factored)),
                     ARRAY[]::date[]) AS uncovered_dates"""


_SPECS = (
    ("daily", "日线行情", "P0"),
    ("daily_basic", "估值与换手", "P0"),
    ("stk_limit", "涨跌停价格", "P0"),
    ("moneyflow_dc", "东财主力/散户资金", "P0"),
    ("adj_factor", "复权因子", "P1"),
    ("moneyflow", "Tushare资金流", "P1"),
    ("moneyflow_ths", "同花顺资金流", "P1"),
    ("cyq_perf", "筹码胜率摘要", "P1"),
    ("cyq_chips", "筹码分布明细", "P1"),
    ("stk_factor_pro", "专业技术因子", "P1"),
)


def raw_api_window_summary(connection: Any, api_name: str, symbol: str, start_date: date, end_date: date) -> dict[str, Any]:
    row = connection.execute(
        """SELECT count(*)::int rows,max(row_data->>'trade_date') latest_date
             FROM quant.tushare_raw_records
            WHERE api_name=%s AND row_data->>'ts_code'=%s
              AND row_data->>'trade_date' BETWEEN %s AND %s""",
        (api_name, symbol, start_date.strftime("%Y%m%d"), end_date.strftime("%Y%m%d")),
    ).fetchone()
    return {"rows": int(row["rows"] or 0), "latest_date": row["latest_date"]}


def _retired_note(retired: list[date], adjustment_retired: dict[date, dict[str, Any]]) -> str:
    """Name the ledger evidence for a date nobody is going to repair."""
    reasons = sorted({str(adjustment_retired[value].get("reason") or "") for value in retired
                      if adjustment_retired.get(value)})
    run_keys = [str(adjustment_retired[value].get("run_key") or "") for value in retired
                if adjustment_retired.get(value)]
    return (f"retired: {len(retired)} settled session(s) were refused by the daily-controls "
            "coverage gate often enough to be dropped from the adjustment-factor work list, so "
            "no repair is queued for them (" + "; ".join(reasons) + "). Clear the ledger row(s) "
            + ", ".join(run_keys) + " after the daily cross-section is repaired.")


def _adjustment_item(label: str, priority: str, adjustment: Any,
                     adjustment_pending: set[date],
                     adjustment_retired: dict[date, dict[str, Any]] | None = None) -> dict[str, Any]:
    """Derive the adj_factor readiness item from real, symbol-scoped coverage.

    ``ready`` means every settled session of THIS symbol in the window carries
    a real cumulative factor.  ``pending`` means the ones that do not are all
    on the maintenance job's work list, so they are expected to arrive.
    ``retired`` means at least one of them has been dropped from that list by
    the blocked-date ledger, so claiming a repair is queued would be false --
    the verdict is separated from ``missing`` because the operator action is
    different (repair the daily cross-section and clear the ledger row, rather
    than wait).  Anything else is ``missing`` -- including the mixed case,
    which fails closed rather than advertising a window the factor job will
    not complete.
    """
    retired_details = adjustment_retired or {}
    rows = int((adjustment or {}).get("rows") or 0)
    latest_date = (adjustment or {}).get("latest_date")
    settled_sessions = int((adjustment or {}).get("settled_sessions") or 0)
    uncovered = list((adjustment or {}).get("uncovered_dates") or [])
    queued = [value for value in uncovered if value in adjustment_pending]
    retired = [value for value in uncovered if value in retired_details]
    if not uncovered and settled_sessions and rows:
        status, note = "ready", (
            f"complete: all {settled_sessions} settled session(s) in this window carry a real "
            "cumulative factor for this symbol")
    elif retired:
        status, note = "retired", _retired_note(retired, retired_details)
    elif uncovered and len(queued) == len(uncovered):
        status, note = "pending", (
            f"pending: {len(uncovered)} of {settled_sessions} settled session(s) have no real "
            "cumulative factor yet and are queued for the adjustment-factor maintenance job "
            "(scripts/adjustment-factor-maintenance.py sync)")
    elif uncovered:
        status, note = "missing", (
            f"missing: {len(uncovered)} of {settled_sessions} settled session(s) have no real "
            f"cumulative factor and only {len(queued)} of them are on the maintenance work list; "
            "adjusted prices over this window fail closed")
    else:
        status, note = "missing", (
            "missing: no settled session in this window carries a real cumulative factor "
            "(placeholder rows are deliberately not counted)")
    return {"api_name": "adj_factor", "label": label, "priority": priority, "rows": rows,
            "latest_date": str(latest_date) if latest_date else None, "status": status,
            "settled_sessions": settled_sessions,
            "pending_dates": [str(value) for value in queued],
            "retired_dates": [str(value) for value in retired],
            "missing_dates": [str(value) for value in uncovered
                              if value not in adjustment_pending and value not in retired_details],
            "note": note}


def stock_window_readiness(database: Any, symbol: str, start_date: date, end_date: date) -> dict[str, Any]:
    """Report only locally persisted evidence; never trigger a provider call."""
    table_by_api = {
        "daily": "quant.canonical_bars_daily",
        "daily_basic": "quant.daily_fundamentals",
        "stk_limit": "quant.daily_trade_limits",
    }
    with database.transaction() as connection:
        # Adjustment factors arrive on their own maintenance lane, so an empty
        # window here has three very different meanings: the date was never
        # fetched at all, it is queued for the factor job, or the factor job
        # has retired it and will never fetch it again.  The first two used to
        # read as a bare "missing" -- and before the identity placeholder was
        # removed they both read as a false "ready"; the third used to read as
        # "pending ... queued", which promised a repair nobody was going to
        # attempt.
        pending, adjustment_retired = pending_and_retired_dates_between(
            connection, start_date, end_date)
        adjustment_pending = set(pending)
        adjustment = connection.execute(
            ADJUSTMENT_WINDOW_SQL,
            (symbol, start_date, end_date, symbol, start_date, end_date),
        ).fetchone()
        items: list[dict[str, Any]] = []
        for api_name, label, priority in _SPECS:
            table = table_by_api.get(api_name)
            if api_name == "adj_factor":
                items.append(_adjustment_item(
                    label, priority, adjustment, adjustment_pending, adjustment_retired))
                continue
            if table is not None:
                row = connection.execute(
                    f"""SELECT count(*)::int rows,max(trading_date) latest_date
                         FROM {table}
                        WHERE symbol=%s AND trading_date BETWEEN %s AND %s""",
                    (symbol, start_date, end_date),
                ).fetchone()
                rows, latest_date = int(row["rows"] or 0), row["latest_date"]
            else:
                summary = raw_api_window_summary(connection, api_name, symbol, start_date, end_date)
                rows, latest_date = summary["rows"], summary["latest_date"]
            items.append({"api_name": api_name, "label": label, "priority": priority, "rows": rows,
                          "latest_date": str(latest_date) if latest_date else None,
                          "status": "ready" if rows > 0 else "missing"})
    blockers = [item["api_name"] for item in items if item["priority"] == "P0" and item["status"] != "ready"]
    return {"symbol": symbol, "window_start": str(start_date), "window_end": str(end_date),
            "mode": "on_demand_single_stock_window", "decision_ready": not blockers,
            "blockers": blockers, "items": items}


def stock_study_claims(database: Any, symbol: str) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Aggregate only locally available, text-derived stock claims."""
    with database.transaction() as connection:
        rows = connection.execute(
            """SELECT c.claim_id,a.name analyst_name,c.subject_label,c.direction,c.strength,c.horizon_days,
                      c.extraction_confidence,c.available_at,left(e.body,500) evidence
                 FROM quant.analyst_claims c JOIN quant.remote_analysts a ON a.remote_analyst_id=c.remote_analyst_id
                 JOIN quant.analyst_evidence e ON e.evidence_id=c.evidence_id
                WHERE c.scope='stock' AND c.subject_key=%s AND c.available_at<=now()
                ORDER BY c.available_at DESC,c.created_at DESC LIMIT 50""",
            (symbol,),
        ).fetchall()
    claims = [dict(row) for row in rows]
    denominator = sum(float(row["strength"] or 0) * float(row["extraction_confidence"] or 0) for row in claims)
    weighted = sum(float(row["direction"] or 0) * float(row["strength"] or 0) * float(row["extraction_confidence"] or 0) for row in claims)
    normalized = round(weighted / denominator, 4) if denominator else 0.0
    direction = "positive" if normalized >= 0.2 else "negative" if normalized <= -0.2 else "neutral"
    return claims, {
        "claim_count": len(claims),
        "positive": sum(1 for row in claims if row["direction"] > 0),
        "negative": sum(1 for row in claims if row["direction"] < 0),
        "neutral": sum(1 for row in claims if row["direction"] == 0),
        "score": normalized, "direction": direction,
    }


__all__ = [
    "ADJUSTMENT_WINDOW_SQL", "raw_api_window_summary", "stock_study_claims", "stock_window_readiness",
]
