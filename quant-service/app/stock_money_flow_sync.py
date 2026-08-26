"""End-of-day per-stock capital flow ingestion.

Sector-level flow was already ingested (``ths_sector_flows``), but nothing in
this codebase ever stored *per-stock* capital flow.  The only per-stock flow
number available anywhere was ``main_net_inflow`` scraped live from a public
Eastmoney endpoint during the intraday scan - a research-only value that is
discarded once the scan ends, so no post-close study could ask whether main
flow preceded anything.

Three independent end-of-day cross-sections are stored, deliberately kept
apart rather than merged, because each vendor defines "main"/"large" order
buckets differently and averaging them would invent a number none of them
published:

``moneyflow``      exchange-derived buy/sell volume and amount by order size
``moneyflow_dc``   Eastmoney's net amount plus super-large/large/medium/small
                   buckets and their percentage rates
``moneyflow_ths``  THS's equivalent decomposition

Boundary, stated plainly: **every one of these is end-of-day only**.  Probed
against the ProMax gateway during the 2026-08-26 session, all three returned
zero rows for that same day while returning full cross-sections (5546 / 6000 /
5210 rows) for the prior session.  So this closes the *research* gap - main
flow is now queryable for post-close and backtest work - and does not close
the intraday one.  ``signal_rules``'s live ``main_net_inflow`` still has no
licensed intraday source, and nothing here should be read as providing one.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any, Awaitable, Callable

from psycopg.types.json import Json


FLOW_APIS = ("moneyflow", "moneyflow_dc", "moneyflow_ths")
#: Below this the provider clearly returned a partial cross-section; a partial
#: flow snapshot is worse than none because a missing symbol is silently read
#: as "no flow" by any downstream aggregate.
MINIMUM_COVERAGE_RATIO = 0.80


def _number(value: Any) -> float | None:
    try:
        return float(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def normalize_flow_rows(api_name: str, rows: list[dict[str, Any]], trade_date: date,
                        parse_date: Callable[[Any], date | None]) -> list[dict[str, Any]]:
    """Keep one row per symbol for the requested date, preserving vendor units.

    The raw payload is retained verbatim.  Only the fields every vendor agrees
    on are promoted to columns; the vendor-specific bucket decomposition stays
    in ``raw`` so a later study can use it without this module having to claim
    the three schemas are equivalent.
    """
    by_symbol: dict[str, dict[str, Any]] = {}
    for row in rows:
        symbol = str(row.get("ts_code") or "").upper()
        if not symbol or parse_date(row.get("trade_date")) != trade_date:
            continue
        # moneyflow reports buy/sell legs; the DC/THS feeds report a net amount
        # directly.  Derive the net only when the vendor did not supply one.
        net = _number(row.get("net_amount"))
        if net is None:
            buy = _number(row.get("buy_elg_amount")) or 0.0
            buy += _number(row.get("buy_lg_amount")) or 0.0
            sell = _number(row.get("sell_elg_amount")) or 0.0
            sell += _number(row.get("sell_lg_amount")) or 0.0
            net = buy - sell if (buy or sell) else None
        by_symbol[symbol] = {
            "symbol": symbol, "trading_date": trade_date, "source": api_name,
            "net_amount": net,
            "net_amount_rate": _number(row.get("net_amount_rate")),
            "buy_elg_amount": _number(row.get("buy_elg_amount")),
            "buy_lg_amount": _number(row.get("buy_lg_amount")),
            "buy_md_amount": _number(row.get("buy_md_amount")),
            "buy_sm_amount": _number(row.get("buy_sm_amount")),
            "raw": dict(row),
        }
    return list(by_symbol.values())


def persist_flow_rows(connection: Any, rows: list[dict[str, Any]], provider: str,
                      available_at: datetime) -> int:
    for row in rows:
        connection.execute(
            """INSERT INTO quant.stock_money_flow_daily(
                    symbol,trading_date,source,provider,net_amount,net_amount_rate,
                    buy_elg_amount,buy_lg_amount,buy_md_amount,buy_sm_amount,available_at,raw)
               SELECT %s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s
                WHERE EXISTS(SELECT 1 FROM quant.instruments WHERE symbol=%s)
               ON CONFLICT(symbol,trading_date,source) DO UPDATE SET
                 provider=EXCLUDED.provider,net_amount=EXCLUDED.net_amount,
                 net_amount_rate=EXCLUDED.net_amount_rate,buy_elg_amount=EXCLUDED.buy_elg_amount,
                 buy_lg_amount=EXCLUDED.buy_lg_amount,buy_md_amount=EXCLUDED.buy_md_amount,
                 buy_sm_amount=EXCLUDED.buy_sm_amount,available_at=EXCLUDED.available_at,
                 raw=EXCLUDED.raw""",
            (row["symbol"], row["trading_date"], row["source"], provider, row["net_amount"],
             row["net_amount_rate"], row["buy_elg_amount"], row["buy_lg_amount"],
             row["buy_md_amount"], row["buy_sm_amount"], available_at, Json(row["raw"]),
             row["symbol"]),
        )
    return len(rows)


async def sync(
    trade_date: date,
    *,
    call_tushare_api: Callable[..., Awaitable[Any]],
    parse_date: Callable[[Any], date | None],
    expected_symbols: Callable[[date], int],
    run_database_blocking: Callable[..., Awaitable[Any]],
    db: Any,
    safe_error_detail: Callable[[str, int], str],
) -> dict[str, Any]:
    """Fetch and store one completed session's per-stock flow cross-sections.

    Each source is independent: one vendor's outage never blocks the others,
    and a cross-section that covers less than ``MINIMUM_COVERAGE_RATIO`` of the
    session's known universe is rejected rather than stored, so a truncated
    response cannot masquerade as "these symbols had no flow".
    """
    expected = await run_database_blocking(expected_symbols, trade_date)
    if expected <= 0:
        return {"status": "blocked", "trade_date": str(trade_date),
                "reason": "no daily bars for this date; flow would have no universe to check against"}
    observed_at = datetime.now(timezone.utc)
    fetched: dict[str, Any] = {}
    errors: dict[str, str] = {}
    for api_name in FLOW_APIS:
        try:
            result = await call_tushare_api(api_name, {"trade_date": trade_date.strftime("%Y%m%d")}, None, "auto")
        except Exception as error:
            errors[api_name] = safe_error_detail(str(error), 300)
            continue
        rows = normalize_flow_rows(api_name, result.rows, trade_date, parse_date)
        if len(rows) < int(expected * MINIMUM_COVERAGE_RATIO):
            errors[api_name] = (f"returned {len(rows)} rows for a {expected}-symbol session; "
                                f"below the {MINIMUM_COVERAGE_RATIO:.0%} coverage floor")
            continue
        fetched[api_name] = {"provider": result.provider.key, "rows": rows}
    if not fetched:
        return {"status": "blocked", "trade_date": str(trade_date), "expected_symbols": expected,
                "reason": "no per-stock flow cross-section passed its coverage gate", "errors": errors}

    def persist() -> dict[str, int]:
        stored: dict[str, int] = {}
        with db.transaction() as connection:
            for api_name, payload in fetched.items():
                stored[api_name] = persist_flow_rows(
                    connection, payload["rows"], payload["provider"], observed_at,
                )
        return stored

    stored = await run_database_blocking(persist, timeout_seconds=180)
    return {"status": "completed" if not errors else "partial", "trade_date": str(trade_date),
            "expected_symbols": expected, "rows": stored,
            "providers": {name: payload["provider"] for name, payload in fetched.items()},
            "errors": errors or None,
            "boundary": "end_of_day_only; no licensed intraday per-stock flow exists"}


__all__ = [
    "FLOW_APIS", "MINIMUM_COVERAGE_RATIO", "normalize_flow_rows", "persist_flow_rows", "sync",
]
