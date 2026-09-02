"""Persistence for derived multiscale market-flow research evidence."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from psycopg.types.json import Json

from .market_flow_features import board_flow_breadth, intraday_flow_state, volume_flow_regime
from .sector_flow_repository import rebuild_sector_flow_daily_features


CHINA = ZoneInfo("Asia/Shanghai")


def _items(row: Any) -> list[dict[str, Any]]:
    return list((dict(row.get("payload") or {}) if row else {}).get("items") or [])


def _feature_status(features: dict[str, Any]) -> str:
    if not features.get("board_count"):
        return "insufficient"
    return "partial" if features.get("quality_flags") else "ready"


def _insert_feature(
    connection: Any,
    *,
    feature_key: str,
    exchange_date: Any,
    cadence: str,
    observed_at: datetime,
    source_snapshot_minute: datetime | None,
    status: str,
    market_state: str,
    features: dict[str, Any],
) -> None:
    connection.execute(
        """INSERT INTO quant.market_flow_feature_snapshots(
               feature_key,exchange_date,cadence,observed_at,source_snapshot_minute,status,market_state,
               concept_count,concept_positive_ratio,concept_median_flow,concept_mean_change_pct,
               five_minute_positive_ratio_delta,session_positive_ratio_delta,afternoon_repair_strength,
               market_amount,market_volume,amount_change_pct,volume_change_pct,advancer_ratio,features,quality_flags)
           VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
           ON CONFLICT(feature_key) DO UPDATE SET
             observed_at=EXCLUDED.observed_at,source_snapshot_minute=EXCLUDED.source_snapshot_minute,
             status=EXCLUDED.status,market_state=EXCLUDED.market_state,concept_count=EXCLUDED.concept_count,
             concept_positive_ratio=EXCLUDED.concept_positive_ratio,concept_median_flow=EXCLUDED.concept_median_flow,
             concept_mean_change_pct=EXCLUDED.concept_mean_change_pct,
             five_minute_positive_ratio_delta=EXCLUDED.five_minute_positive_ratio_delta,
             session_positive_ratio_delta=EXCLUDED.session_positive_ratio_delta,
             afternoon_repair_strength=EXCLUDED.afternoon_repair_strength,
             market_amount=EXCLUDED.market_amount,market_volume=EXCLUDED.market_volume,
             amount_change_pct=EXCLUDED.amount_change_pct,volume_change_pct=EXCLUDED.volume_change_pct,
             advancer_ratio=EXCLUDED.advancer_ratio,features=EXCLUDED.features,
             quality_flags=EXCLUDED.quality_flags,updated_at=now()""",
        (
            feature_key, exchange_date, cadence, observed_at, source_snapshot_minute, status, market_state,
            int(features.get("board_count") or features.get("concept_board_count") or 0),
            features.get("positive_ratio", features.get("concept_positive_ratio")),
            features.get("median_flow"), features.get("mean_change_pct"),
            features.get("five_minute_positive_ratio_delta"), features.get("session_positive_ratio_delta"),
            features.get("afternoon_repair_strength"), features.get("market_amount"), features.get("market_volume"),
            features.get("amount_change_pct"), features.get("volume_change_pct"), features.get("advancer_ratio"),
            Json(features), Json(sorted(set(features.get("quality_flags") or []))),
        ),
    )


def persist_intraday_market_flow_feature(
    database: Any,
    snapshot_minute: datetime,
    observed_at: datetime,
) -> dict[str, Any]:
    """Derive one coverage-gated minute state from already stored raw snapshots."""
    with database.transaction() as connection:
        return _persist_intraday_market_flow_feature_on(connection, snapshot_minute, observed_at)


def _persist_intraday_market_flow_feature_on(
    connection: Any,
    snapshot_minute: datetime,
    observed_at: datetime,
) -> dict[str, Any]:
    """Same derivation as :func:`persist_intraday_market_flow_feature`, on a caller-owned connection.

    Used by :func:`rebuild_stored_market_flow_features` so a multi-day rebuild
    processes every minute row inside one transaction instead of opening one
    transaction per minute.
    """
    local = snapshot_minute.astimezone(CHINA)
    exchange_date = local.date()
    session_start_time = time(9, 30) if local.time() >= time(9, 30) else time(9, 20)
    session_start = datetime.combine(exchange_date, session_start_time, tzinfo=CHINA).astimezone(timezone.utc)
    afternoon_start = datetime.combine(exchange_date, time(13, 0), tzinfo=CHINA).astimezone(timezone.utc)
    current = connection.execute(
        """SELECT payload FROM quant.intraday_board_flow_snapshots
             WHERE snapshot_minute=%s AND status IN ('completed','partial')""",
        (snapshot_minute,),
    ).fetchone()
    if current is None:
        return {"status": "insufficient", "state": "insufficient", "quality_flags": ["source_snapshot_missing"]}
    five_minute = connection.execute(
        """SELECT payload FROM quant.intraday_board_flow_snapshots
             WHERE snapshot_minute<=%s AND snapshot_minute>=%s
               AND status IN ('completed','partial')
             ORDER BY snapshot_minute DESC LIMIT 1""",
        (snapshot_minute - timedelta(minutes=5), session_start),
    ).fetchone()
    session_reference = connection.execute(
        """SELECT payload FROM quant.intraday_board_flow_snapshots
             WHERE snapshot_minute>=%s AND snapshot_minute<=%s
               AND status IN ('completed','partial')
             ORDER BY snapshot_minute LIMIT 1""",
        (session_start, snapshot_minute),
    ).fetchone()
    afternoon_min = None
    if local.time() >= time(13, 0):
        row = connection.execute(
            """SELECT min(concept_positive_ratio) AS minimum
                 FROM quant.market_flow_feature_snapshots
                WHERE exchange_date=%s AND cadence='minute'
                  AND observed_at>=%s AND observed_at<%s
                  AND concept_positive_ratio IS NOT NULL""",
            (exchange_date, afternoon_start, observed_at),
        ).fetchone()
        afternoon_min = float(row["minimum"]) if row and row["minimum"] is not None else None

    current_breadth = board_flow_breadth(_items(current))
    features = intraday_flow_state(
        current_breadth,
        five_minute_reference=board_flow_breadth(_items(five_minute)) if five_minute else None,
        session_reference=board_flow_breadth(_items(session_reference)) if session_reference else None,
        afternoon_min_positive_ratio=afternoon_min,
    )
    status = _feature_status(features)
    _insert_feature(
        connection,
        feature_key=f"minute:{snapshot_minute.isoformat()}", exchange_date=exchange_date,
        cadence="minute", observed_at=observed_at, source_snapshot_minute=snapshot_minute,
        status=status, market_state=str(features["state"]), features=features,
    )
    return {"status": status, **features}


def persist_market_snapshot_flow_feature(
    connection: Any,
    *,
    session: str,
    exchange_date: Any,
    observed_at: datetime,
    summary: dict[str, Any],
) -> dict[str, Any]:
    """Attach prior-close volume and latest intraday breadth to a market snapshot."""
    previous = connection.execute(
        """SELECT summary FROM quant.market_snapshot_runs
             WHERE session='close' AND exchange_date<%s
             ORDER BY exchange_date DESC,observed_at DESC LIMIT 1""",
        (exchange_date,),
    ).fetchone()
    latest_flow = connection.execute(
        """SELECT observed_at,concept_count,concept_positive_ratio,concept_median_flow,
                  concept_mean_change_pct,market_state,features,quality_flags
             FROM quant.market_flow_feature_snapshots
            WHERE exchange_date=%s AND cadence='minute' AND observed_at<=%s
            ORDER BY observed_at DESC LIMIT 1""",
        (exchange_date, observed_at),
    ).fetchone()
    concept_flow = {
        "board_count": int(latest_flow["concept_count"]),
        "positive_ratio": float(latest_flow["concept_positive_ratio"]),
        "median_flow": float(latest_flow["concept_median_flow"]) if latest_flow["concept_median_flow"] is not None else None,
        "mean_change_pct": float(latest_flow["concept_mean_change_pct"]) if latest_flow["concept_mean_change_pct"] is not None else None,
    } if latest_flow and latest_flow["concept_positive_ratio"] is not None else None
    features = volume_flow_regime(
        summary,
        previous_close_summary=dict(previous["summary"] or {}) if previous else None,
        concept_flow=concept_flow,
    )
    features["source_minute_market_state"] = latest_flow["market_state"] if latest_flow else None
    status = "partial" if features["quality_flags"] else "ready"
    if features["market_amount"] is None and features["market_volume"] is None:
        status = "insufficient"
    _insert_feature(
        connection,
        feature_key=f"snapshot:{exchange_date}:{session}", exchange_date=exchange_date,
        cadence=session, observed_at=observed_at,
        source_snapshot_minute=latest_flow["observed_at"] if latest_flow else None,
        status=status, market_state=str(features["state"]), features=features,
    )
    return {"status": status, **features}


def rebuild_stored_market_flow_features(
    database: Any,
    start_date: date,
    end_date: date,
) -> dict[str, Any]:
    """Rebuild derived features from local evidence without provider calls.

    Minute rows are processed chronologically so afternoon-repair features can
    only see earlier same-day observations.  Market snapshots are rebuilt
    afterwards, allowing their close/midday rows to reference the derived
    minute state from the same exchange date.
    """
    if end_date < start_date:
        raise ValueError("end_date must not be before start_date")
    if (end_date - start_date).days > 45:
        raise ValueError("stored feature rebuild is capped at 45 calendar days")
    start_utc = datetime.combine(start_date, time.min, tzinfo=CHINA).astimezone(timezone.utc)
    end_utc = datetime.combine(end_date + timedelta(days=1), time.min, tzinfo=CHINA).astimezone(timezone.utc)
    # Previously one transaction per read plus one further transaction per
    # minute row and per snapshot row (potentially hundreds across a 45-day
    # window).  Everything below now shares the single transaction opened
    # here, matching how ``materialize_sector_flow_daily_outcomes`` already
    # reads the minute-derived rows it depends on inside the same commit.
    minute_counts = {"ready": 0, "partial": 0, "insufficient": 0}
    snapshot_counts = {"ready": 0, "partial": 0, "insufficient": 0}
    with database.transaction() as connection:
        minute_rows = connection.execute(
            """SELECT snapshot_minute,observed_at
                 FROM quant.intraday_board_flow_snapshots
                WHERE snapshot_minute>=%s AND snapshot_minute<%s
                  AND status IN ('completed','partial')
                ORDER BY snapshot_minute""",
            (start_utc, end_utc),
        ).fetchall()
        snapshot_rows = connection.execute(
            """SELECT DISTINCT ON(exchange_date,session)
                      exchange_date,session,observed_at,summary
                 FROM quant.market_snapshot_runs
                WHERE exchange_date BETWEEN %s AND %s
                  AND session IN ('midday','close')
                  AND status IN ('ready','degraded')
                ORDER BY exchange_date,session,observed_at DESC""",
            (start_date, end_date),
        ).fetchall()

        for row in minute_rows:
            result = _persist_intraday_market_flow_feature_on(connection, row["snapshot_minute"], row["observed_at"])
            result_status = str(result.get("status") or "insufficient")
            minute_counts[result_status] = minute_counts.get(result_status, 0) + 1

        for row in snapshot_rows:
            result = persist_market_snapshot_flow_feature(
                connection,
                session=str(row["session"]),
                exchange_date=row["exchange_date"],
                observed_at=row["observed_at"],
                summary=dict(row["summary"] or {}),
            )
            result_status = str(result.get("status") or "insufficient")
            snapshot_counts[result_status] = snapshot_counts.get(result_status, 0) + 1

    sector_daily = rebuild_sector_flow_daily_features(database, start_date, end_date)
    return {
        "status": "completed",
        "start_date": str(start_date),
        "end_date": str(end_date),
        "source": "stored_evidence_only",
        "provider_calls": 0,
        "minute_rows": len(minute_rows),
        "minute_status_counts": minute_counts,
        "snapshot_rows": len(snapshot_rows),
        "snapshot_status_counts": snapshot_counts,
        "sector_daily": sector_daily,
        "decision_eligible": False,
    }


__all__ = [
    "persist_intraday_market_flow_feature",
    "persist_market_snapshot_flow_feature",
    "rebuild_stored_market_flow_features",
]
