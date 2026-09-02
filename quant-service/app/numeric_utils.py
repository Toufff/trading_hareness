"""Small deterministic numeric normalization helpers shared by live/replay code."""

from __future__ import annotations

from decimal import Decimal
from typing import Any


def intraday_number(value: Any) -> float | None:
    """Parse provider numeric fields without changing missing values to zero."""
    try:
        return float(str(value).replace(",", "").replace("%", "").strip())
    except (TypeError, ValueError):
        return None


def decimal_or_none(value: Any) -> Decimal | None:
    """Convert provider scalar values for PostgreSQL numeric fields.

    A string like ``"nan"`` or ``"inf"`` parses without error into a
    non-finite ``Decimal``, which would otherwise be silently promoted into
    a numeric column (e.g. ``daily_fundamentals``) that has no coherent
    representation for it.  Such a value is treated as absent instead.
    """
    if value is None or value == "":
        return None
    result = Decimal(str(value))
    return result if result.is_finite() else None


__all__ = ["decimal_or_none", "intraday_number"]
