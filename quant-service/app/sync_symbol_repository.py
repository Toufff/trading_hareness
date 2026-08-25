"""Synchronous local fallback universes for bounded provider synchronization."""

from __future__ import annotations

from typing import Any


def core_symbols(database: Any) -> list[str]:
    """Return enabled core symbols in configured priority order."""
    with database.transaction() as connection:
        rows = connection.execute(
            """SELECT symbol FROM quant.universe_members
                 WHERE universe_key='core' AND enabled
                 ORDER BY priority,symbol""",
        ).fetchall()
    return [str(row["symbol"]) for row in rows]


def analyst_claim_symbols(database: Any) -> list[str]:
    """Return syntactically valid stock subjects retained in analyst claims."""
    with database.transaction() as connection:
        rows = connection.execute(
            """SELECT DISTINCT subject_key FROM quant.analyst_claims
                 WHERE scope='stock' AND subject_key ~ '^\\d{6}\\.(SH|SZ|BJ)$'""",
        ).fetchall()
    return [str(row["subject_key"]) for row in rows]


__all__ = ["analyst_claim_symbols", "core_symbols"]
