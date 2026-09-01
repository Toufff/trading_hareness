"""Native-async read model for actual holdings and personal decision briefs."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from .personal_decision_contracts import assemble_personal_decision_brief


async def _one(connection: Any, sql: str, params: tuple[Any, ...] = ()) -> Any:
    result = await connection.execute(sql, params)
    return await result.fetchone()


async def _all(connection: Any, sql: str, params: tuple[Any, ...] = ()) -> list[Any]:
    result = await connection.execute(sql, params)
    return await result.fetchall()


async def latest_broker_snapshot(async_database: Any, account_key: str) -> dict[str, Any] | None:
    async with async_database.transaction() as connection:
        snapshot = await _one(
            connection,
            """SELECT snapshot_id,account_key,source,source_snapshot_key,observed_at,verification,
                      cash,total_asset,total_market_value,content_hash,metadata,recorded_at
                 FROM quant.broker_portfolio_snapshots WHERE account_key=%s
                ORDER BY observed_at DESC,recorded_at DESC LIMIT 1""",
            (account_key,),
        )
        if not snapshot:
            return None
        positions = await _all(
            connection,
            """SELECT symbol,name,quantity,sellable_quantity,average_cost,market_price,market_value,
                      unrealized_pnl,position_weight_pct,metadata
                 FROM quant.broker_position_snapshots WHERE snapshot_id=%s ORDER BY market_value DESC NULLS LAST,symbol""",
            (snapshot["snapshot_id"],),
        )
    return {**dict(snapshot), "positions": [dict(row) for row in positions], "live_orders": False}


async def active_trade_plans(async_database: Any, as_of_at: datetime) -> list[dict[str, Any]]:
    async with async_database.transaction() as connection:
        rows = await _all(
            connection,
            """SELECT DISTINCT ON (plan_kind,symbol)
                      plan_key,plan_kind,symbol,name,as_of_at,valid_until,action,entry_zone,add_trigger,
                      reduce_trigger,exit_trigger,stop_price,target_prices,max_position_pct,rationale,
                      evidence_refs,risk_flags,metadata
                FROM quant.personal_trade_plans
                WHERE as_of_at<=%s AND valid_until>=%s
                ORDER BY plan_kind,symbol,created_at DESC,as_of_at DESC""",
            (as_of_at, as_of_at),
        )
    return [dict(row) for row in rows]


async def latest_market_section(
    async_database: Any,
    *,
    as_of_at: datetime | None = None,
    max_age: timedelta = timedelta(days=4),
) -> dict[str, Any] | None:
    async with async_database.transaction() as connection:
        row = await _one(
            connection,
            """SELECT review_id,exchange_date,session,observed_at,market_state,data_boundary,report
                 FROM quant.strategy_review_runs ORDER BY observed_at DESC LIMIT 1""",
        )
    if not row:
        return None
    payload = dict(row)
    observed_at = payload.get("observed_at")
    boundary = as_of_at or datetime.now(timezone.utc)
    if isinstance(observed_at, str):
        observed_at = datetime.fromisoformat(observed_at.replace("Z", "+00:00"))
    age = (
        boundary.astimezone(timezone.utc) - observed_at.astimezone(timezone.utc)
        if isinstance(observed_at, datetime) and observed_at.tzinfo is not None
        else None
    )
    temporally_current = age is not None and -timedelta(minutes=5) <= age <= max_age
    report = payload.get("report") if isinstance(payload.get("report"), dict) else {}
    index_context = report.get("index_breadth_context") if isinstance(report, dict) else {}
    quality_flags = list(index_context.get("quality_flags") or []) if isinstance(index_context, dict) else []
    status = (
        "ready" if temporally_current and not quality_flags
        else "degraded" if temporally_current
        else "unavailable"
    )
    return {"status": status, "quality_flags": quality_flags, **payload}


async def latest_personal_decision_brief(
    async_database: Any, account_key: str, *, as_of_at: datetime | None = None,
) -> dict[str, Any]:
    observed_at = as_of_at or datetime.now(timezone.utc)
    portfolio = await latest_broker_snapshot(async_database, account_key)
    plans = await active_trade_plans(async_database, observed_at)
    market = await latest_market_section(async_database, as_of_at=observed_at)
    return assemble_personal_decision_brief(
        as_of_at=observed_at, market_section=market, portfolio=portfolio, plans=plans,
    )


async def latest_decision_research(async_database: Any) -> dict[str, Any]:
    """Return the latest bounded dossier batch with human-readable gates."""
    async with async_database.transaction() as connection:
        latest = await _one(
            connection,
            "SELECT max(as_of_date) AS as_of_date FROM quant.decision_research_dossiers",
        )
        as_of_date = latest["as_of_date"] if latest else None
        if as_of_date is None:
            return {"as_of_date": None, "items": [], "summary": {"total": 0, "passed": 0, "rejected": 0, "incomplete": 0}}
        rows = await _all(
            connection,
            """WITH latest_model AS (
                   SELECT model_version FROM quant.decision_research_dossiers
                    WHERE as_of_date=%s ORDER BY created_at DESC LIMIT 1
               ), latest_candidate_run AS (
                   SELECT source_candidate_run_id FROM quant.decision_research_dossiers
                    WHERE as_of_date=%s AND model_version=(SELECT model_version FROM latest_model)
                      AND evidence_snapshot->>'role'='candidate' AND source_candidate_run_id IS NOT NULL
                    ORDER BY created_at DESC LIMIT 1
               ), selected AS (
                   SELECT DISTINCT ON (d.symbol,d.evidence_snapshot->>'role') d.dossier_id
                     FROM quant.decision_research_dossiers d
                    WHERE d.as_of_date=%s AND d.model_version=(SELECT model_version FROM latest_model)
                      AND (
                        d.evidence_snapshot->>'role'='holding'
                        OR d.source_candidate_run_id=(SELECT source_candidate_run_id FROM latest_candidate_run)
                      )
                    ORDER BY d.symbol,d.evidence_snapshot->>'role',d.created_at DESC
               )
               SELECT d.dossier_id,d.dossier_key,d.as_of_date,d.symbol,d.name,d.strategy_family,
                      d.model_version,d.status,d.conclusion,d.source_candidate_rank,d.evidence_snapshot,
                      d.evidence_refs,d.created_at,
                      coalesce(jsonb_agg(jsonb_build_object(
                        'gate_key',g.gate_key,'label',g.label,'verdict',g.verdict,
                        'independent_run',g.independent_run,'conclusion',g.conclusion,'evidence',g.evidence
                      ) ORDER BY g.gate_key) FILTER (WHERE g.gate_key IS NOT NULL),'[]'::jsonb) AS gates
                 FROM quant.decision_research_dossiers d
                 JOIN selected selected_dossier ON selected_dossier.dossier_id=d.dossier_id
                 LEFT JOIN quant.decision_research_gates g ON g.dossier_id=d.dossier_id
                GROUP BY d.dossier_id
                ORDER BY d.status='passed' DESC,d.source_candidate_rank NULLS FIRST,d.symbol""",
            (as_of_date, as_of_date, as_of_date),
        )
    items = [dict(row) for row in rows]
    return {
        "as_of_date": as_of_date,
        "items": items,
        "summary": {
            "total": len(items),
            "passed": sum(item["status"] == "passed" for item in items),
            "rejected": sum(item["status"] == "rejected" for item in items),
            "incomplete": sum(item["status"] == "incomplete" for item in items),
        },
        "boundary": "terminal research audit; a passed short-term dossier is not a long-term value claim",
    }


__all__ = [
    "active_trade_plans", "latest_broker_snapshot", "latest_decision_research",
    "latest_market_section", "latest_personal_decision_brief",
]
