"""Composition helpers kept out of the ASGI root."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

from ..trade_discipline.alerts_runtime import (
    DisciplineAlertRuntimeDependencies, alert_account_key as discipline_account_key,
    alert_interval_seconds, alerts_enabled as discipline_alerts_enabled, run_discipline_alert_loop,
)
from .runtime import (
    IntradayAdvisoryDependencies, account_key as advisory_account_key,
    enabled as intraday_advisory_enabled, run_intraday_advisory_loop,
)


def build_notification_loops(*, database: Any, run_database: Any, fetch_minutes: Any,
                             fetch_quotes: Any, fetch_indices: Any, post_text: Any, post_card: Any, session_open: Any,
                             dashboard_url: Any) -> tuple[Any, Any]:
    now = lambda: datetime.now(timezone.utc).astimezone(ZoneInfo("Asia/Shanghai"))  # noqa: E731
    discipline = lambda: run_discipline_alert_loop(DisciplineAlertRuntimeDependencies(  # noqa: E731
        database=database, run_database=run_database, fetch_minutes=fetch_minutes, post_text=post_text,
        session_open=session_open, dashboard_url=dashboard_url, account_key=discipline_account_key,
        now=now, interval_seconds=alert_interval_seconds))
    advisory = lambda: run_intraday_advisory_loop(IntradayAdvisoryDependencies(  # noqa: E731
        database=database, run_database=run_database, fetch_quotes=fetch_quotes, fetch_indices=fetch_indices,
        post_text=post_text, post_card=post_card,
        session_open=session_open, now=now, account_key=advisory_account_key))
    return discipline, advisory


__all__ = ["build_notification_loops", "discipline_alerts_enabled", "intraday_advisory_enabled"]
