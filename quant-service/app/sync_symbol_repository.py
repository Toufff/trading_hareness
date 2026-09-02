"""Synchronous local fallback universes for bounded provider synchronization."""

from __future__ import annotations

from typing import Any

#: Shared verbatim by ``async_sync_symbol_repository.py`` so the sync and
#: native-async fallback universes can never drift into two different
#: queries against the same tables.
CORE_SYMBOLS_SQL = """SELECT symbol FROM quant.universe_members
                 WHERE universe_key='core' AND enabled
                 ORDER BY priority,symbol"""
ANALYST_CLAIM_SYMBOLS_SQL = """SELECT DISTINCT subject_key FROM quant.analyst_claims
                 WHERE scope='stock' AND subject_key ~ '^\\d{6}\\.(SH|SZ|BJ)$'"""


def core_symbols(database: Any) -> list[str]:
    """Return enabled core symbols in configured priority order."""
    with database.transaction() as connection:
        rows = connection.execute(CORE_SYMBOLS_SQL).fetchall()
    return [str(row["symbol"]) for row in rows]


def analyst_claim_symbols(database: Any) -> list[str]:
    """Return syntactically valid stock subjects retained in analyst claims."""
    with database.transaction() as connection:
        rows = connection.execute(ANALYST_CLAIM_SYMBOLS_SQL).fetchall()
    return [str(row["subject_key"]) for row in rows]


__all__ = ["ANALYST_CLAIM_SYMBOLS_SQL", "CORE_SYMBOLS_SQL", "analyst_claim_symbols", "core_symbols"]
