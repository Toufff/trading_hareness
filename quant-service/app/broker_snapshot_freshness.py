"""Shared as-of eligibility for broker facts; never initiates a refresh."""
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

SHANGHAI = ZoneInfo("Asia/Shanghai")


def broker_freshness(portfolio, as_of_at, reference_trade_date=None,
                     max_age=timedelta(days=4), future_tolerance=timedelta(minutes=5)):
    observed = (portfolio or {}).get("observed_at")
    if isinstance(observed, str):
        try:
            observed = datetime.fromisoformat(observed.replace("Z", "+00:00"))
        except ValueError:
            observed = None
    age = as_of_at.astimezone(timezone.utc) - observed.astimezone(timezone.utc) if isinstance(observed, datetime) and observed.tzinfo else None
    metadata = (portfolio or {}).get("metadata") or {}
    raw_day = metadata.get("trade_date")
    try:
        trade_date = date.fromisoformat(str(raw_day)) if raw_day else observed.astimezone(SHANGHAI).date() if isinstance(observed, datetime) and observed.tzinfo else None
    except ValueError:
        trade_date = None
    current = bool(portfolio and portfolio.get("verification") == "verified_exact" and age is not None
                   and -future_tolerance <= age <= max_age and trade_date is not None)
    # Preserve the existing decision policy; ingestion itself has no market
    # calendar/quote gate. This does not assert no trades after observed_at.
    if current and reference_trade_date:
        current = trade_date >= reference_trade_date
    return {"current": current, "observed_at": observed, "trade_date": trade_date, "age": age,
            "subsequent_trades": "unknown", "sync_mode": "manual"}
