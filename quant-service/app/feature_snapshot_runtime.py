"""Runtime adapter for deterministic feature-snapshot materialization.

The repository owns SQL and feature construction; this module owns the
application-level transaction boundary and explicit dependency assembly.  It
does not know about HTTP, provider fetching, or recommendation promotion.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from typing import Any


@dataclass(frozen=True)
class FeatureSnapshotRuntimeDependencies:
    database: Any
    materialize: Callable[..., dict[str, Any]]
    feature_version: str
    number: Callable[[Any], float]
    market_regime: Callable[[Any, date], str]
    analyst_text_factor_summary: Callable[[Any, date], dict[str, Any]]
    latest_tushare_row: Callable[[Any, str, str, date], dict[str, Any] | None]
    analyst_feature: Callable[[Any, str, date], dict[str, Any]]


class FeatureSnapshotRuntime:
    """Materialize one local, research-only feature snapshot atomically."""

    def __init__(self, dependencies: FeatureSnapshotRuntimeDependencies) -> None:
        self._dependencies = dependencies

    def build(self, as_of_date: date, universe_key: str) -> dict[str, Any]:
        dependencies = self._dependencies
        with dependencies.database.transaction() as connection:
            return dependencies.materialize(
                connection,
                as_of_date,
                universe_key,
                feature_version=dependencies.feature_version,
                number=dependencies.number,
                market_regime=dependencies.market_regime,
                analyst_text_factor_summary=dependencies.analyst_text_factor_summary,
                latest_tushare_row=dependencies.latest_tushare_row,
                analyst_feature=dependencies.analyst_feature,
            )


__all__ = ["FeatureSnapshotRuntime", "FeatureSnapshotRuntimeDependencies"]
