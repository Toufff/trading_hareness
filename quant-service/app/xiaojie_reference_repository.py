"""Session-scoped reference data for xiaojie leader-flow indicators.

Everything here is end-of-previous-session material - limit prices, the prior
bar, a 20-day high, MA5, sector membership - so it is read once per trading
date and reused by every 30-second scan rather than re-queried each time.

Point-in-time discipline: every read is bounded to sessions strictly before
the scan's own trading date.  Today's bars land in the same tables after the
close, and an intraday indicator that silently started using them would be
reading its own outcome.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any, Awaitable, Callable

from psycopg.types.json import Json

#: Sessions used for the breakout high and the recent-behaviour counters.
LOOKBACK_SESSIONS = 20
MA_SESSIONS = 5


def trade_limits(connection: Any, trading_date: date) -> dict[str, float]:
    """Upper limit price per symbol for the session being scanned.

    Limits are published for the session itself and are known before the open,
    so this is the one reference legitimately read for ``trading_date``.
    """
    rows = connection.execute(
        """SELECT DISTINCT ON (symbol) symbol, limit_up FROM quant.daily_trade_limits
            WHERE trading_date=%s AND limit_up IS NOT NULL
            ORDER BY symbol, provider""",
        (trading_date,),
    ).fetchall()
    return {str(row["symbol"]): float(row["limit_up"]) for row in rows}


async def ensure_session_trade_limits(
    trading_date: date, *,
    read_limits: Callable[[date], Awaitable[dict[str, float]]],
    call_tushare_api: Callable[..., Awaitable[Any]],
    persist_limits: Callable[[date, list[dict[str, Any]]], Awaitable[int]],
) -> dict[str, Any]:
    """Guarantee the session's limit prices exist before the first scan needs them.

    Limit prices are published for the session and are known before the open,
    but ``daily_trade_limits`` is only written by the post-close control sync -
    so intraday the table holds every session except the one being traded.
    Any board-state indicator would silently see no limits at all.  When the
    row set for the date is missing, it is fetched once and persisted, after
    which every later scan reads it from the database.
    """
    existing = await read_limits(trading_date)
    if existing:
        return {"status": "already_present", "symbols": len(existing), "limits": existing}
    call = await call_tushare_api("stk_limit", {"trade_date": trading_date.strftime("%Y%m%d")}, None, "auto")
    rows = [row for row in call.rows if str(row.get("ts_code") or "").strip()]
    if not rows:
        return {"status": "unavailable", "symbols": 0, "limits": {}}
    stored = await persist_limits(trading_date, rows)
    limits = await read_limits(trading_date)
    return {"status": "fetched", "symbols": len(limits), "stored": stored,
            "provider": call.provider.key, "limits": limits}


def persist_trade_limit_rows(connection: Any, trading_date: date, rows: list[dict[str, Any]],
                             provider: str, available_at: datetime) -> int:
    """Upsert one session's published limit prices."""
    stored = 0
    for row in rows:
        symbol = str(row.get("ts_code") or "").upper()
        try:
            up_limit = float(row["up_limit"]) if row.get("up_limit") not in (None, "") else None
            down_limit = float(row["down_limit"]) if row.get("down_limit") not in (None, "") else None
        except (TypeError, ValueError):
            continue
        if not symbol or up_limit is None:
            continue
        connection.execute(
            """INSERT INTO quant.daily_trade_limits(
                    symbol,trading_date,limit_up,limit_down,provider,available_at,raw)
               SELECT %s,%s,%s,%s,%s,%s,%s
                WHERE EXISTS(SELECT 1 FROM quant.instruments WHERE symbol=%s)
               ON CONFLICT(symbol,trading_date,provider) DO UPDATE SET
                 limit_up=EXCLUDED.limit_up,limit_down=EXCLUDED.limit_down,
                 available_at=EXCLUDED.available_at,raw=EXCLUDED.raw""",
            (symbol, trading_date, up_limit, down_limit, provider, available_at,
             Json(dict(row)), symbol),
        )
        stored += 1
    return stored


def sector_membership(connection: Any, trading_date: date,
                      taxonomy_key: str = "ths_concept_flow") -> dict[str, set[str]]:
    rows = connection.execute(
        """SELECT symbol, sector_key FROM quant.sector_membership_history
            WHERE taxonomy_key=%s AND effective_from<=%s
              AND (effective_to IS NULL OR effective_to>=%s)""",
        (taxonomy_key, trading_date, trading_date),
    ).fetchall()
    membership: dict[str, set[str]] = {}
    for row in rows:
        membership.setdefault(str(row["symbol"]), set()).add(str(row["sector_key"]))
    return membership


def candidate_references(connection: Any, trading_date: date) -> dict[str, dict[str, Any]]:
    """Prior-session bar, 20-day high, MA5 and stagnation counters per symbol.

    ``days_without_new_high`` and ``days_without_rise`` are counted over the
    completed sessions before ``trading_date`` so the exit rules that read them
    are answerable from the first scan of the day rather than only at the close.
    """
    rows = connection.execute(
        """WITH recent AS (
              SELECT symbol, trading_date, open, high, low, close, pre_close, limit_up, volume,
                     row_number() OVER (PARTITION BY symbol ORDER BY trading_date DESC) AS rn
                FROM quant.canonical_bars_daily
               WHERE trading_date < %s AND trading_date >= %s - INTERVAL '90 days'
                 AND volume > 0 AND NOT coalesce(is_suspended, false)
           ), prior AS (
              SELECT symbol, open, high, low, close, pre_close, limit_up
                FROM recent WHERE rn = 1
           ), window_stats AS (
              SELECT symbol, max(high) AS high_20d, min(low) AS low_20d,
                     avg(close) FILTER (WHERE rn <= %s) AS ma5,
                     avg(close) AS ma20,
                     avg(volume) FILTER (WHERE rn <= %s) * 100 AS mean_volume_5d,
                     max(close) FILTER (WHERE rn = 10) AS close_10_sessions_ago
                FROM recent WHERE rn <= %s GROUP BY symbol
           ), no_new_high AS (
              SELECT symbol, count(*) AS days FROM recent r
               WHERE rn <= %s
                 AND NOT EXISTS (
                   SELECT 1 FROM recent p
                    WHERE p.symbol = r.symbol AND p.rn < r.rn AND p.high > r.high)
                 AND rn > 1
               GROUP BY symbol
           ), no_rise AS (
              SELECT symbol, count(*) AS days FROM recent
               WHERE rn <= %s AND pre_close IS NOT NULL AND close <= pre_close
               GROUP BY symbol
           )
           SELECT prior.symbol, prior.open, prior.high, prior.low, prior.close,
                  prior.pre_close, prior.limit_up,
                  window_stats.high_20d, window_stats.low_20d, window_stats.ma5,
                  window_stats.ma20, window_stats.mean_volume_5d,
                  window_stats.close_10_sessions_ago,
                  coalesce(no_new_high.days, 0) AS days_without_new_high,
                  coalesce(no_rise.days, 0) AS days_without_rise
             FROM prior
             LEFT JOIN window_stats USING (symbol)
             LEFT JOIN no_new_high USING (symbol)
             LEFT JOIN no_rise USING (symbol)""",
        (trading_date, trading_date, MA_SESSIONS, MA_SESSIONS, LOOKBACK_SESSIONS,
         LOOKBACK_SESSIONS, MA_SESSIONS),
    ).fetchall()
    references: dict[str, dict[str, Any]] = {}
    for row in rows:
        references[str(row["symbol"])] = {
            "prior_bar": {
                "open": float(row["open"]) if row["open"] is not None else None,
                "high": float(row["high"]) if row["high"] is not None else None,
                "low": float(row["low"]) if row["low"] is not None else None,
                "close": float(row["close"]) if row["close"] is not None else None,
                "pre_close": float(row["pre_close"]) if row["pre_close"] is not None else None,
                "limit_up": float(row["limit_up"]) if row["limit_up"] is not None else None,
            },
            "high_20d": float(row["high_20d"]) if row["high_20d"] is not None else None,
            "low_20d": float(row["low_20d"]) if row["low_20d"] is not None else None,
            "ma5": float(row["ma5"]) if row["ma5"] is not None else None,
            "ma20": float(row["ma20"]) if row["ma20"] is not None else None,
            "mean_volume_5d": float(row["mean_volume_5d"]) if row["mean_volume_5d"] is not None else None,
            "close_10_sessions_ago": (float(row["close_10_sessions_ago"])
                                      if row["close_10_sessions_ago"] is not None else None),
            "days_without_new_high": int(row["days_without_new_high"] or 0),
            "days_without_rise": int(row["days_without_rise"] or 0),
        }
    return references


def market_volume_baseline(connection: Any, trading_date: date,
                           sessions: int = MA_SESSIONS) -> float | None:
    """Mean total market volume, in shares, over the recent completed sessions.

    ``canonical_bars_daily.volume`` is in 手, converted here so the baseline is
    directly comparable with the all-A snapshot's share counts.
    """
    # Index series live in the same table and carry aggregate volumes: on
    # 2026-08-25 000001.SH, 399001.SZ, 000300.SH and 399006.SZ alone summed to
    # 84.45 of the table's 189.37 billion shares, against 104.83 billion for
    # the listed names.  Including them nearly doubled the baseline and made a
    # normal session read as half its usual volume.  They carry an instruments
    # row but no listing date, which is the discriminator used here - a listed
    # security has one, an index does not.
    row = connection.execute(
        """SELECT avg(total) * 100 AS baseline FROM (
             SELECT b.trading_date, sum(b.volume) AS total
               FROM quant.canonical_bars_daily b
               JOIN quant.instruments i ON i.symbol = b.symbol
              WHERE i.list_date IS NOT NULL
                AND b.trading_date < %s AND b.volume > 0
                AND NOT coalesce(b.is_suspended, false)
              GROUP BY b.trading_date ORDER BY b.trading_date DESC LIMIT %s) recent""",
        (trading_date, sessions),
    ).fetchone()
    return float(row["baseline"]) if row and row["baseline"] is not None else None


def load_session_reference(connection: Any, trading_date: date) -> dict[str, Any]:
    """One call for everything a session's indicator construction needs."""
    return {
        "trading_date": trading_date,
        "limits": trade_limits(connection, trading_date),
        "membership": sector_membership(connection, trading_date),
        "references": candidate_references(connection, trading_date),
        "market_volume_baseline": market_volume_baseline(connection, trading_date),
    }


__all__ = [
    "LOOKBACK_SESSIONS", "MA_SESSIONS", "candidate_references", "ensure_session_trade_limits",
    "load_session_reference", "persist_trade_limit_rows",
    "market_volume_baseline", "sector_membership", "trade_limits",
]
