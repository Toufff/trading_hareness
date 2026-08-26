"""Prior-session limit-up names as a watchlist universe source.

This is the densest forward-looking universe available anywhere in this
system, and it is deliberately *not* an entry signal.  Both halves of that
sentence are measured, over 156 sessions and 852,380 symbol-days from
2026-01-01 to 2026-08-25.

Density - close-to-close, which is what a naive study reports:

    set                          n        next-day limit-up   next-day return
    whole market            852,380                   1.58%           -0.015%
    limit-up yesterday       13,537                  20.20%           +1.648%
    one-word board            839                    61.50%           +5.388%

A 12.8x and 38.9x lift looks like an edge.  It is not a tradable one, because
that return is realised between yesterday's close and today's close and the
entry it assumes is unavailable:

    set                    open already locked      buyable    open->close
    limit-up yesterday                   8.23%       12,423        +0.006%
    one-word board                      55.42%          374        -0.689%

A one-word board opens locked at the limit more than half the time - there is
no fill - and on the days it can be bought it loses 0.689% from open to close.
The whole gross number lives in the overnight gap.  Even the close-to-close
figure overstates what was achievable, since buying at a limit-locked close is
itself usually impossible.

So this module proposes a universe, not a trade.  Its value is that 20% of
these names touch the limit again, which is where intraday timing rules have
something to work with - and today the 36-name watchlist caught none of the 52
names that limit-up'd (2026-08-26), nor any of the top ten market movers.
Whether an intraday entry inside this universe beats the open is untested:
that needs minute history this deployment does not yet have.
"""

from __future__ import annotations

from datetime import date
from typing import Any

STRATEGY_KEY = "limit_up_continuation"
MODEL_VERSION = "limit-up-continuation-v1"
PROPOSAL_SOURCE = "limit_up_continuation"
#: Measured over 2026-01-01..2026-08-25; carried in evidence so a consumer
#: never has to take the lift on trust.
MEASURED = {
    "window": "2026-01-01..2026-08-25 (156 sessions, 852,380 symbol-days)",
    "market_next_day_limit_up_rate": 0.0158,
    "limit_up_next_day_limit_up_rate": 0.2020,
    "one_word_next_day_limit_up_rate": 0.6150,
    "limit_up_open_locked_rate": 0.0823,
    "one_word_open_locked_rate": 0.5542,
    "limit_up_open_to_close_pct": 0.006,
    "one_word_open_to_close_pct": -0.689,
    "note": "close-to-close lift is large; open-to-close edge is absent. "
            "universe source only, never an entry instruction",
}


def prior_session_limit_ups(connection: Any, as_of_date: date) -> list[dict[str, Any]]:
    """Names that closed locked at the upper limit on the most recent session.

    Derived from canonical daily bars rather than a vendor pool so the set is
    reproducible from data already under quality control.  ``one_word`` marks
    the boards that never traded away from the limit - the highest-density and
    least-tradable subset, flagged so a consumer can tell the two apart.
    """
    rows = connection.execute(
        """WITH latest AS (
             SELECT max(trading_date) AS trading_date FROM quant.canonical_bars_daily
              WHERE trading_date <= %s AND volume > 0
           )
           SELECT b.symbol, b.trading_date, b.close, b.limit_up, b.open, b.high, b.low,
                  b.volume, b.amount,
                  (b.open = b.high AND b.high = b.low) AS one_word,
                  (SELECT count(*) FROM quant.canonical_bars_daily p
                    WHERE p.symbol = b.symbol AND p.trading_date <= b.trading_date
                      AND p.trading_date > b.trading_date - INTERVAL '20 days'
                      AND p.limit_up IS NOT NULL AND p.close >= p.limit_up - 0.005
                  ) AS limit_ups_20d
             FROM quant.canonical_bars_daily b JOIN latest ON latest.trading_date = b.trading_date
            WHERE b.limit_up IS NOT NULL AND b.volume > 0
              AND NOT coalesce(b.is_suspended, false)
              AND b.close >= b.limit_up - 0.005
            ORDER BY b.symbol""",
        (as_of_date,),
    ).fetchall()
    return [dict(row) for row in rows]


def build_proposals(candidates: list[dict[str, Any]], liquidity: dict[str, dict[str, Any]],
                    traded_value: dict[str, float | None]) -> dict[str, Any]:
    """Screen for liquidity and rank by traded value.

    Ordering is by traded value because the study found no feature that ranks
    *within* the set on a tradable basis - one-word boards score highest on
    continuation and worst on achievable return, so ranking by continuation
    would put the least buyable names first.  Traded value at least orders by
    how much size the names can absorb.
    """
    selected: list[dict[str, Any]] = []
    excluded = {"illiquid": 0}
    for candidate in candidates:
        symbol = str(candidate["symbol"])
        screen = liquidity.get(symbol) or {"eligible": False, "flags": ["liquidity_context_missing"]}
        if not screen["eligible"]:
            excluded["illiquid"] += 1
            continue
        selected.append({
            "symbol": symbol,
            "median_daily_amount": traded_value.get(symbol),
            "one_word": bool(candidate.get("one_word")),
            "evidence": {
                "proposal_source": PROPOSAL_SOURCE,
                "prior_session": str(candidate["trading_date"]),
                "one_word_board": bool(candidate.get("one_word")),
                "limit_ups_last_20_sessions": int(candidate.get("limit_ups_20d") or 1),
                "median_daily_amount_yuan": traded_value.get(symbol),
                "liquidity_flags": screen["flags"],
                "selection_basis": "closed locked at the upper limit on the prior session",
                "measured": MEASURED,
                "tradability_warning": (
                    "次日开盘即封概率 8.23%（一字板 55.42%）；可买入者开盘到收盘平均 "
                    "+0.006%（一字板 -0.689%）。此为观察池来源，不是入场信号。"
                ),
            },
        })
    selected.sort(key=lambda item: (item["median_daily_amount"] is not None,
                                    item["median_daily_amount"] or 0.0), reverse=True)
    return {"selected": selected, "eligible_total": len(selected),
            "considered": len(candidates), "excluded": excluded,
            "one_word_count": sum(1 for item in selected if item["one_word"])}


__all__ = [
    "MEASURED", "MODEL_VERSION", "PROPOSAL_SOURCE", "STRATEGY_KEY",
    "build_proposals", "prior_session_limit_ups",
]
