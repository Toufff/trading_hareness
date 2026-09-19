"""Explicit adjusted-price contract for cross-session research features.

Canonical bars intentionally retain exchange/raw prices for execution facts,
price-limit checks and audit.  Cross-session research is different: it needs a
consistent adjustment basis or it must decline to calculate.  This module is
the single place that makes that distinction explicit.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


ADJUSTMENT_MISSING_FLAG = "adj_factor_missing"
CORPORATE_ACTION_UNRESOLVED_FLAG = "corporate_action_unresolved"
#: Recorded, never penalised: the window is usable, on an explicitly stated
#: assumption rather than on a fetched factor.
CARRIED_FORWARD_FLAG = "adj_factor_carried_forward"

#: The longest trailing factor gap a window may carry the last real factor
#: across.  Beyond it the assumption stops being "the fetch lane is a day or
#: two behind" and becomes "nobody is fetching", which must fail closed.
MAX_CARRIED_FACTOR_SESSIONS = 5

#: A-share prices are published to the fen (0.01 CNY), so two sessions are
#: continuous when the later bar's ``pre_close`` and the earlier bar's
#: ``close`` agree to that unit.  Anything larger is the exchange telling us a
#: corporate action happened.
CORPORATE_ACTION_PRICE_TOLERANCE = 0.01

_TOLERANCE_EPSILON = 1e-9


def number(value: Any) -> float | None:
    try:
        return float(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


@dataclass(frozen=True)
class FactorResolution:
    """One window's adjustment basis, or an explicit refusal to build one."""

    factors: tuple[float, ...] | None
    flags: tuple[str, ...]
    carried_sessions: int = 0

    @property
    def usable(self) -> bool:
        return self.factors is not None


def _refuse(flag: str) -> FactorResolution:
    return FactorResolution(None, (flag,), 0)


def resolve_factors(rows: list[dict[str, Any]]) -> FactorResolution:
    """Resolve one window's factors, carrying the last real one across a
    *trailing* gap when the bars themselves show no corporate action.

    Why a carry-forward is the honest reading, and why it is not the old
    ``1.0`` placeholder:

    * The placeholder was wrong **in absolute terms**: it claimed the
      cumulative factor *is* 1.0, so ``close * adj_factor`` silently became an
      unadjusted series that was not comparable with the adjusted history
      before it.  Carrying the last real factor keeps the same basis as the
      rest of the window, so every cross-session ratio in that window stays
      exactly as valid as it was on the last fetched day.
    * A corporate action is rare (a handful per day across ~5,500 symbols) and
      it is *not invisible*: the exchange republishes the session's
      ``pre_close`` at the adjusted level, so an ex-rights or ex-dividend day
      announces itself as ``pre_close != previous close``.  "Unchanged" is
      therefore the correct neutral assumption for a short trailing gap on a
      symbol whose bars show no such break, and the bars are the evidence, not
      the guess.
    * The assumption is bounded three ways: only a **trailing** gap (a NULL
      with real factors after it is a hole in fetched history, not a lagging
      fetch, and still fails closed), at most
      :data:`MAX_CARRIED_FACTOR_SESSIONS` sessions, and only when every
      carried session's ``pre_close`` is present and continuous.  A window
      whose bars do not carry ``pre_close`` at all proves nothing and fails
      closed exactly as before.

    Nothing here is ever written back: the carried factor lives in one
    request's research view.  ``quant.canonical_bars_daily.adj_factor`` stays
    NULL until tushare supplies the real cumulative factor, which is what
    ``tests/test_adjustment_factor_semantics_guard.py`` protects.
    """
    if not rows:
        return FactorResolution((), (), 0)
    resolved: list[float | None] = []
    for row in rows:
        factor = number(row.get("adj_factor"))
        resolved.append(factor if factor is not None and factor > 0 else None)
    if all(factor is not None for factor in resolved):
        return FactorResolution(tuple(float(factor) for factor in resolved), (), 0)  # type: ignore[arg-type]
    last_real = max((index for index, factor in enumerate(resolved) if factor is not None), default=-1)
    if last_real < 0:
        # No anchor inside the window at all.
        return _refuse(ADJUSTMENT_MISSING_FLAG)
    if any(resolved[index] is None for index in range(last_real)):
        # An interior hole: real factors exist after it, so this is missing
        # history rather than a fetch that has not caught up yet.
        return _refuse(ADJUSTMENT_MISSING_FLAG)
    carried_sessions = len(rows) - 1 - last_real
    if carried_sessions > MAX_CARRIED_FACTOR_SESSIONS:
        return _refuse(ADJUSTMENT_MISSING_FLAG)
    for index in range(last_real + 1, len(rows)):
        pre_close = number(rows[index].get("pre_close"))
        previous_close = number(rows[index - 1].get("close"))
        if pre_close is None or pre_close <= 0 or previous_close is None or previous_close <= 0:
            # Without the exchange's own continuity statement there is no
            # evidence that no corporate action happened.
            return _refuse(ADJUSTMENT_MISSING_FLAG)
        if abs(pre_close - previous_close) - CORPORATE_ACTION_PRICE_TOLERANCE > _TOLERANCE_EPSILON:
            return _refuse(CORPORATE_ACTION_UNRESOLVED_FLAG)
    anchor = float(resolved[last_real])  # type: ignore[arg-type]
    factors = tuple(float(factor) if factor is not None else anchor for factor in resolved)
    return FactorResolution(factors, (CARRIED_FORWARD_FLAG,), carried_sessions)


def carried_forward_sessions(rows: list[dict[str, Any]] | None) -> int:
    """Count the sessions in a prepared research view that were carried."""
    return sum(1 for row in rows or () if row.get("research_adj_factor_carried"))


def adjusted_value(row: dict[str, Any], field: str = "close") -> float | None:
    """Return a strict same-day adjusted research value.

    There is deliberately no raw-price fallback.  A missing factor is a data
    quality condition, not evidence that the raw series is continuous through
    corporate actions.  A single row is also not a window: the trailing-gap
    carry-forward in :func:`resolve_factors` needs a neighbouring session's
    close to test continuity against, so a caller holding one row keeps the
    strict answer.  Callers that do hold the window resolve it there and pass
    the resolved factor in as ``adj_factor``.
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

    A window whose only incomplete factors sit in a short trailing gap with no
    corporate-action signature is prepared on the carried factor and flagged
    ``adj_factor_carried_forward`` (see :func:`resolve_factors`); the carried
    rows carry ``research_adj_factor_carried`` so the count survives into the
    caller's payload.
    """
    if not rows:
        return [], []
    resolution = resolve_factors(rows)
    if resolution.factors is None:
        return None, list(resolution.flags)
    first_carried = len(rows) - resolution.carried_sessions
    prepared: list[dict[str, Any]] = []
    for index, row in enumerate(rows):
        item = dict(row)
        factor = resolution.factors[index]
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
        item["research_adj_factor"] = factor
        if resolution.carried_sessions and index >= first_carried:
            item["research_adj_factor_carried"] = True
        prepared.append(item)
    return prepared, list(resolution.flags)


__all__ = [
    "ADJUSTMENT_MISSING_FLAG",
    "CARRIED_FORWARD_FLAG",
    "CORPORATE_ACTION_PRICE_TOLERANCE",
    "CORPORATE_ACTION_UNRESOLVED_FLAG",
    "FactorResolution",
    "MAX_CARRIED_FACTOR_SESSIONS",
    "adjusted_bars",
    "adjusted_value",
    "carried_forward_sessions",
    "number",
    "resolve_factors",
]
