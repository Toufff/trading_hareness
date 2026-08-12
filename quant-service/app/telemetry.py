"""Process-local Prometheus primitives for the quant research service.

The service is deliberately loopback-bound.  This module defines metrics only;
it never polls providers or writes database state, so scraping cannot consume
market-data quota or mutate research evidence.
"""

from prometheus_client import CONTENT_TYPE_LATEST, Counter, Gauge, Histogram, generate_latest


provider_requests_total = Counter(
    "quant_provider_requests_total",
    "Completed provider operations by outcome.",
    ["provider", "capability", "outcome"],
)
provider_latency_seconds = Histogram(
    "quant_provider_latency_seconds",
    "Observed provider latency when measured.",
    ["provider", "capability"],
)
provider_shared_rate_limit_wait_seconds = Histogram(
    "quant_provider_shared_rate_limit_wait_seconds",
    "Local wait before a cross-process provider reservation may start.",
    ["provider"],
)
provider_shared_rate_limit_rejections_total = Counter(
    "quant_provider_shared_rate_limit_rejections_total",
    "Requests rejected before upstream work because the shared provider queue exceeded its wait budget.",
    ["provider"],
)
db_pool_connections = Gauge(
    "quant_db_pool_connections",
    "Current database pool connections.",
    ["state"],
)
db_blocking_tasks = Gauge(
    "quant_db_blocking_tasks",
    "Legacy synchronous database work submitted to the bounded executor.",
    ["state"],
)
bounded_executor_tasks = Gauge(
    "quant_bounded_executor_tasks",
    "Actual worker occupancy for bounded blocking executors.",
    ["executor", "state"],
)
bounded_executor_rejections_total = Counter(
    "quant_bounded_executor_rejections_total",
    "Blocking work rejected because an executor's bounded queue is full.",
    ["executor"],
)
provider_circuit_open = Gauge(
    "quant_provider_circuit_open",
    "Providers currently skipped by circuit breaker.",
)
background_loop_restarts_total = Counter(
    "quant_background_loop_restarts_total",
    "Unexpected background-loop exits restarted by the runtime supervisor.",
    ["loop"],
)
intraday_scan_duration_seconds = Histogram(
    "quant_intraday_scan_duration_seconds",
    "End-to-end duration of one bounded intraday watchlist scan.",
    ["outcome"],
)

__all__ = [
    "CONTENT_TYPE_LATEST",
    "bounded_executor_tasks",
    "background_loop_restarts_total",
    "bounded_executor_rejections_total",
    "db_blocking_tasks",
    "db_pool_connections",
    "generate_latest",
    "intraday_scan_duration_seconds",
    "provider_circuit_open",
    "provider_latency_seconds",
    "provider_requests_total",
    "provider_shared_rate_limit_rejections_total",
    "provider_shared_rate_limit_wait_seconds",
]
