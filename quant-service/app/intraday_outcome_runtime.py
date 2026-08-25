"""Transactional runtime adapter for research-only intraday outcome settlement."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any


@dataclass(frozen=True)
class IntradayOutcomeRuntimeDependencies:
    database: Any
    outcome_cutoff: Callable[[date | None], datetime]
    refresh_attributions: Callable[[Any], int]
    settle: Callable[..., dict[str, Any]]
    horizons: tuple[tuple[str, int], ...]
    direction_for: Callable[[str], int | None]
    metrics_for: Callable[..., Any]
    decimal_or_none: Callable[[Any], Any]
    barrier_spec_type: Callable[[], Any]
    triple_barrier_label: Callable[..., Any]
    persist_barrier_outcome: Callable[..., Any]
    return_decomposition: Callable[..., dict[str, Any]]
    json_safe: Callable[[Any], Any]


class IntradayOutcomeRuntime:
    """Settle retained signal evidence and its attribution in one transaction."""

    def __init__(self, dependencies: IntradayOutcomeRuntimeDependencies) -> None:
        self._dependencies = dependencies

    def recompute(self, as_of_date: date | None = None) -> dict[str, Any]:
        dependencies = self._dependencies
        cutoff = dependencies.outcome_cutoff(as_of_date)
        with dependencies.database.transaction() as connection:
            attribution_backfilled = dependencies.refresh_attributions(connection, cutoff=cutoff)
            result = dependencies.settle(
                connection,
                as_of_date,
                cutoff=cutoff,
                horizons=dependencies.horizons,
                direction_for=dependencies.direction_for,
                metrics_for=dependencies.metrics_for,
                decimal_or_none=dependencies.decimal_or_none,
                barrier_spec_type=dependencies.barrier_spec_type,
                triple_barrier_label=dependencies.triple_barrier_label,
                persist_barrier_outcome=dependencies.persist_barrier_outcome,
                return_decomposition=dependencies.return_decomposition,
                json_safe=dependencies.json_safe,
            )
        return {**result, "attribution_backfilled": attribution_backfilled}


__all__ = ["IntradayOutcomeRuntime", "IntradayOutcomeRuntimeDependencies"]
