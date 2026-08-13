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
    """Convert provider scalar values for PostgreSQL numeric fields."""
    return Decimal(str(value)) if value is not None and value != "" else None


__all__ = ["decimal_or_none", "intraday_number"]
