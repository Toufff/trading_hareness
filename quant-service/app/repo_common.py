"""Shared repository infrastructure: bounded pagination, symbol validation,
row-fetch helpers, and numeric coercion.

The repository layer had accumulated dozens of independent copies of the same
small pieces of plumbing: ~30 inline ``max(1, min(int(limit), N))`` clamps,
eight copies of the ``next_offset`` pagination expression, nine-plus inline
``\\d{6}\\.(SH|SZ|BJ)`` symbol regexes, four module-local async ``_fetchall``
helpers and thirty-five near-identical ``_number`` coercion functions (see the
trading_hareness audit, section J).  This module gives repositories owned by
this work package one place to import each of those from instead of drifting
further apart.  It has no database or provider dependency of its own.
"""

from __future__ import annotations

import re
from typing import Any, Sequence


#: Canonical A-share/exchange symbol shape, e.g. ``600000.SH``.
SYMBOL_RE = re.compile(r"^\d{6}\.(SH|SZ|BJ)$")


def bounded_limit(value: Any, maximum: int, minimum: int = 1) -> int:
    """Clamp a caller-supplied page size into ``[minimum, maximum]``.

    Every read endpoint that accepts a ``limit`` query parameter must reject
    an unbounded, negative or non-numeric value before it reaches SQL.  A
    bad input falls back to ``minimum`` rather than raising, matching the
    permissive behaviour of the inline clamps this replaces.
    """
    try:
        numeric = int(value)
    except (TypeError, ValueError):
        numeric = minimum
    return max(minimum, min(numeric, maximum))


def bounded_offset(value: Any, minimum: int = 0) -> int:
    """Clamp a caller-supplied page offset to a non-negative integer."""
    try:
        numeric = int(value)
    except (TypeError, ValueError):
        numeric = minimum
    return max(minimum, numeric)


def next_offset(offset: int, returned: int, total: int) -> int | None:
    """Return the next page offset, or ``None`` once the page reaches the end.

    Mirrors the ``offset + len(rows) if offset + len(rows) < total else None``
    expression duplicated across the read models and repositories.
    """
    advanced = offset + returned
    return advanced if advanced < total else None


def paginate(rows: Sequence[Any], *, limit: int, offset: int, total: int) -> dict[str, Any]:
    """Return the common ``{items, limit, offset, total, next_offset}`` shape."""
    return {
        "items": list(rows),
        "limit": limit,
        "offset": offset,
        "total": total,
        "next_offset": next_offset(offset, len(rows), total),
    }


def numeric_or_default(value: Any, default: float = 0.0) -> float:
    """Coerce a possibly-``Decimal``/``None``/empty-string DB value to ``float``.

    This is the ``_number`` helper duplicated, with drifting defaults and
    edge-case handling, across roughly three dozen modules.  Repositories
    owned by this work package import this single implementation instead of
    keeping their own copy.
    """
    try:
        return float(value) if value not in (None, "") else default
    except (TypeError, ValueError):
        return default


def fetch_one(connection: Any, sql: str, params: Any = ()) -> Any:
    """Run one query on a synchronous connection and return its single row."""
    return connection.execute(sql, params).fetchone()


def fetch_all(connection: Any, sql: str, params: Any = ()) -> list[Any]:
    """Run one query on a synchronous connection and return all rows."""
    return connection.execute(sql, params).fetchall()


async def async_fetch_one(connection: Any, sql: str, params: Any = ()) -> Any:
    """Async counterpart of :func:`fetch_one` for native async connections."""
    result = await connection.execute(sql, params)
    return await result.fetchone()


async def async_fetch_all(connection: Any, sql: str, params: Any = ()) -> list[Any]:
    """Async counterpart of :func:`fetch_all`.

    Replaces the module-local ``async def _fetchall(connection, query, params)``
    helper duplicated verbatim across several async repositories.
    """
    result = await connection.execute(sql, params)
    return await result.fetchall()


__all__ = [
    "SYMBOL_RE",
    "async_fetch_all",
    "async_fetch_one",
    "bounded_limit",
    "bounded_offset",
    "fetch_all",
    "fetch_one",
    "next_offset",
    "numeric_or_default",
    "paginate",
]
