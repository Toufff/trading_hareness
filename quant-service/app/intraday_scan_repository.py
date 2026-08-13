"""Persistence primitives for one intraday scan's terminal state.

The scan scheduler owns timing and provider calls; this module owns only the
small synchronous transaction used to record a blocked/empty terminal state.
Keeping the database object explicit makes the function safe to submit to the
bounded database executor and gives the remaining scan persistence a stable
extraction seam.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
import uuid

from psycopg.types.json import Json

from .provider_health import record_provider_failure
from .tushare_providers import safe_error_detail


def persist_intraday_scan_terminal(
    database: Any,
    scan_id: uuid.UUID,
    observed_at: datetime,
    status: str,
    requested_symbols: list[str],
    source_status: dict[str, Any],
    summary: dict[str, Any],
    provider_failure: str | None = None,
    provider_latency_ms: int | None = None,
) -> None:
    """Write a terminal scan state and optional public-source failure.

    ``provider_latency_ms`` is optional for compatibility with non-provider
    terminal states (for example a closed-session gate).  When a provider
    failed, preserving the measured elapsed time prevents the health panel
    from losing the last useful latency sample.
    """
    with database.transaction() as connection:
        if provider_failure:
            record_provider_failure(
                connection,
                "tencent_free",
                "realtime_quote",
                safe_error_detail(provider_failure, 300),
                provider_latency_ms,
            )
        connection.execute(
            """INSERT INTO quant.intraday_scan_runs(
                   scan_id,observed_at,status,requested_symbols,source_status,summary
               ) VALUES(%s,%s,%s,%s,%s,%s)""",
            (
                scan_id,
                observed_at,
                status,
                Json(requested_symbols),
                Json(source_status),
                Json(summary),
            ),
        )


__all__ = ["persist_intraday_scan_terminal"]
