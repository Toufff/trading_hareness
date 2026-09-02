"""Causal same-minute volume-baseline queries for intraday evidence.

The database functions deliberately work on persisted prior sessions only.
They have no provider dependency and can serve both the live scanner and a
future local event replay without changing their time semantics.
"""

from __future__ import annotations

from typing import Any, Callable


_ALLOWED_SOURCES = (
    "tushare_super_get_rt_min_daily",
    "tushare_super_rt_min_daily",
    "tencent_intraday_minutes",
)


def _profile(bucket: str | None, row: dict[str, Any] | None, *, number: Callable[[Any], float | None]) -> dict[str, Any]:
    if bucket is None:
        return {"status": "invalid_minute_bucket", "sample_days": 0}
    sample_days = int((row or {}).get("sample_days") or 0)
    median_volume = number((row or {}).get("median_volume"))
    return {
        "status": "ready" if sample_days >= 8 and median_volume else "insufficient_history",
        "minute_bucket": bucket,
        "sample_days": sample_days,
        "median_volume": median_volume,
        "minimum_sample_days": 8,
    }


def volume_time_profile(
    symbol: str,
    minute_time: Any,
    as_of_date: Any,
    connection: Any,
    *,
    minute_bucket_fn: Callable[[Any], str | None],
    number: Callable[[Any], float | None],
) -> dict[str, Any]:
    """Build one strictly-prior-day, same-minute volume baseline."""
    return volume_time_profiles(
        {str(symbol): minute_time}, as_of_date, connection,
        minute_bucket_fn=minute_bucket_fn, number=number,
    ).get(str(symbol), {"status": "invalid_minute_bucket", "sample_days": 0})


def volume_time_profiles(
    symbol_minutes: dict[str, Any],
    as_of_date: Any,
    connection: Any,
    *,
    minute_bucket_fn: Callable[[Any], str | None],
    number: Callable[[Any], float | None],
) -> dict[str, dict[str, Any]]:
    """Build explicit watchlist baselines with one ``symbol × minute`` query."""
    buckets = {
        str(symbol): minute_bucket_fn(minute_time)
        for symbol, minute_time in symbol_minutes.items()
        if str(symbol)
    }
    valid_pairs = sorted((symbol, bucket) for symbol, bucket in buckets.items() if bucket is not None)
    rows_by_symbol: dict[str, dict[str, Any]] = {}
    if valid_pairs:
        symbols, minute_buckets = zip(*valid_pairs)
        rows = connection.execute(
            """SELECT m.symbol,m.minute_bucket,count(DISTINCT m.trading_date)::int AS sample_days,
                      percentile_cont(0.5) WITHIN GROUP (ORDER BY m.volume) AS median_volume
                 FROM quant.intraday_minute_sessions m
                 JOIN unnest(%s::text[],%s::text[]) AS wanted(symbol,minute_bucket)
                   ON wanted.symbol=m.symbol AND wanted.minute_bucket=m.minute_bucket
                WHERE m.trading_date<%s
                  AND m.source_name=ANY(%s)
                  AND m.volume IS NOT NULL AND m.volume>0
                GROUP BY m.symbol,m.minute_bucket""",
            (list(symbols), list(minute_buckets), as_of_date, list(_ALLOWED_SOURCES)),
        ).fetchall()
        rows_by_symbol = {str(row["symbol"]): dict(row) for row in rows}
    return {
        symbol: _profile(bucket, rows_by_symbol.get(symbol), number=number)
        for symbol, bucket in buckets.items()
    }


def attach_volume_time_profile(
    minute_feature: dict[str, Any] | None,
    profile: dict[str, Any] | None,
    *,
    number: Callable[[Any], float | None],
) -> dict[str, Any] | None:
    """Add a saved causal profile without changing other minute evidence."""
    if minute_feature is None:
        return None
    profile = dict(profile or {"status": "invalid_minute_bucket", "sample_days": 0})
    current_volume = number(minute_feature.get("minute_volume_lot"))
    if profile.get("status") == "ready" and current_volume is not None and profile.get("median_volume"):
        profile["volume_surprise"] = round(current_volume / float(profile["median_volume"]), 4)
    else:
        profile["volume_surprise"] = None
    return {**minute_feature, "time_bucket_volume_profile": profile}


__all__ = ["attach_volume_time_profile", "volume_time_profile", "volume_time_profiles"]
