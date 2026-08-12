"""Read-only, bounded intraday signal outcome and attribution projection."""

from __future__ import annotations

from typing import Any, Callable


def latest_intraday_outcomes(
    database: Any,
    limit: int,
    *,
    market_context_batch_fn: Callable[[Any, list[tuple[Any, str]]], dict[tuple[Any, str], dict[str, Any]]],
    attribution_fn: Callable[[str, str, dict[str, Any] | None, dict[str, Any] | None, dict[str, Any]], dict[str, Any]],
    attribution_summary_fn: Callable[[list[dict[str, Any]]], dict[str, Any]],
) -> dict[str, Any]:
    """Read stored outcomes only; board context is batched inside one transaction."""
    bounded_limit = max(1, min(limit, 500))
    attribution_window_limit = 5000
    missing_context = {
        "status": "missing", "market_state": "unknown", "board_snapshot_age_seconds": None,
        "symbol_board_matches": [], "notice": "no board snapshot existed before the signal",
    }
    with database.transaction() as connection:
        raw_rows = connection.execute(
            """SELECT o.signal_event_id,o.horizon_key,o.direction,o.entry_observed_at,o.entry_price,o.exit_observed_at,o.exit_price,
                      o.raw_return,o.maximum_favorable_excursion,o.maximum_adverse_excursion,o.status,o.tradability,o.source_status,o.calculated_at,
                      s.symbol,s.signal_key,s.signal_type,s.severity,s.state,s.score,s.observed_at,s.conditions,s.evidence,s.risk_flags
                 FROM quant.intraday_signal_outcomes o
                 JOIN quant.intraday_signal_events s ON s.signal_event_id=o.signal_event_id
                ORDER BY o.calculated_at DESC,s.observed_at DESC,o.horizon_key
                LIMIT %s""",
            (attribution_window_limit,),
        ).fetchall()
        raw_items = [dict(raw_row) for raw_row in raw_rows]
        market_contexts = market_context_batch_fn(
            connection,
            [(item["observed_at"], str(item["symbol"])) for item in raw_items],
        )
        rows: list[dict[str, Any]] = []
        attribution_cache: dict[str, dict[str, Any]] = {}
        for item in raw_items:
            evidence = item.get("evidence") if isinstance(item.get("evidence"), dict) else {}
            attribution = evidence.get("attribution") if isinstance(evidence.get("attribution"), dict) else None
            if attribution is None:
                event_key = str(item["signal_event_id"])
                attribution = attribution_cache.get(event_key)
                if attribution is None:
                    attribution = attribution_fn(
                        str(item["signal_key"]), str(item["signal_type"]), item.get("conditions"), evidence,
                        market_contexts.get((item["observed_at"], str(item["symbol"])), missing_context),
                    )
                    attribution_cache[event_key] = attribution
            item["attribution"] = attribution
            item.pop("evidence", None)
            rows.append(item)
        summary = connection.execute(
            """SELECT horizon_key,status,count(*)::int rows,avg(raw_return) avg_directional_return,
                      avg(maximum_favorable_excursion) avg_mfe,avg(maximum_adverse_excursion) avg_mae
                 FROM quant.intraday_signal_outcomes
                 GROUP BY horizon_key,status ORDER BY horizon_key,status"""
        ).fetchall()
    attribution_summary = attribution_summary_fn(rows)
    return {
        "items": rows[:bounded_limit], "summary": summary, "attribution_summary": attribution_summary["items"],
        "attribution_validation_gate": attribution_summary["validation_gate"],
        "attribution_window_outcomes": len(rows), "attribution_window_limit": attribution_window_limit,
        "notice": "结果只衡量信号后的可观察价格路径，不代表成交、收益承诺或自动交易表现。",
    }


__all__ = ["latest_intraday_outcomes"]
