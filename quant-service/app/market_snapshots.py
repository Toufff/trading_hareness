"""Pure calculations for bounded whole-market snapshot runs."""

from __future__ import annotations

from statistics import median
from typing import Any


PUBLIC_QUOTE_PROVIDERS = frozenset({"eastmoney_free", "tencent_free", "sina_free"})


def as_number(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number else None


def quote_change_percent(quote: dict[str, Any]) -> float | None:
    explicit = as_number(quote.get("pct_chg"))
    if explicit is not None:
        return explicit
    close, pre_close = as_number(quote.get("close")), as_number(quote.get("pre_close"))
    if close is None or pre_close is None or pre_close == 0:
        return None
    return (close / pre_close - 1.0) * 100.0


def summarize_quotes(quotes: list[dict[str, Any]]) -> dict[str, Any]:
    changes = [change for quote in quotes if (change := quote_change_percent(quote)) is not None]
    amounts = [amount for quote in quotes if (amount := as_number(quote.get("amount"))) is not None and amount >= 0]
    volumes = [volume for quote in quotes if (volume := as_number(quote.get("vol"))) is not None and volume >= 0]
    return {
        "quoted_symbols": len(quotes),
        "priced_symbols": len(changes),
        "advancers": sum(change > 0 for change in changes),
        "decliners": sum(change < 0 for change in changes),
        "unchanged": sum(change == 0 for change in changes),
        "median_change_pct": round(median(changes), 4) if changes else None,
        "mean_change_pct": round(sum(changes) / len(changes), 4) if changes else None,
        "market_amount": round(sum(amounts), 2) if amounts else None,
        "market_volume": round(sum(volumes), 2) if volumes else None,
    }


def snapshot_status(*, universe_count: int, quote_count: int, minimum_universe: int, minimum_coverage: float,
                    licensed_providers: set[str], observed_providers: set[str]) -> tuple[str, bool, list[str]]:
    flags: list[str] = []
    coverage = quote_count / universe_count if universe_count else 0.0
    if universe_count < minimum_universe:
        flags.append("universe_too_small")
    if coverage < minimum_coverage:
        flags.append("realtime_quote_coverage_below_threshold")
    if not observed_providers:
        flags.append("no_realtime_quotes")
    licensed = bool(licensed_providers.intersection(observed_providers))
    if not licensed:
        flags.append("no_licensed_realtime_market_feed")
    if "universe_too_small" in flags or "realtime_quote_coverage_below_threshold" in flags or "no_realtime_quotes" in flags:
        return "blocked", False, flags
    if not licensed:
        return "degraded", False, flags
    return "ready", True, flags
