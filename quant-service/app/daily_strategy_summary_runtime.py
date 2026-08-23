"""Production runtime adapter for the frontend-only daily strategy summary.

The summary is deliberately a durable UI receipt, not a second Feishu channel:
watched-stock alerts remain the only chat delivery path. Scheduler timing and
terminal semantics stay in ``daily_strategy_summary_scheduler``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Awaitable, Callable

from .daily_strategy_summary_scheduler import DailyStrategySummarySchedulerDependencies


@dataclass(frozen=True)
class DailyStrategySummaryRuntimeDependencies:
    database: Any
    run_database: Callable[..., Awaitable[Any]]
    build_summary: Callable[[date], dict[str, Any]]
    summary_text: Callable[[dict[str, Any], str | None], str]
    dashboard_url: Callable[[], str | None]
    json_safe: Callable[[Any], Any]
    json_value: Callable[[Any], Any]
    terminal_for_exchange_date: Callable[[Any, date], bool]
    calendar_open: Callable[[date], Awaitable[bool]]
    now: Callable[[], datetime]
    scheduler: Callable[[DailyStrategySummarySchedulerDependencies], Awaitable[None]]


async def run_daily_strategy_summary(
    exchange_date: date,
    dependencies: DailyStrategySummaryRuntimeDependencies,
) -> dict[str, Any]:
    """Persist one frontend-only daily receipt without external delivery."""
    summary = await dependencies.run_database(dependencies.build_summary, exchange_date)
    text = dependencies.summary_text(summary, dependencies.dashboard_url())

    def persist_frontend_only() -> None:
        with dependencies.database.transaction() as connection:
            connection.execute(
                """INSERT INTO quant.strategy_day_summaries(exchange_date,payload,message_text,delivery_status,error_message)
                   VALUES(%s,%s,%s,'suppressed','suppressed: Feishu is reserved for watched-stock strategy signals')
                   ON CONFLICT(exchange_date) DO UPDATE SET payload=EXCLUDED.payload,message_text=EXCLUDED.message_text,
                       delivery_status='suppressed',next_attempt_at=NULL,
                       error_message=EXCLUDED.error_message,updated_at=now()""",
                (exchange_date, dependencies.json_value(dependencies.json_safe(summary)), text),
            )
    await dependencies.run_database(persist_frontend_only)
    return {
        "status": "suppressed",
        "exchange_date": str(exchange_date),
        "summary": summary,
        "reason": "Feishu is reserved for watched-stock strategy signals",
    }


async def run_daily_strategy_summary_loop(
    dependencies: DailyStrategySummaryRuntimeDependencies,
) -> None:
    """Run the existing 19:15--19:30 scheduler with durable local receipts."""
    async def terminal_for_date(exchange_date: date) -> bool:
        def load() -> bool:
            with dependencies.database.transaction() as connection:
                return dependencies.terminal_for_exchange_date(connection, exchange_date)
        return bool(await dependencies.run_database(load, timeout_seconds=10))

    await dependencies.scheduler(DailyStrategySummarySchedulerDependencies(
        calendar_open=dependencies.calendar_open,
        terminal_for_date=terminal_for_date,
        run_summary=lambda exchange_date: run_daily_strategy_summary(exchange_date, dependencies),
        now=dependencies.now,
    ))


__all__ = [
    "DailyStrategySummaryRuntimeDependencies",
    "run_daily_strategy_summary",
    "run_daily_strategy_summary_loop",
]
