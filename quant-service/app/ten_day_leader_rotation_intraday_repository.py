"""Persistence boundary for intraday ten-day-shadow observations."""

from __future__ import annotations

from typing import Any, Callable

from psycopg.types.json import Json


def persist_intraday_rotation_observations(
    connection: Any, *, run_id: Any, scan_id: Any, observations: list[dict[str, Any]],
    json_safe: Callable[[Any], Any],
) -> int:
    """Append bounded research observations; no signal/order table is touched."""
    for item in observations:
        connection.execute(
            """INSERT INTO quant.ten_day_leader_rotation_intraday_observations(
                   run_id,scan_id,symbol,observed_at,quote_source,shadow_state,shadow_eligible,
                   decision_eligible,evidence,reason_codes,risk_flags,source_snapshot)
               VALUES(%s,%s,%s,%s,%s,%s,%s,false,%s,%s,%s,%s)
               ON CONFLICT(run_id,scan_id,symbol) DO UPDATE SET
                   quote_source=EXCLUDED.quote_source,shadow_state=EXCLUDED.shadow_state,
                   shadow_eligible=EXCLUDED.shadow_eligible,evidence=EXCLUDED.evidence,
                   reason_codes=EXCLUDED.reason_codes,risk_flags=EXCLUDED.risk_flags,
                   source_snapshot=EXCLUDED.source_snapshot""",
            (run_id, scan_id, item["symbol"], item["observed_at"], item["quote_source"], item["shadow_state"],
             item["shadow_eligible"], Json(json_safe(item["evidence"])), Json(json_safe(item["reason_codes"])),
             Json(json_safe(item["risk_flags"])), Json(json_safe(item["source_snapshot"]))),
        )
    return len(observations)


def persist_intraday_rotation_scan_status(
    connection: Any, *, scan_id: Any, status: dict[str, Any], json_safe: Callable[[Any], Any],
) -> None:
    """Expose a shadow batch result without changing primary scan semantics."""
    connection.execute(
        """UPDATE quant.intraday_scan_runs
              SET source_status=source_status || %s::jsonb
            WHERE scan_id=%s""",
        (Json({"ten_day_leader_rotation_shadow": json_safe(status)}), scan_id),
    )


__all__ = ["persist_intraday_rotation_observations", "persist_intraday_rotation_scan_status"]
