"""Explicit adjusted-price contract for cross-session research features.

Canonical bars intentionally retain exchange/raw prices for execution facts,
price-limit checks and audit.  Cross-session research is different: it needs a
consistent adjustment basis or it must decline to calculate.  This module is
the single place that makes that distinction explicit.
"""

from __future__ import annotations

from typing import Any


ADJUSTMENT_MISSING_FLAG = "adj_factor_missing"
CORPORATE_ACTION_UNRESOLVED_FLAG = "corporate_action_unresolved"


def number(value: Any) -> float | None:
    try:
        return float(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def adjusted_value(row: dict[str, Any], field: str = "close") -> float | None:
    """Return a strict same-day adjusted research value.

    There is deliberately no raw-price fallback.  A missing factor is a data
    quality condition, not evidence that the raw series is continuous through
    corporate actions.
    """
    raw = number(row.get(field))
    factor = number(row.get("adj_factor"))
    if raw is None or factor is None or factor <= 0:
        return None
    return raw * factor


def adjusted_bars(rows: list[dict[str, Any]], *, fields: tuple[str, ...] = ("open", "high", "low", "close")) -> tuple[list[dict[str, Any]] | None, list[str]]:
    """Copy bars with ``research_*`` values, or return explicit quality flags.

    The caller keeps raw bars for all execution semantics.  Cross-session
    indicators must use only the returned view.  A discontinuity with complete
    factors is already represented by the adjusted values; an incomplete
    factor window is never silently mixed with raw values.
    """
    if not rows:
        return [], []
    prepared: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        factor = number(item.get("adj_factor"))
        if factor is None or factor <= 0:
            return None, [ADJUSTMENT_MISSING_FLAG]
        for field in fields:
            raw = number(item.get(field))
            # Some sparse historical records lack open/high/low.  Close is
            # mandatory for a cross-session feature; auxiliary fields fall
            # back to the adjusted close only inside the research view.
            if raw is None:
                if field == "close":
                    return None, [CORPORATE_ACTION_UNRESOLVED_FLAG]
                raw = number(item.get("close"))
            if raw is None:
                return None, [CORPORATE_ACTION_UNRESOLVED_FLAG]
            item[f"research_{field}"] = raw * factor
        prepared.append(item)
    return prepared, []


__all__ = [
    "ADJUSTMENT_MISSING_FLAG",
    "CORPORATE_ACTION_UNRESOLVED_FLAG",
    "adjusted_bars",
    "adjusted_value",
    "number",
]
