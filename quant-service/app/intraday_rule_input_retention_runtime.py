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
    # The edge change journal is a delivery log: every row it carries also
    # lives in the table whose trigger produced it.  Left out of retention it
    # grew to 468k rows and 43% of the whole edge database by 2026-08-28,
    # pushing the hot budget to 99% so the storage guard stopped non-essential
    # capture for a whole session.  Optional so a deployment without the
    # journal keeps the original two-table behaviour.
    prune_change_journal: Callable[..., Any] | None = None
    change_journal_retention_days: Callable[[], int] | None = None


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
        journal_cutoff = self._journal_cutoff(observed_at)

        def prune() -> None:
            with self._dependencies.database.transaction() as connection:
                self._dependencies.prune_rule_inputs(connection, cutoff=rule_input_cutoff)
                self._dependencies.prune_ephemeral_events(connection, cutoff=event_cutoff)
                if journal_cutoff is not None:
                    self._dependencies.prune_change_journal(connection, cutoff=journal_cutoff)

        await self._dependencies.run_database(prune)
        self._pruned_on = local_date

    def _journal_cutoff(self, observed_at: datetime) -> datetime | None:
        """Resolve the journal horizon, or None when journal pruning is absent.

        Both the pruner and its retention window must be configured: half a
        configuration would either call nothing or call it with no bound.
        """
        prune = self._dependencies.prune_change_journal
        retention_days = self._dependencies.change_journal_retention_days
        if prune is None or retention_days is None:
            return None
        days = retention_days()
        return observed_at - timedelta(days=days) if days > 0 else None


__all__ = [
    "IntradayRuleInputRetentionDependencies",
    "IntradayRuleInputRetentionRuntime",
]
