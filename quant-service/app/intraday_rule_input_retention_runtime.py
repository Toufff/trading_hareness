"""Application adapter for bounded intraday rule-input evidence retention."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo


@dataclass(frozen=True)
class IntradayRuleInputRetentionDependencies:
    database: Any
    run_database: Callable[..., Awaitable[Any]]
    rule_input_retention_days: Callable[[], int]
    ephemeral_signal_retention_days: Callable[[], int]
    prune_rule_inputs: Callable[..., Any]
    prune_ephemeral_events: Callable[..., Any]


class IntradayRuleInputRetentionRuntime:
    """Run at most one retention transaction for each Shanghai exchange date."""

    def __init__(self, dependencies: IntradayRuleInputRetentionDependencies) -> None:
        self._dependencies = dependencies
        self._pruned_on: date | None = None

    async def prune_if_due(self, observed_at: datetime) -> None:
        local_date = observed_at.astimezone(ZoneInfo("Asia/Shanghai")).date()
        if self._pruned_on == local_date:
            return
        rule_input_cutoff = observed_at - timedelta(days=self._dependencies.rule_input_retention_days())
        event_cutoff = observed_at - timedelta(days=self._dependencies.ephemeral_signal_retention_days())

        def prune() -> None:
            with self._dependencies.database.transaction() as connection:
                self._dependencies.prune_rule_inputs(connection, cutoff=rule_input_cutoff)
                self._dependencies.prune_ephemeral_events(connection, cutoff=event_cutoff)

        await self._dependencies.run_database(prune)
        self._pruned_on = local_date


__all__ = [
    "IntradayRuleInputRetentionDependencies",
    "IntradayRuleInputRetentionRuntime",
]
