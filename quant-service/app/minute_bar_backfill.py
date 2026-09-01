"""Historical minute-bar backfill, scoped to a candidate set.

``quant.market_bars_minute`` was empty and the only intraday history anywhere
in this deployment was ``intraday_minute_sessions`` - 36 watchlist symbols over
11 sessions.  That is why most intraday strategy rules (re-seal confirmation,
drawdown-to-VWAP entries, timed MA5 breaks) could not be replayed at all.

``stk_mins`` on the ProMax gateway serves 1-minute bars at least two years
back.  It answers one ts_code per request - a comma-separated batch returns
nothing - so a full-market backfill is not realistic under the gateway's
per-minute budget.  Scoping to a candidate set is: the limit-up pool runs
roughly 50-100 names a session, which is a few thousand requests for a
multi-month window rather than millions.

Two data-quality behaviours of the upstream are handled here rather than left
for every consumer to rediscover:

- Recent sessions return extra rows outside the continuous auction (09:25
  auction prints, 15:3x after-hours). Older sessions return a clean 241 bars.
  Bars outside the session are dropped so a VWAP or MA computed downstream is
  not contaminated by an auction print.
- Recent sessions also redeliver most minutes twice, once on the exact minute
  and once with a jittered second offset (``09:32:00`` and ``09:32:07``).
  Measured on 000017.SZ for 2026-08-25: 466 raw rows for a 241-minute session,
  221 minutes duplicated with byte-identical values, 4 differing - three only
  in floating-point noise on ``amount``, and one (14:15) where the on-the-minute
  row carried the complete 12718 shares against 4100 on the jittered copy.
  Rows are therefore collapsed to one per minute preferring the exact minute
  stamp.  Without this a VWAP or volume sum would double-count almost every bar.
- ``trade_time`` arrives as an exchange-local wall clock with no offset.
"""

from __future__ import annotations

import hashlib
import uuid
from datetime import date, datetime, time as _time, timezone
from typing import Any, Awaitable, Callable, Sequence
from zoneinfo import ZoneInfo

from psycopg.types.json import Json


CN_TZ = ZoneInfo("Asia/Shanghai")
MINUTE_API = "stk_mins"
SOURCE_NAME = "tushare_stk_mins"
#: Continuous auction only; an opening-auction or after-hours print is not a
#: minute bar and must not enter a VWAP or moving average.
MORNING_OPEN, MORNING_CLOSE = _time(9, 30), _time(11, 30)
AFTERNOON_OPEN, AFTERNOON_CLOSE = _time(13, 0), _time(15, 0)
#: A complete A-share session is 240 continuous-auction minutes; the upstream
#: includes the 09:30 stamp, so 241 rows is a full session.
FULL_SESSION_BARS = 241


def in_session(moment: datetime) -> bool:
    local = moment.astimezone(CN_TZ).time()
    return (MORNING_OPEN <= local <= MORNING_CLOSE) or (AFTERNOON_OPEN <= local <= AFTERNOON_CLOSE)


def _number(value: Any) -> float | None:
    try:
        return float(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def parse_trade_time(value: Any) -> datetime | None:
    """Interpret the upstream's offset-free exchange wall clock."""
    text = str(value or "").strip()
    if not text:
        return None
    for pattern in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return datetime.strptime(text, pattern).replace(tzinfo=CN_TZ)
        except ValueError:
            continue
    return None


def normalize_minute_rows(symbol: str, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Collapse the upstream rows into one in-session bar per minute.

    A minute delivered both on the exact minute and with a jittered second
    offset keeps the on-the-minute row: it is the upstream's canonical stamp,
    and in the only observed case where the two genuinely disagreed it held the
    complete minute's volume while the jittered copy held a fraction of it.
    """
    by_minute: dict[datetime, tuple[int, dict[str, Any]]] = {}
    for row in rows:
        if str(row.get("ts_code") or "").upper() != symbol:
            continue
        stamped = parse_trade_time(row.get("trade_time"))
        if stamped is None or not in_session(stamped):
            continue
        close = _number(row.get("close"))
        if close is None or close <= 0:
            continue
        minute = stamped.replace(second=0, microsecond=0)
        # Rank 0 wins: an exact minute stamp beats a jittered redelivery.
        rank = 0 if stamped.second == 0 and stamped.microsecond == 0 else 1
        existing = by_minute.get(minute)
        # Rank decides only between an exact stamp and a jittered one.  Two
        # rows with the same rank are indistinguishable deliveries of the same
        # minute, so the later one wins, as it did before this collapsing.
        if existing is not None and existing[0] < rank:
            continue
        by_minute[minute] = (rank, {
            "symbol": symbol, "bar_time": minute,
            "open": _number(row.get("open")), "high": _number(row.get("high")),
            "low": _number(row.get("low")), "close": close,
            "volume": _number(row.get("vol")), "amount": _number(row.get("amount")),
            "raw": dict(row),
        })
    return [by_minute[key][1] for key in sorted(by_minute)]


def ensure_import_record(connection: Any, symbol: str, trading_date: date, row_count: int,
                         started_at: datetime) -> uuid.UUID:
    """Create the provenance row ``market_bars_minute.import_id`` requires.

    Minute bars previously arrived only through offline file imports, so the
    table makes that provenance mandatory.  A provider-sourced backfill is
    still an import; it is recorded as one, keyed deterministically on
    (symbol, session, source) so a re-run updates rather than accumulating a
    fresh record for every retry.
    """
    file_name = f"{SOURCE_NAME}/{symbol}/{trading_date.isoformat()}"
    digest = hashlib.sha256(file_name.encode()).hexdigest()
    import_id = uuid.uuid5(uuid.NAMESPACE_URL, file_name)
    connection.execute(
        """INSERT INTO quant.offline_imports(
                import_id,source_name,file_name,file_sha256,dataset_kind,status,
                row_count,rejected_rows,started_at,finished_at)
           VALUES(%s,%s,%s,%s,'minute_bar','completed',%s,0,%s,%s)
           ON CONFLICT(import_id) DO UPDATE SET row_count=EXCLUDED.row_count,
             status=EXCLUDED.status,finished_at=EXCLUDED.finished_at""",
        (import_id, SOURCE_NAME, file_name, digest, row_count, started_at, started_at),
    )
    return import_id


def persist_minute_rows(connection: Any, rows: list[dict[str, Any]], available_at: datetime,
                        import_id: uuid.UUID | None = None) -> int:
    for row in rows:
        connection.execute(
            """INSERT INTO quant.market_bars_minute(
                    symbol,bar_time,open,high,low,close,volume,amount,source_name,
                    import_id,available_at,raw)
               SELECT %s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s
                WHERE EXISTS(SELECT 1 FROM quant.instruments WHERE symbol=%s)
               ON CONFLICT(symbol,bar_time,source_name) DO UPDATE SET
                 open=EXCLUDED.open,high=EXCLUDED.high,low=EXCLUDED.low,close=EXCLUDED.close,
                 volume=EXCLUDED.volume,amount=EXCLUDED.amount,
                 available_at=EXCLUDED.available_at,raw=EXCLUDED.raw""",
            (row["symbol"], row["bar_time"], row["open"], row["high"], row["low"], row["close"],
             row["volume"], row["amount"], SOURCE_NAME, import_id, available_at,
             Json(row["raw"]), row["symbol"]),
        )
    return len(rows)


def reconcile_against_daily(connection: Any, symbol: str, trading_date: date) -> dict[str, Any]:
    """Compare the stored minute volume against the canonical daily bar.

    This is the only independent check available on backfilled intraday data,
    and it is decisive: a complete session reconciles exactly (000017.SZ on
    2026-08-25 summed to 57.856M shares against a 57.856M daily bar, 0.000%),
    while a session the upstream truncated shows the shortfall (603118.SH,
    226 of 241 bars, -10.18%).  ``canonical_bars_daily.volume`` is in 手.
    """
    row = connection.execute(
        """SELECT sum(m.volume) minute_volume, max(d.volume)*100 daily_volume, count(*) bars
             FROM quant.market_bars_minute m
             JOIN quant.canonical_bars_daily d
               ON d.symbol=m.symbol AND d.trading_date=%s
            WHERE m.symbol=%s AND m.source_name=%s
              AND (m.bar_time AT TIME ZONE 'Asia/Shanghai')::date=%s""",
        (trading_date, symbol, SOURCE_NAME, trading_date),
    ).fetchone()
    if row is None or not row["minute_volume"] or not row["daily_volume"]:
        return {"status": "unverifiable", "reason": "no daily bar or no stored minute volume"}
    minute_volume, daily_volume = float(row["minute_volume"]), float(row["daily_volume"])
    difference = minute_volume / daily_volume * 100 - 100
    return {
        "status": "reconciled" if abs(difference) <= 0.5 else "shortfall",
        "bars": int(row["bars"]), "minute_volume": minute_volume,
        "daily_volume": daily_volume, "difference_pct": round(difference, 4),
    }


def limit_up_symbols(connection: Any, start_date: date, end_date: date) -> dict[date, list[str]]:
    """Sessions mapped to the names that closed locked at the limit.

    Derived from the canonical daily bars rather than a vendor pool so the
    candidate set is reproducible from data already under quality control.
    """
    rows = connection.execute(
        """SELECT trading_date, symbol FROM quant.canonical_bars_daily
            WHERE trading_date BETWEEN %s AND %s
              AND limit_up IS NOT NULL AND volume > 0 AND NOT coalesce(is_suspended, false)
              AND close >= limit_up - 0.005
            ORDER BY trading_date, symbol""",
        (start_date, end_date),
    ).fetchall()
    sessions: dict[date, list[str]] = {}
    for row in rows:
        sessions.setdefault(row["trading_date"], []).append(str(row["symbol"]))
    return sessions


async def backfill_symbol_session(
    symbol: str, trading_date: date, *,
    selection_roles: Sequence[str] = (),
    call_tushare_api: Callable[..., Awaitable[Any]],
    run_database_blocking: Callable[..., Awaitable[Any]],
    db: Any,
) -> dict[str, Any]:
    """Fetch and store one symbol's minute bars for one session."""
    stamp = trading_date.strftime("%Y-%m-%d")
    call = await call_tushare_api(
        MINUTE_API,
        {"ts_code": symbol, "freq": "1min",
         "start_date": f"{stamp} 09:30:00", "end_date": f"{stamp} 15:00:00"},
        # Automatic routing keeps the audited ProMax GET as first candidate but
        # can fall through to the SDK when the GET gateway returns an empty
        # session (the upstream has done this intermittently).
        None, "auto",
    )
    rows = normalize_minute_rows(symbol, call.rows)
    if not rows:
        return {"symbol": symbol, "trading_date": str(trading_date), "status": "empty", "bars": 0}
    roles = sorted({str(role) for role in selection_roles if str(role)})
    if roles:
        rows = [{**row, "raw": {**dict(row["raw"]), "selection_roles": roles}} for row in rows]
    observed_at = datetime.now(timezone.utc)

    def persist() -> int:
        with db.transaction() as connection:
            import_id = ensure_import_record(connection, symbol, trading_date, len(rows), observed_at)
            return persist_minute_rows(connection, rows, observed_at, import_id)

    stored = await run_database_blocking(persist, timeout_seconds=120)

    def verify() -> dict[str, Any]:
        with db.transaction() as connection:
            return reconcile_against_daily(connection, symbol, trading_date)

    reconciliation = await run_database_blocking(verify, timeout_seconds=60)
    return {"symbol": symbol, "trading_date": str(trading_date),
            "status": "completed" if stored >= FULL_SESSION_BARS - 1 else "partial",
            "bars": stored, "expected": FULL_SESSION_BARS,
            "selection_roles": roles, "reconciliation": reconciliation}


__all__ = [
    "CN_TZ", "FULL_SESSION_BARS", "MINUTE_API", "SOURCE_NAME",
    "backfill_symbol_session", "ensure_import_record", "in_session", "limit_up_symbols",
    "normalize_minute_rows", "parse_trade_time", "persist_minute_rows",
    "reconcile_against_daily",
]
