"""Point-in-time local reads used while materializing deterministic features."""

from __future__ import annotations

from datetime import date
from typing import Any, Callable


def market_regime(connection: Any, as_of_date: date, number: Callable[[Any], float]) -> str:
    rows = connection.execute(
        """SELECT close FROM quant.canonical_bars_daily WHERE symbol='000300.SH' AND trading_date<=%s
           ORDER BY trading_date DESC LIMIT 21""", (as_of_date,)
    ).fetchall()
    if len(rows) < 20:
        return "unknown"
    return "risk_on" if number(rows[0]["close"]) >= number(rows[-1]["close"]) else "risk_off"


def latest_tushare_row(connection: Any, api_name: str, symbol: str, as_of_date: date) -> dict[str, Any] | None:
    """Read persisted raw evidence; never call a provider."""
    rows = connection.execute(
        """SELECT row_data FROM quant.tushare_raw_records
           WHERE api_name=%s AND row_data->>'ts_code'=%s
             AND coalesce(row_data->>'trade_date','')<=%s
           ORDER BY coalesce(row_data->>'trade_date','') DESC,available_at DESC LIMIT 1""",
        (api_name, symbol, as_of_date.strftime("%Y%m%d")),
    ).fetchall()
    return dict(rows[0]["row_data"]) if rows else None


def analyst_feature(connection: Any, symbol: str, as_of_date: date, number: Callable[[Any], float]) -> dict[str, Any]:
    rows = connection.execute(
        """SELECT o.analyst_id AS remote_analyst_id,o.direction,o.strength,o.confidence AS extraction_confidence,
                  o.horizon_days,left(o.evidence_span,220) evidence
           FROM quant.analyst_observations o
           WHERE o.scope='stock' AND o.subject_key=%s AND o.status='eligible'
             AND (o.strategy_available_at AT TIME ZONE 'Asia/Shanghai')::date<=%s
           ORDER BY o.strategy_available_at DESC,o.created_at DESC LIMIT 50""", (symbol, as_of_date)
    ).fetchall()
    if not rows:
        return {"consensus": 0.0, "claim_count": 0, "analyst_skill": 0.5, "evidence": [],
                "status": "research_only_no_eligible_observation"}
    weighted = [number(row["direction"]) * number(row["strength"]) * number(row["extraction_confidence"]) for row in rows]
    weights = [number(row["strength"]) * number(row["extraction_confidence"]) for row in rows]
    return {"consensus": round(sum(weighted) / sum(weights), 5) if sum(weights) else 0.0,
            "claim_count": len(rows), "analyst_skill": 0.5, "status": "eligible_observation_context_only",
            # The same two columns are coerced above to compute the consensus;
            # passing them through raw here hands psycopg a Decimal, whose JSON
            # dump has no default hook, so the whole feature snapshot fails to
            # persist for any symbol that has an eligible observation.
            "evidence": [{"analyst_id": row["remote_analyst_id"], "direction": number(row["direction"]),
                          "strength": number(row["strength"]), "horizon_days": row["horizon_days"],
                          "evidence": row["evidence"]}
                         for row in rows[:8]]}

