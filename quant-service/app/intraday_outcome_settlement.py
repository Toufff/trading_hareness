"""Persisted-evidence settlement for confirmed intraday signal events."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any, Callable
from zoneinfo import ZoneInfo

from psycopg.types.json import Json

from .intraday_clock import continuous_auction_bounds, intraday_outcome_window
from .outcome_recomputation import _archive_before_overwrite, bars_snapshot_hash


INTRADAY_EXIT_QUOTE_TOLERANCE_SECONDS = 90
#: Bumped from ``intraday-outcome-settlement-v1`` (the version the migration
#: backfills onto pre-existing rows): the ``next_close`` reference no longer
#: grabs "whichever bar comes next" for a symbol, which silently stretched
#: past a suspension gap; it now requires the trade calendar's actual next
#: session, and settles a delisted symbol at its last observed close instead
#: of leaving it pending forever.
METHODOLOGY_VERSION = "intraday-outcome-settlement-v2"


def _next_calendar_trading_date(connection: Any, after_date: date, as_of_date: date) -> date | None:
    row = connection.execute(
        """SELECT calendar_date FROM quant.market_trade_calendar
             WHERE exchange='SSE' AND is_open AND calendar_date>%s AND calendar_date<=%s
             ORDER BY calendar_date LIMIT 1""",
        (after_date, as_of_date),
    ).fetchone()
    return row["calendar_date"] if row else None


def settle(
    connection: Any, as_of_date: date | None, *, cutoff: datetime,
    horizons: tuple[tuple[str, int], ...], direction_for: Callable[[str], int | None],
    metrics_for: Callable[[Decimal, int, list[Decimal]], dict[str, Decimal] | None],
    decimal_or_none: Callable[[Any], Decimal | None], barrier_spec_type: Callable[[], Any],
    triple_barrier_label: Callable[..., Any], persist_barrier_outcome: Callable[..., Any],
    return_decomposition: Callable[..., dict[str, Any]], json_safe: Callable[[Any], Any],
) -> dict[str, Any]:
    """Settle only from rows already persisted before ``cutoff``; never fetch."""
    horizon_counts = {key: 0 for key, _ in horizons}
    matured = pending = 0
    signals = connection.execute(
        """SELECT signal_event_id,symbol,signal_type,observed_at,evidence
             FROM quant.intraday_signal_events
            WHERE state IN ('confirmed','alerted') AND signal_type IN ('entry','watch','reduce','exit')
              AND observed_at<=%s ORDER BY observed_at""", (cutoff,),
    ).fetchall()
    for signal in signals:
        direction = direction_for(str(signal["signal_type"]))
        evidence = signal["evidence"] if isinstance(signal["evidence"], dict) else {}
        entry_price = decimal_or_none((evidence.get("tencent") or {}).get("price"))
        entry_observed_at = signal["observed_at"]
        if entry_price is None:
            signal_bounds = continuous_auction_bounds(signal["observed_at"])
            entry_quote = None
            if signal_bounds is not None:
                session_start, _ = signal_bounds
                entry_quote = connection.execute(
                    """SELECT observed_at,price FROM quant.intraday_quote_observations
                         WHERE symbol=%s AND source_name='tencent_free' AND observed_at<=%s AND observed_at>=%s
                           AND price>0 ORDER BY observed_at DESC LIMIT 1""",
                    (signal["symbol"], signal["observed_at"], max(session_start, signal["observed_at"] - timedelta(seconds=90))),
                ).fetchone()
            if entry_quote:
                entry_price, entry_observed_at = Decimal(entry_quote["price"]), entry_quote["observed_at"]
        if entry_price is None or direction is None:
            continue
        barrier_spec = barrier_spec_type()
        barrier_bounds = continuous_auction_bounds(entry_observed_at)
        barrier_deadline = entry_observed_at + timedelta(minutes=barrier_spec.max_horizon_minutes)
        if barrier_bounds is None:
            barrier_result: dict[str, Any] = {
                "status": "unavailable", "label": None, "reason": "entry_outside_continuous_auction",
            }
            barrier_rows: list[Any] = []
        else:
            _, session_end = barrier_bounds
            barrier_end = min(cutoff, session_end, barrier_deadline)
            barrier_rows = connection.execute(
                """SELECT observed_at,price FROM quant.intraday_quote_observations
                     WHERE symbol=%s AND source_name='tencent_free' AND observed_at>%s AND observed_at<=%s AND price>0
                     ORDER BY observed_at""",
                (signal["symbol"], entry_observed_at, barrier_end),
            ).fetchall()
            barrier_result = triple_barrier_label(
                [dict(row) for row in barrier_rows], entry_price=entry_price,
                entry_at=entry_observed_at, spec=barrier_spec,
            )
            # The generic labeler deliberately knows no exchange sessions.  A
            # truncated 60-minute path must not sit pending forever or borrow
            # the afternoon/next-day path on a later settlement run.
            if (barrier_result.get("status") == "pending" and session_end < barrier_deadline
                    and cutoff >= session_end):
                barrier_result = {
                    "status": "unavailable", "label": None,
                    "reason": "barrier_horizon_crosses_continuous_session_boundary",
                    **{key: barrier_result[key] for key in ("last_at", "last_price") if key in barrier_result},
                }
        persist_barrier_outcome(
            connection, signal["signal_event_id"], spec=barrier_spec, entry_at=entry_observed_at,
            entry_price=entry_price, result=barrier_result,
            source_status={
                "path": "local_tencent_free", "cutoff": cutoff.isoformat(),
                "session_bounded": True,
                "row_count": len(barrier_rows),
                "reason": barrier_result.get("reason"),
            },
        )
        for horizon_key, minutes in horizons:
            window = intraday_outcome_window(
                entry_observed_at, horizon_minutes=minutes, cutoff=cutoff,
                tolerance_seconds=INTRADAY_EXIT_QUOTE_TOLERANCE_SECONDS,
            )
            exit_quote = None
            # ``unavailable`` after the quote-delay tolerance has elapsed is
            # not permission to skip the original bounded interval.  The
            # quote may already be in the local ledger when a post-close
            # recompute runs.  Only a session-crossing window has no query
            # bounds and must never borrow lunch/overnight data.
            if (window.get("query_start") is not None and window.get("query_end") is not None
                    and window["query_end"] >= window["query_start"]):
                exit_quote = connection.execute(
                    """SELECT observed_at,price FROM quant.intraday_quote_observations
                         WHERE symbol=%s AND source_name='tencent_free' AND observed_at>=%s AND observed_at<=%s AND price>0
                         ORDER BY observed_at LIMIT 1""",
                    (signal["symbol"], window["query_start"], window["query_end"]),
                ).fetchone()
            status = "matured" if exit_quote else str(window["status"])
            if exit_quote:
                path = connection.execute(
                    """SELECT price FROM quant.intraday_quote_observations
                         WHERE symbol=%s AND source_name='tencent_free' AND observed_at>=%s AND observed_at<=%s AND price>0
                         ORDER BY observed_at""",
                    (signal["symbol"], signal["observed_at"], exit_quote["observed_at"]),
                ).fetchall()
                outcome = metrics_for(entry_price, direction, [Decimal(row["price"]) for row in path])
                matured += 1
                snapshot_hash = bars_snapshot_hash([str(row["price"]) for row in path])
            else:
                outcome = None
                snapshot_hash = None
                if status == "pending":
                    pending += 1
            _archive_before_overwrite(
                connection, "intraday_signal_outcomes", "signal_event_id=%s AND horizon_key=%s",
                (signal["signal_event_id"], horizon_key), ("signal_event_id", "horizon_key"),
            )
            connection.execute(
                """INSERT INTO quant.intraday_signal_outcomes(signal_event_id,horizon_key,direction,entry_observed_at,entry_price,
                     exit_observed_at,exit_price,raw_return,maximum_favorable_excursion,maximum_adverse_excursion,status,tradability,
                     source_status,methodology_version,bars_snapshot_hash)
                   VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'observed_quote_only',%s,%s,%s)
                   ON CONFLICT(signal_event_id,horizon_key) DO UPDATE SET exit_observed_at=EXCLUDED.exit_observed_at,
                     exit_price=EXCLUDED.exit_price,raw_return=EXCLUDED.raw_return,
                     maximum_favorable_excursion=EXCLUDED.maximum_favorable_excursion,
                     maximum_adverse_excursion=EXCLUDED.maximum_adverse_excursion,status=EXCLUDED.status,
                     tradability=EXCLUDED.tradability,source_status=EXCLUDED.source_status,
                     methodology_version=EXCLUDED.methodology_version,bars_snapshot_hash=EXCLUDED.bars_snapshot_hash,calculated_at=now()""",
                (signal["signal_event_id"], horizon_key, direction, entry_observed_at, entry_price,
                 exit_quote["observed_at"] if exit_quote else None, exit_quote["price"] if exit_quote else None,
                 outcome["raw_return"] if outcome else None, outcome["maximum_favorable_excursion"] if outcome else None,
                 outcome["maximum_adverse_excursion"] if outcome else None, status,
                 Json({
                     "entry": "signal_evidence.tencent.price", "exit": "tencent_free",
                     "cutoff": cutoff.isoformat(), "settlement_window": {
                         key: value.isoformat() if isinstance(value, datetime) else value
                         for key, value in window.items()
                     },
                 }), METHODOLOGY_VERSION, snapshot_hash),
            )
            horizon_counts[horizon_key] += 1
        signal_date = signal["observed_at"].astimezone(ZoneInfo("Asia/Shanghai")).date()
        same_day_close: Decimal | None = None
        for horizon_key in ("close", "next_close"):
            delisted_next_close = False
            if horizon_key == "close":
                # Same-session close: no calendar lookup needed, a suspended
                # session simply has no bar and correctly stays pending.
                daily_exit = connection.execute(
                    """SELECT trading_date,available_at,open,close FROM quant.canonical_bars_daily
                         WHERE symbol=%s AND trading_date=%s AND available_at>%s AND available_at<=%s
                         ORDER BY trading_date LIMIT 1""", (signal["symbol"], signal_date, signal["observed_at"], cutoff),
                ).fetchone()
            else:
                # The trade calendar's actual next session, never "whichever
                # bar for this symbol comes next": that silently stretched the
                # reference across a suspension gap.  A delisted symbol
                # settles at its last observed close instead of staying
                # pending forever.
                target_exit_date = _next_calendar_trading_date(connection, signal_date, cutoff.astimezone(ZoneInfo("Asia/Shanghai")).date())
                daily_exit = None
                if target_exit_date is not None:
                    instrument = connection.execute(
                        "SELECT delist_date FROM quant.instruments WHERE symbol=%s", (signal["symbol"],),
                    ).fetchone()
                    delist_date = instrument["delist_date"] if instrument else None
                    if delist_date is not None and delist_date <= target_exit_date:
                        daily_exit = connection.execute(
                            """SELECT trading_date,available_at,open,close FROM quant.canonical_bars_daily
                                 WHERE symbol=%s AND trading_date<=%s AND available_at<=%s
                                 ORDER BY trading_date DESC LIMIT 1""",
                            (signal["symbol"], delist_date, cutoff),
                        ).fetchone()
                        delisted_next_close = daily_exit is not None
                    else:
                        daily_exit = connection.execute(
                            """SELECT trading_date,available_at,open,close FROM quant.canonical_bars_daily
                                 WHERE symbol=%s AND trading_date=%s AND available_at>%s AND available_at<=%s""",
                            (signal["symbol"], target_exit_date, signal["observed_at"], cutoff),
                        ).fetchone()
            status = "matured" if daily_exit else "pending"
            outcome = metrics_for(entry_price, direction, [Decimal(daily_exit["close"])]) if daily_exit else None
            if horizon_key == "close" and daily_exit:
                same_day_close = Decimal(daily_exit["close"])
            decomposition = return_decomposition(
                entry_price, direction, same_day_close,
                Decimal(daily_exit["open"]) if horizon_key == "next_close" and daily_exit and daily_exit["open"] else None,
                Decimal(daily_exit["close"]) if horizon_key == "next_close" and daily_exit else None,
            ) if horizon_key == "next_close" else None
            tradability = "delisted" if delisted_next_close else "daily_close_reference"
            snapshot_hash = bars_snapshot_hash([str(daily_exit["trading_date"]), str(daily_exit["close"])]) if daily_exit else None
            _archive_before_overwrite(
                connection, "intraday_signal_outcomes", "signal_event_id=%s AND horizon_key=%s",
                (signal["signal_event_id"], horizon_key), ("signal_event_id", "horizon_key"),
            )
            connection.execute(
                """INSERT INTO quant.intraday_signal_outcomes(signal_event_id,horizon_key,direction,entry_observed_at,entry_price,
                     exit_observed_at,exit_price,raw_return,maximum_favorable_excursion,maximum_adverse_excursion,status,tradability,
                     source_status,methodology_version,bars_snapshot_hash)
                   VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                   ON CONFLICT(signal_event_id,horizon_key) DO UPDATE SET exit_observed_at=EXCLUDED.exit_observed_at,
                     exit_price=EXCLUDED.exit_price,raw_return=EXCLUDED.raw_return,
                     maximum_favorable_excursion=EXCLUDED.maximum_favorable_excursion,
                     maximum_adverse_excursion=EXCLUDED.maximum_adverse_excursion,status=EXCLUDED.status,
                     tradability=EXCLUDED.tradability,source_status=EXCLUDED.source_status,
                     methodology_version=EXCLUDED.methodology_version,bars_snapshot_hash=EXCLUDED.bars_snapshot_hash,calculated_at=now()""",
                (signal["signal_event_id"], horizon_key, direction, entry_observed_at, entry_price,
                 daily_exit["available_at"] if daily_exit else None, daily_exit["close"] if daily_exit else None,
                 outcome["raw_return"] if outcome else None, outcome["maximum_favorable_excursion"] if outcome else None,
                 outcome["maximum_adverse_excursion"] if outcome else None, status, tradability,
                 Json(json_safe({"entry": "signal_evidence.tencent.price", "exit": "canonical_daily_close", "cutoff": cutoff.isoformat(),
                                 "return_decomposition": decomposition})), METHODOLOGY_VERSION, snapshot_hash),
            )
            if status == "matured":
                matured += 1
            else:
                pending += 1
    return {"as_of_date": str(as_of_date) if as_of_date else None, "signals": len(signals),
            "outcome_rows": sum(horizon_counts.values()) + len(signals) * 2,
            "matured": matured, "pending": pending, "intraday_horizons": horizon_counts}


__all__ = ["settle"]
