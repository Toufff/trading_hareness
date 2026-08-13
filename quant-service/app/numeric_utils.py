"""Small deterministic numeric normalization helpers shared by live/replay code."""

from __future__ import annotations

from typing import Any


def intraday_number(value: Any) -> float | None:
    """Parse provider numeric fields without changing missing values to zero."""
    try:
        return float(str(value).replace(",", "").replace("%", "").strip())
    except (TypeError, ValueError):
        return None


__all__ = ["intraday_number"]
