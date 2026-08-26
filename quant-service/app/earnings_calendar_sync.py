"""Ingest the scheduled reporting calendar and prior earnings guidance.

Every selection strategy in this codebase worked from price and volume alone,
so a stock whose interim report is scheduled for tomorrow was indistinguishable
from one with no scheduled event.  That is the single largest blind spot found
while reviewing three names that limit-up'd on 2026-08-26: two of them had
their half-year report registered for exactly that date, visible in the
exchange calendar the day before.

Three Tushare cross-sections are ingested per reporting period:

``disclosure_date``  ``pre_date`` is the exchange-registered scheduled date,
                     known days in advance; ``actual_date`` fills in once the
                     report lands.  Storing both is what keeps "as of the
                     previous close, who was scheduled for tomorrow"
                     answerable after the fact rather than only in hindsight.
``forecast``         业绩预告 - a guidance range published ahead of the report.
``express``          业绩快报 - preliminary actual results, also ahead of it.

The last two are stored to establish *what the market already knew*, not to
predict a report's contents.  Measured over 2026-07-20..2026-08-25 across 27
sessions (see disclosure_day_watch.py), liquid scheduled disclosers carrying
prior guidance limit-up at 1.60% - indistinguishable from the 1.65% base rate -
while those with no prior guidance reach 3.77%.  Guidance is priced when it is
published, so it removes the surprise rather than signalling one.

A period is fetched whole in one bounded call per API; none of these three
supports offset paging across every configured provider, so partial results
are rejected instead of being stitched into a synthetic cross-section.
"""

from __future__ import annotations

import hashlib
import json
from datetime import date, datetime, timezone
from typing import Any, Awaitable, Callable

from psycopg.types.json import Json


CALENDAR_APIS = ("disclosure_date", "forecast", "express")
#: A report cannot be scheduled for a period that has not ended yet, and the
#: calendar for a just-ended period takes days to populate.
PERIOD_SETTLE_DAYS = 10


def reporting_period(as_of_date: date, *, settle_days: int = PERIOD_SETTLE_DAYS) -> date:
    """Return the most recent quarter end whose calendar is worth fetching.

    Quarter ends are the only valid ``period``/``end_date`` values for these
    APIs.  A quarter that ended within ``settle_days`` is skipped because its
    disclosure calendar is not registered yet, and asking for it returns an
    almost-empty cross-section that would look like a provider failure.
    """
    year, quarter_ends = as_of_date.year, ((3, 31), (6, 30), (9, 30), (12, 31))
    candidates = [date(year - 1, 12, 31)] + [date(year, month, day) for month, day in quarter_ends]
    eligible = [period for period in candidates if (as_of_date - period).days >= settle_days]
    return max(eligible)


def _number(value: Any) -> float | None:
    try:
        return float(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _text(value: Any, limit: int = 4000) -> str | None:
    text = str(value).strip() if value not in (None, "") else ""
    return text[:limit] or None


def normalize_disclosure_rows(rows: list[dict[str, Any]], period: date,
                              parse_date: Callable[[Any], date | None]) -> list[dict[str, Any]]:
    """Keep one row per symbol for the requested period only."""
    by_symbol: dict[str, dict[str, Any]] = {}
    for row in rows:
        symbol = str(row.get("ts_code") or "").upper()
        if not symbol or parse_date(row.get("end_date")) != period:
            continue
        by_symbol[symbol] = {
            "symbol": symbol, "period": period,
            "pre_date": parse_date(row.get("pre_date")),
            "actual_date": parse_date(row.get("actual_date")),
            "modify_date": parse_date(row.get("modify_date")),
            "raw": dict(row),
        }
    return list(by_symbol.values())


def normalize_forecast_rows(rows: list[dict[str, Any]], period: date,
                            parse_date: Callable[[Any], date | None]) -> list[dict[str, Any]]:
    normalized: dict[tuple[str, date], dict[str, Any]] = {}
    for row in rows:
        symbol = str(row.get("ts_code") or "").upper()
        ann_date = parse_date(row.get("ann_date"))
        if not symbol or ann_date is None or parse_date(row.get("end_date")) != period:
            continue
        normalized[(symbol, ann_date)] = {
            "symbol": symbol, "period": period, "ann_date": ann_date,
            "forecast_type": _text(row.get("type"), 64),
            "p_change_min": _number(row.get("p_change_min")), "p_change_max": _number(row.get("p_change_max")),
            "net_profit_min": _number(row.get("net_profit_min")), "net_profit_max": _number(row.get("net_profit_max")),
            "last_parent_net": _number(row.get("last_parent_net")),
            "first_ann_date": parse_date(row.get("first_ann_date")),
            "summary": _text(row.get("summary")), "change_reason": _text(row.get("change_reason")),
            "raw": dict(row),
        }
    return list(normalized.values())


def normalize_express_rows(rows: list[dict[str, Any]], period: date,
                           parse_date: Callable[[Any], date | None]) -> list[dict[str, Any]]:
    normalized: dict[tuple[str, date], dict[str, Any]] = {}
    for row in rows:
        symbol = str(row.get("ts_code") or "").upper()
        ann_date = parse_date(row.get("ann_date"))
        if not symbol or ann_date is None or parse_date(row.get("end_date")) != period:
            continue
        normalized[(symbol, ann_date)] = {
            "symbol": symbol, "period": period, "ann_date": ann_date,
            "revenue": _number(row.get("revenue")), "operate_profit": _number(row.get("operate_profit")),
            "total_profit": _number(row.get("total_profit")), "n_income": _number(row.get("n_income")),
            "total_assets": _number(row.get("total_assets")), "diluted_eps": _number(row.get("diluted_eps")),
            "diluted_roe": _number(row.get("diluted_roe")), "yoy_net_profit": _number(row.get("yoy_net_profit")),
            "perf_summary": _text(row.get("perf_summary")), "raw": dict(row),
        }
    return list(normalized.values())


NORMALIZERS: dict[str, Callable[..., list[dict[str, Any]]]] = {
    "disclosure_date": normalize_disclosure_rows,
    "forecast": normalize_forecast_rows,
    "express": normalize_express_rows,
}


def persist_disclosure_schedule(connection: Any, rows: list[dict[str, Any]], provider: str,
                                available_at: datetime) -> int:
    for row in rows:
        connection.execute(
            """INSERT INTO quant.disclosure_schedule(
                    symbol,period,provider,pre_date,actual_date,modify_date,available_at,raw)
               SELECT %s,%s,%s,%s,%s,%s,%s,%s
                WHERE EXISTS(SELECT 1 FROM quant.instruments WHERE symbol=%s)
               ON CONFLICT(symbol,period,provider) DO UPDATE SET
                 pre_date=EXCLUDED.pre_date,actual_date=EXCLUDED.actual_date,
                 modify_date=EXCLUDED.modify_date,available_at=EXCLUDED.available_at,raw=EXCLUDED.raw""",
            (row["symbol"], row["period"], provider, row["pre_date"], row["actual_date"],
             row["modify_date"], available_at, Json(row["raw"]), row["symbol"]),
        )
    return len(rows)


def persist_earnings_forecasts(connection: Any, rows: list[dict[str, Any]], provider: str,
                               available_at: datetime) -> int:
    for row in rows:
        connection.execute(
            """INSERT INTO quant.earnings_forecasts(
                    symbol,period,ann_date,provider,forecast_type,p_change_min,p_change_max,
                    net_profit_min,net_profit_max,last_parent_net,first_ann_date,summary,change_reason,
                    available_at,raw)
               SELECT %s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s
                WHERE EXISTS(SELECT 1 FROM quant.instruments WHERE symbol=%s)
               ON CONFLICT(symbol,period,ann_date,provider) DO UPDATE SET
                 forecast_type=EXCLUDED.forecast_type,p_change_min=EXCLUDED.p_change_min,
                 p_change_max=EXCLUDED.p_change_max,net_profit_min=EXCLUDED.net_profit_min,
                 net_profit_max=EXCLUDED.net_profit_max,last_parent_net=EXCLUDED.last_parent_net,
                 first_ann_date=EXCLUDED.first_ann_date,summary=EXCLUDED.summary,
                 change_reason=EXCLUDED.change_reason,available_at=EXCLUDED.available_at,raw=EXCLUDED.raw""",
            (row["symbol"], row["period"], row["ann_date"], provider, row["forecast_type"],
             row["p_change_min"], row["p_change_max"], row["net_profit_min"], row["net_profit_max"],
             row["last_parent_net"], row["first_ann_date"], row["summary"], row["change_reason"],
             available_at, Json(row["raw"]), row["symbol"]),
        )
    return len(rows)


def persist_earnings_express(connection: Any, rows: list[dict[str, Any]], provider: str,
                             available_at: datetime) -> int:
    for row in rows:
        connection.execute(
            """INSERT INTO quant.earnings_express(
                    symbol,period,ann_date,provider,revenue,operate_profit,total_profit,n_income,
                    total_assets,diluted_eps,diluted_roe,yoy_net_profit,perf_summary,available_at,raw)
               SELECT %s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s
                WHERE EXISTS(SELECT 1 FROM quant.instruments WHERE symbol=%s)
               ON CONFLICT(symbol,period,ann_date,provider) DO UPDATE SET
                 revenue=EXCLUDED.revenue,operate_profit=EXCLUDED.operate_profit,
                 total_profit=EXCLUDED.total_profit,n_income=EXCLUDED.n_income,
                 total_assets=EXCLUDED.total_assets,diluted_eps=EXCLUDED.diluted_eps,
                 diluted_roe=EXCLUDED.diluted_roe,yoy_net_profit=EXCLUDED.yoy_net_profit,
                 perf_summary=EXCLUDED.perf_summary,available_at=EXCLUDED.available_at,raw=EXCLUDED.raw""",
            (row["symbol"], row["period"], row["ann_date"], provider, row["revenue"], row["operate_profit"],
             row["total_profit"], row["n_income"], row["total_assets"], row["diluted_eps"],
             row["diluted_roe"], row["yoy_net_profit"], row["perf_summary"], available_at,
             Json(row["raw"]), row["symbol"]),
        )
    return len(rows)


PERSISTERS: dict[str, Callable[..., int]] = {
    "disclosure_date": persist_disclosure_schedule,
    "forecast": persist_earnings_forecasts,
    "express": persist_earnings_express,
}


async def sync(
    as_of_date: date,
    *,
    call_tushare_api: Callable[..., Awaitable[Any]],
    parse_date: Callable[[Any], date | None],
    run_database_blocking: Callable[..., Awaitable[Any]],
    db: Any,
    safe_error_detail: Callable[[str, int], str],
    period: date | None = None,
) -> dict[str, Any]:
    """Fetch one reporting period's calendar and guidance, then promote it whole.

    Each API is independent: a failure of one is reported and skipped rather
    than blocking the others, because the disclosure calendar is useful on its
    own and guidance is useful on its own.  An empty ``express`` response is
    legitimate - very few companies publish one - so emptiness is never
    treated as a failure here.
    """
    target_period = period or reporting_period(as_of_date)
    stamp = target_period.strftime("%Y%m%d")
    observed_at = datetime.now(timezone.utc)
    fetched: dict[str, Any] = {}
    errors: dict[str, str] = {}
    for api_name in CALENDAR_APIS:
        params = {"end_date": stamp} if api_name == "disclosure_date" else {"period": stamp}
        try:
            result = await call_tushare_api(api_name, params, None, "auto")
        except Exception as error:  # one API's outage must not hide the others
            errors[api_name] = safe_error_detail(str(error), 300)
            continue
        rows = NORMALIZERS[api_name](result.rows, target_period, parse_date)
        fetched[api_name] = {"provider": result.provider.key, "rows": rows}
    if not fetched:
        return {"status": "blocked", "as_of_date": str(as_of_date), "period": str(target_period),
                "reason": "every reporting-calendar API failed", "errors": errors}

    def persist() -> dict[str, int]:
        stored: dict[str, int] = {}
        with db.transaction() as connection:
            for api_name, payload in fetched.items():
                stored[api_name] = PERSISTERS[api_name](
                    connection, payload["rows"], payload["provider"], observed_at,
                )
        return stored

    stored = await run_database_blocking(persist, timeout_seconds=180)
    request_key = hashlib.sha256(
        json.dumps({"capability": "earnings_calendar", "period": stamp}, sort_keys=True).encode(),
    ).hexdigest()
    return {"status": "completed" if not errors else "partial", "as_of_date": str(as_of_date),
            "period": str(target_period), "rows": stored,
            "providers": {name: payload["provider"] for name, payload in fetched.items()},
            "errors": errors or None, "request_key": request_key}


__all__ = [
    "CALENDAR_APIS", "NORMALIZERS", "PERIOD_SETTLE_DAYS", "PERSISTERS",
    "normalize_disclosure_rows", "normalize_express_rows", "normalize_forecast_rows",
    "persist_disclosure_schedule", "persist_earnings_express", "persist_earnings_forecasts",
    "reporting_period", "sync",
]
