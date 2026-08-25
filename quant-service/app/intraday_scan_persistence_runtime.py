"""Runtime adapter for one atomic intraday scan evidence write."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any
import uuid

from .intraday_scan_signal_persistence import (
    IntradayScanPersistenceServiceDependencies,
    persist_scan_transaction,
)


@dataclass(frozen=True)
class IntradayScanPersistenceRuntime:
    """Bind one declared persistence graph to the scan service callback shape."""

    dependencies: IntradayScanPersistenceServiceDependencies
    persist_transaction: Callable[..., list[dict[str, Any]]] = persist_scan_transaction

    def persist(
        self,
        scan_id: uuid.UUID,
        observed_at: datetime,
        selected_symbols: list[str],
        source_status: dict[str, Any],
        watches: list[dict[str, Any]],
        quotes: dict[str, dict[str, Any]],
        all_a_rows: list[dict[str, Any]],
        quote_latency_ms: int,
        tushare_minutes: dict[str, dict[str, Any]],
        surge_features: dict[str, dict[str, Any]],
        peer_contexts: dict[str, dict[str, Any]],
        fast_confirmations: dict[str, dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Keep all scan evidence under the pre-composed single transaction."""
        return self.persist_transaction(
            self.dependencies,
            scan_id=scan_id,
            observed_at=observed_at,
            selected_symbols=selected_symbols,
            source_status=source_status,
            watches=watches,
            quotes=quotes,
            all_a_rows=all_a_rows,
            quote_latency_ms=quote_latency_ms,
            tushare_minutes=tushare_minutes,
            surge_features=surge_features,
            peer_contexts=peer_contexts,
            fast_confirmations=fast_confirmations,
        )


__all__ = ["IntradayScanPersistenceRuntime"]
