"""Read-only daily control-plane readiness policy.

Daily adjustment factors and price limits are equities-only controls.  Index
rows may coexist with the full-market daily table, but they intentionally do
not have ``adj_factor`` or ``stk_limit`` records and must not make the equity
decision gate appear unhealthy.
"""

from __future__ import annotations

import math
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from datetime import date
from typing import Any, Mapping

from .full_market_daily_controls_sync import sync as sync_full_market_daily_controls_isolated


MINIMUM_ALL_A_COVERAGE_RATIO = 0.95

#: Exchanges whose coverage decides the equity readiness gate.  ``all_a``
#: deliberately includes the Beijing Stock Exchange (``universe_history`` keeps
#: ``BJ`` members), but no configured daily-bar provider publishes a BJ
#: cross-section yet.  Counting BJ in the denominator turned a complete SH/SZ
#: session into a blocked one the moment 312 ``920*`` codes joined the
#: universe, so BJ stays observed-but-ungated until a BJ daily source exists.
GATED_EXCHANGES = ('SH', 'SZ')

#: The *only* exchanges allowed to sit outside the gate.  Both the readiness
#: payload and :func:`daily_row_count` decide membership from this one tuple by
#: exclusion, never by listing the gated exchanges: an inclusion list silently
#: drops any suffix nobody enumerated (an unsuffixed symbol, a future board)
#: from the expected population on one side and keeps it on the other, so the
#: sync and the gate could disagree about the same session.
UNGATED_EXCHANGES = ('BJ',)

#: ``|expected_delta|`` above this share of the expected population is a
#: universe drift the reason text has to name, not background churn.
EXPECTED_DELTA_REPORT_RATIO = 0.02

#: The daily controls sync considers a longhuvip cross-section "usable" once
#: it covers at least this many symbols and its factor/limit controls reach
#: the same minimum coverage ratio as the equity readiness gate above.
LONGHU_MINIMUM_DAILY_ROWS = 3500

#: Rows whose symbol carries no recognizable ``.SH``/``.SZ``/``.BJ`` suffix are
#: bucketed here and stay inside the gate: an unclassifiable symbol must not be
#: able to leave the decision gate by hiding in an ungated bucket.
UNKNOWN_EXCHANGE = 'UNKNOWN'


EQUITY_DAILY_CONTROL_STATUS_SQL = """WITH equity_bars AS (
       SELECT bar.symbol,bar.trading_date,bar.adj_factor,bar.limit_up,bar.limit_down,
              coalesce(nullif(upper(split_part(bar.symbol,'.',2)),''),'UNKNOWN') AS exchange
         FROM quant.canonical_bars_daily bar
        WHERE bar.quality_status IN ('fresh','partial')
          AND EXISTS (
            SELECT 1 FROM quant.universe_membership_history membership
             WHERE membership.universe_key='all_a' AND membership.symbol=bar.symbol
               AND membership.effective_from<=bar.trading_date
               AND (membership.effective_to IS NULL OR membership.effective_to>=bar.trading_date)
          )
   ), latest AS (
       SELECT max(trading_date) AS trading_date FROM equity_bars
   ), previous AS (
       SELECT max(prior.trading_date) AS trading_date
         FROM equity_bars prior CROSS JOIN latest
        WHERE prior.trading_date<latest.trading_date
   ), expected AS (
       SELECT latest.trading_date,
              coalesce(nullif(upper(split_part(membership.symbol,'.',2)),''),'UNKNOWN') AS exchange,
              count(DISTINCT membership.symbol)::int AS expected_daily_rows
         FROM latest
         LEFT JOIN quant.universe_membership_history membership
           ON membership.universe_key='all_a'
          AND membership.effective_from<=latest.trading_date
          AND (membership.effective_to IS NULL OR membership.effective_to>=latest.trading_date)
        GROUP BY latest.trading_date,2
   ), expected_previous AS (
       SELECT previous.trading_date,count(DISTINCT membership.symbol)::int AS expected_daily_rows
         FROM previous
         LEFT JOIN quant.universe_membership_history membership
           ON membership.universe_key='all_a'
          AND membership.effective_from<=previous.trading_date
          AND (membership.effective_to IS NULL OR membership.effective_to>=previous.trading_date)
        GROUP BY previous.trading_date
   ), expected_sources AS (
       SELECT jsonb_object_agg(grouped.source,grouped.symbols) AS sources FROM (
           SELECT coalesce(membership.source,'unknown') AS source,
                  count(DISTINCT membership.symbol)::int AS symbols
             FROM latest JOIN quant.universe_membership_history membership
               ON membership.universe_key='all_a'
              AND membership.effective_from=latest.trading_date
              AND (membership.effective_to IS NULL OR membership.effective_to>=latest.trading_date)
            GROUP BY 1) grouped
   ) SELECT expected.trading_date,expected.exchange,expected.expected_daily_rows,
       count(DISTINCT bar.symbol)::int AS daily_rows,
       count(DISTINCT bar.symbol) FILTER (WHERE bar.adj_factor IS NOT NULL)::int AS adjustment_rows,
       count(DISTINCT bar.symbol) FILTER (WHERE bar.limit_up IS NOT NULL AND bar.limit_down IS NOT NULL)::int AS limit_rows,
       expected_previous.trading_date AS expected_previous_trading_day,
       expected_previous.expected_daily_rows AS expected_previous_daily_rows,
       expected_sources.sources AS expected_sources
     FROM expected
     LEFT JOIN equity_bars bar
       ON bar.trading_date=expected.trading_date AND bar.exchange=expected.exchange
     LEFT JOIN expected_previous ON TRUE
     LEFT JOIN expected_sources ON TRUE
    GROUP BY expected.trading_date,expected.exchange,expected.expected_daily_rows,
             expected_previous.trading_date,expected_previous.expected_daily_rows,expected_sources.sources
    ORDER BY expected.exchange"""


def status_query(trade_date: date | None = None) -> tuple[str, tuple]:
    """Historical repairs verify their requested date, not a newer partial day."""
    if trade_date is None:
        return EQUITY_DAILY_CONTROL_STATUS_SQL, ()
    return EQUITY_DAILY_CONTROL_STATUS_SQL.replace(
        'SELECT max(trading_date) AS trading_date FROM equity_bars',
        'SELECT %s::date AS trading_date'), (trade_date,)


def _absent_payload() -> dict[str, Any]:
    return {
        "state": "absent", "trade_date": None,
        "daily_rows": 0, "expected_daily_rows": 0, "minimum_required_rows": 0,
        "coverage_ratio": 0.0, "adjustment_rows": 0, "limit_rows": 0,
        "by_exchange": {}, "gating_exchanges": list(GATED_EXCHANGES), "ungated_exchanges": [],
        "all_a": {"expected_daily_rows": 0, "daily_rows": 0},
        "expected_previous_trading_day": None, "expected_previous_daily_rows": None,
        "expected_delta": None, "expected_sources": {},
        "reason": "no canonical equity daily bars",
    }


def _format_expected_sources(sources: Mapping[str, Any]) -> str:
    ordered = sorted(sources.items(), key=lambda item: (-int(item[1]), str(item[0])))
    return ', '.join(f"{name} {int(count)}" for name, count in ordered)


def status_payload(rows: Iterable[Mapping[str, Any]] | None) -> dict[str, Any]:
    """Return an explicit fail-closed readiness result from the per-exchange rows.

    The gate excludes :data:`UNGATED_EXCHANGES` only.  Every other exchange is
    still reported in ``by_exchange``/``ungated_exchanges`` so a report can show
    that, say, BJ has no daily source, instead of silently diluting the SH/SZ
    coverage ratio that actually decides whether the session is usable.

    The SQL returns one row per exchange, so a caller that still says
    ``fetchone()`` would hand over a single arbitrary exchange (``BJ`` first,
    alphabetically) and get a confidently wrong ``blocked`` verdict.  That
    mistake raises here instead of being papered over with a single-row branch.
    """
    if rows is None:
        return _absent_payload()
    if isinstance(rows, Mapping):
        raise TypeError(
            "status_payload takes every per-exchange row (cursor.fetchall()), not a single row")
    dated = [row for row in rows if row and row.get("trading_date") is not None]
    if not dated:
        return _absent_payload()

    by_exchange: dict[str, dict[str, Any]] = {}
    for row in dated:
        exchange = str(row.get("exchange") or UNKNOWN_EXCHANGE).upper()
        bucket = by_exchange.setdefault(
            exchange, {"expected": 0, "daily": 0, "adjustment": 0, "limit": 0})
        bucket["daily"] += int(row.get("daily_rows") or 0)
        bucket["expected"] += int(row.get("expected_daily_rows") or row.get("daily_rows") or 0)
        bucket["adjustment"] += int(row.get("adjustment_rows") or 0)
        bucket["limit"] += int(row.get("limit_rows") or 0)
    for exchange, bucket in by_exchange.items():
        bucket["gated"] = exchange not in UNGATED_EXCHANGES
        bucket["ratio"] = round(bucket["daily"] / bucket["expected"], 4) if bucket["expected"] else 0.0

    gated = {name: bucket for name, bucket in by_exchange.items() if bucket["gated"]}
    ungated = [
        {"exchange": name, "expected": bucket["expected"], "daily": bucket["daily"],
         "ratio": bucket["ratio"]}
        for name, bucket in sorted(by_exchange.items()) if not bucket["gated"]
    ]

    daily_rows = sum(bucket["daily"] for bucket in gated.values())
    expected_daily_rows = sum(bucket["expected"] for bucket in gated.values())
    adjustment_rows = sum(bucket["adjustment"] for bucket in gated.values())
    limit_rows = sum(bucket["limit"] for bucket in gated.values())
    all_a_expected = sum(bucket["expected"] for bucket in by_exchange.values())
    all_a_daily = sum(bucket["daily"] for bucket in by_exchange.values())

    minimum_required_rows = math.ceil(expected_daily_rows * MINIMUM_ALL_A_COVERAGE_RATIO)
    coverage_ratio = round(daily_rows / expected_daily_rows, 4) if expected_daily_rows else 0.0
    cross_section_ready = daily_rows >= minimum_required_rows
    controls_ready = adjustment_rows == daily_rows and limit_rows == daily_rows
    ready = daily_rows > 0 and cross_section_ready and controls_ready

    first = dated[0]
    previous_day = first.get("expected_previous_trading_day")
    previous_expected = first.get("expected_previous_daily_rows")
    previous_expected = int(previous_expected) if previous_expected is not None else None
    expected_delta = all_a_expected - previous_expected if previous_expected is not None else None
    sources = {str(k): int(v) for k, v in dict(first.get("expected_sources") or {}).items()}
    drift = (
        expected_delta is not None
        and abs(expected_delta) > EXPECTED_DELTA_REPORT_RATIO * (all_a_expected or 1)
    )

    gated_label = '+'.join(sorted(gated)) or '+'.join(GATED_EXCHANGES)
    parts = [
        f"{gated_label} {daily_rows}/{expected_daily_rows}="
        f"{(daily_rows / expected_daily_rows if expected_daily_rows else 0.0):.1%} "
        f"{'ready' if cross_section_ready and controls_ready else 'blocked'}"
    ]
    if not cross_section_ready:
        parts.append(
            f"低于 {MINIMUM_ALL_A_COVERAGE_RATIO:.0%} 的 point-in-time all-A 门槛"
            f"（至少 {minimum_required_rows} 只）")
    elif not controls_ready:
        parts.append(
            f"missing same-date adjustment or limit controls："
            f"复权 {adjustment_rows}/{daily_rows}、涨跌停 {limit_rows}/{daily_rows}")
    parts.extend(
        f"{item['exchange']} {item['daily']}/{item['expected']} 未参与门槛" for item in ungated)
    if drift:
        source_text = _format_expected_sources(sources)
        # The delta is a net difference between two point-in-time populations;
        # the source grouping counts membership rows that *start* on this date,
        # re-affirmations of an existing member included.  The two are related
        # but never equal, so they are labelled as two separate quantities
        # rather than joined into one arithmetic a reader would try to balance.
        parts.append(
            f"all_a 预期较上一交易日 {expected_delta:+d}"
            + (f"（当日新增 {sum(sources.values())}，来源分组：{source_text}）" if source_text else ""))
    reason = '；'.join(parts) if (not ready or drift) else None

    return {
        "state": "ready" if ready else "blocked",
        "trade_date": str(first["trading_date"]),
        "daily_rows": daily_rows,
        "expected_daily_rows": expected_daily_rows,
        "minimum_required_rows": minimum_required_rows,
        "coverage_ratio": coverage_ratio,
        "adjustment_rows": adjustment_rows,
        "limit_rows": limit_rows,
        "by_exchange": by_exchange,
        "gating_exchanges": [name for name in GATED_EXCHANGES],
        "ungated_exchanges": ungated,
        "all_a": {"expected_daily_rows": all_a_expected, "daily_rows": all_a_daily},
        "expected_previous_trading_day": str(previous_day) if previous_day is not None else None,
        "expected_previous_daily_rows": previous_expected,
        "expected_delta": expected_delta,
        "expected_sources": sources,
        "reason": reason,
    }


def daily_row_count(database: Any, trade_date: date) -> int:
    """Return a usable all-A daily cross-section count, otherwise fail closed.

    The controls synchronizer must never make a partially fetched daily date
    appear ready merely because its local rows have matching controls.  The
    expected population is the point-in-time all-A membership for this date,
    minus :data:`UNGATED_EXCHANGES` for the same reason the readiness gate
    excludes them: an exchange with no daily source must not shrink this ratio.
    Exclusion, not inclusion, is what keeps this count and
    :func:`status_payload` from disagreeing about an unclassifiable symbol.
    """
    ungated = list(UNGATED_EXCHANGES)
    with database.transaction() as connection:
        row = connection.execute(
            f"""WITH expected AS (
                   SELECT count(DISTINCT symbol)::int AS expected_rows
                     FROM quant.universe_membership_history
                    WHERE universe_key='all_a' AND effective_from<=%s
                      AND (effective_to IS NULL OR effective_to>=%s)
                      AND coalesce(nullif(upper(split_part(symbol,'.',2)),''),'{UNKNOWN_EXCHANGE}')<>ALL(%s)
               ), actual AS (
                   SELECT count(DISTINCT bar.symbol)::int AS actual_rows
                     FROM quant.canonical_bars_daily bar
                     JOIN quant.universe_membership_history membership
                       ON membership.universe_key='all_a' AND membership.symbol=bar.symbol
                      AND membership.effective_from<=%s
                      AND (membership.effective_to IS NULL OR membership.effective_to>=%s)
                    WHERE bar.trading_date=%s AND bar.quality_status IN ('fresh','partial')
                      AND coalesce(nullif(upper(split_part(bar.symbol,'.',2)),''),'{UNKNOWN_EXCHANGE}')<>ALL(%s)
               ) SELECT expected_rows,actual_rows FROM expected CROSS JOIN actual""",
            (trade_date, trade_date, ungated, trade_date, trade_date, trade_date, ungated),
        ).fetchone()
    expected = int((row or {}).get("expected_rows") or 0)
    actual = int((row or {}).get("actual_rows") or 0)
    return actual if expected and actual >= math.ceil(expected * MINIMUM_ALL_A_COVERAGE_RATIO) else 0


@dataclass(frozen=True)
class DailyControlPlaneSyncDependencies:
    database: Any
    longhu_vendor_configured: Callable[[], bool]
    run_database: Callable[..., Awaitable[Any]]
    call_tushare_api: Callable[..., Awaitable[Any]]
    parse_tushare_date: Callable[[Any], date | None]
    persist_tushare_rows: Callable[..., Any]
    persist_blocked: Callable[[str, Exception], None]
    safe_error_detail: Callable[[str, int], str]
    executor_saturated_error: type[BaseException]
    record_provider_success: Callable[..., None]
    record_provider_failure: Callable[..., None]
    record_provider_api_capability: Callable[..., None]


def _longhu_control_status(database: Any, trade_date: date) -> dict[str, Any] | None:
    with database.transaction() as connection:
        row = connection.execute(
            """WITH daily AS (
                   SELECT count(*)::int AS rows FROM quant.canonical_bars_daily
                    WHERE trading_date=%s AND selected_provider='longhuvip_composite'
                 ), factors AS (
                   SELECT count(DISTINCT symbol)::int AS rows FROM quant.daily_adjustment_factors
                    WHERE trading_date=%s AND provider='longhuvip_composite'
                 ), limits AS (
                   SELECT count(DISTINCT symbol)::int AS rows FROM quant.daily_trade_limits
                    WHERE trading_date=%s AND provider='longhuvip_composite'
                 ) SELECT daily.rows AS daily_rows,factors.rows AS factor_rows,
                          limits.rows AS limit_rows FROM daily,factors,limits""",
            (trade_date, trade_date, trade_date),
        ).fetchone()
    daily_rows = int((row or {}).get("daily_rows") or 0)
    factor_rows = int((row or {}).get("factor_rows") or 0)
    limit_rows = int((row or {}).get("limit_rows") or 0)
    minimum_control_rows = math.ceil(daily_rows * MINIMUM_ALL_A_COVERAGE_RATIO)
    if daily_rows >= LONGHU_MINIMUM_DAILY_ROWS and factor_rows >= minimum_control_rows and limit_rows >= minimum_control_rows:
        return {
            "status": "completed", "trade_date": str(trade_date),
            "provider": "longhuvip_composite", "expected_daily_rows": daily_rows,
            "rows": {"adj_factor": factor_rows, "stk_limit": limit_rows, "suspend_d": 0},
            "quality_note": (
                "adj_factor is same-day identity only; limits are board-rule derived and retain IPO/resumption warnings"
            ),
        }
    return None


async def sync_full_market_daily_controls(
    trade_date: date, dependencies: DailyControlPlaneSyncDependencies,
) -> dict[str, Any]:
    """Fill same-date adjustment, limit and suspension controls after daily sync."""
    if dependencies.longhu_vendor_configured():
        ready = await dependencies.run_database(
            lambda: _longhu_control_status(dependencies.database, trade_date))
        if ready:
            return ready
    return await sync_full_market_daily_controls_isolated(
        trade_date,
        expected_daily_rows=lambda date_: daily_row_count(dependencies.database, date_),
        call_tushare_api=dependencies.call_tushare_api,
        parse_date=dependencies.parse_tushare_date,
        persist_tushare_rows=dependencies.persist_tushare_rows,
        persist_blocked=dependencies.persist_blocked,
        run_database_blocking=dependencies.run_database,
        db=dependencies.database,
        safe_error_detail=dependencies.safe_error_detail,
        executor_saturated_error=dependencies.executor_saturated_error,
        record_provider_success=dependencies.record_provider_success,
        record_provider_failure=dependencies.record_provider_failure,
        record_provider_api_capability=dependencies.record_provider_api_capability,
    )


__all__ = [
    "EQUITY_DAILY_CONTROL_STATUS_SQL", "EXPECTED_DELTA_REPORT_RATIO", "GATED_EXCHANGES",
    "MINIMUM_ALL_A_COVERAGE_RATIO", "UNGATED_EXCHANGES", "status_payload", "status_query",
]
