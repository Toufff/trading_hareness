"""Orchestrate one idempotent Longhu/Tencent post-close market refresh."""

from __future__ import annotations

import hashlib
import json
from datetime import date, datetime, timezone
from typing import Any, Awaitable, Callable

from psycopg.types.json import Json

from .longhu_market_repository import persist_full_market_close, persist_settled_trade_calendar
from .longhu_market_sync import PROVIDER_KEY, merge_cross_section
from .longhu_vendor_source import LonghuVendorSource


MINIMUM_ROWS = 3_500
MINIMUM_COVERAGE = 0.95


async def sync(
    trade_date: date,
    *,
    db: Any,
    run_public_blocking: Callable[..., Awaitable[Any]],
    run_database_blocking: Callable[..., Awaitable[Any]],
    persist_rows: Callable[..., int],
    persist_flow_rows: Callable[..., int],
    source_factory: Callable[[], LonghuVendorSource] = LonghuVendorSource,
    force: bool = False,
) -> dict[str, Any]:
    request_key = hashlib.sha256(json.dumps({
        "capability": "longhu_full_market_close_v2", "trade_date": str(trade_date),
        "minimum_rows": MINIMUM_ROWS, "minimum_coverage": MINIMUM_COVERAGE,
    }, sort_keys=True).encode("utf-8")).hexdigest()

    def prepare() -> dict[str, Any] | None:
        with db.transaction() as connection:
            prior = connection.execute(
                "SELECT status,row_count,metadata FROM quant.fetch_runs WHERE request_key=%s", (request_key,),
            ).fetchone()
            if prior and prior["status"] == "completed" and not force:
                # Older completed receipts can predate derived calendar
                # projection.  Repair that deterministic local artifact even
                # when no provider call is needed.
                calendar_rows = persist_settled_trade_calendar(
                    connection, trade_date, datetime.now(timezone.utc),
                )
                return {
                    "status": "unchanged", "trade_date": str(trade_date),
                    "imported": int(prior["row_count"] or 0), "request_key": request_key,
                    "provider": PROVIDER_KEY, "metadata": prior["metadata"],
                    "calendar_rows": calendar_rows,
                }
            connection.execute(
                """INSERT INTO quant.fetch_runs(
                       provider_key,capability,trade_date,request_key,status,attempt_count,started_at,metadata)
                   VALUES(%s,'daily_all_a',%s,%s,'running',1,now(),%s)
                   ON CONFLICT(request_key) DO UPDATE SET status='running',
                     attempt_count=quant.fetch_runs.attempt_count+1,started_at=now(),finished_at=NULL,
                     error_class=NULL,error_message=NULL""",
                (PROVIDER_KEY, trade_date, request_key, Json({
                    "source": "longhuvip_industry_plus_tencent_ohlc",
                    "physical_vendor_page_limit": 300,
                })),
            )
        return None

    unchanged = await run_database_blocking(prepare)
    if unchanged:
        return unchanged
    try:
        source = source_factory()
        evidence = await run_public_blocking(
            source.fetch_full_market_evidence, trade_date, timeout_seconds=240,
        )
        merged = merge_cross_section(trade_date, evidence["vendor_rows"], evidence["quote_rows"])
        vendor_count = len(evidence["vendor_rows"])
        if vendor_count < MINIMUM_ROWS:
            raise RuntimeError(f"Longhu returned {vendor_count} symbols; minimum is {MINIMUM_ROWS}")
        if len(merged.daily_rows) < MINIMUM_ROWS or merged.coverage < MINIMUM_COVERAGE:
            raise RuntimeError(
                f"cross-source OHLC coverage {len(merged.daily_rows)}/{vendor_count} "
                f"({merged.coverage:.2%}) is below {MINIMUM_COVERAGE:.0%}"
            )
        observed_at = datetime.now(timezone.utc)

        def persist() -> dict[str, Any]:
            with db.transaction() as connection:
                return persist_full_market_close(
                    connection, trade_date=trade_date, request_key=request_key,
                    observed_at=observed_at, merged=merged, source_health=evidence["health"],
                    board_rows=evidence["board_rows"],
                    persist_rows=persist_rows, persist_flow_rows=persist_flow_rows,
                )

        persisted = await run_database_blocking(persist, timeout_seconds=240)
        return {
            "status": "completed", "trade_date": str(trade_date), "provider": PROVIDER_KEY,
            "request_key": request_key, **persisted, "source_health": evidence["health"],
            "semantic_boundary": (
                "main_net is vendor order-size classification; not institution identity or Level-2 order cancellation"
            ),
        }
    except Exception as error:
        def fail() -> None:
            with db.transaction() as connection:
                connection.execute(
                    """UPDATE quant.fetch_runs SET status='failed',finished_at=now(),
                              error_class=%s,error_message=%s
                        WHERE request_key=%s""",
                    (type(error).__name__, str(error)[:1000], request_key),
                )
        await run_database_blocking(fail)
        return {
            "status": "failed", "trade_date": str(trade_date), "provider": PROVIDER_KEY,
            "request_key": request_key, "reason": f"{type(error).__name__}: {error}",
        }


__all__ = ["MINIMUM_COVERAGE", "MINIMUM_ROWS", "sync"]
