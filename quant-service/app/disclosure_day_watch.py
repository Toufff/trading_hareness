"""Next-session scheduled-disclosure watch names for the intraday watchlist.

This is a *capacity allocation* source, not a return forecast.  Its job is to
answer "of the ~700 companies whose report is registered for tomorrow, which
handful is worth spending scarce watchlist slots on", so the intraday rules
that already exist are pointed at names with a known catalyst instead of only
at a hand-curated list.

Measured over 27 sessions, 2026-07-20..2026-08-25, labelling each scheduled
disclosure date with that day's realized move (limit-up detection repaired for
2026-08-24, whose stk_limit cross-section had been left at 161/5549 rows by a
blocked control sync):

    set                                          n        limit-up    >=+7%
    full market                             149566           1.47%    3.78%
    full market, liquidity-eligible         129849           1.65%    4.21%
    scheduled disclosure                      1896           2.74%    6.70%
    scheduled disclosure + liquidity          1642           3.11%    7.43%
      ... and no prior guidance               1141           3.77%    9.03%
      ... and prior guidance exists            501           1.60%    3.79%
      ... and a positive 预告 exists            303           0.66%    3.63%

The split on prior guidance is the whole finding, and it runs opposite to the
obvious intuition.  Guidance is priced on the day it is published, so a name
that already told the market its numbers has no surprise left to deliver: its
disclosure day is statistically indistinguishable from any other day (1.60%
against a 1.65% base), and a *positive* 预告 is actively worse than the base
rate.  Surprise is only possible where the market has not been told yet, so
this module ranks unguided names and excludes guided ones from the ranked
output while still reporting how many were dropped and why.

Honest limits: 27 sessions is one reporting season, and a single season shares
one macro regime, so a 2.3x lift on ~1141 observations is suggestive, not
established.  Nothing here scores a symbol or predicts a direction - the
ranking below is by traded value, a tradability proxy, precisely because the
study found no ranking feature that survives.  These rows are proposals; a
human still decides what enters quant.intraday_watchlists.

That unvalidated ranking is also why ``DEFAULT_TOP_K`` is not small.  The
measured lift belongs to the *eligible set*, not to any ordering within it, so
truncating hard would apply an ordering the study gave no support for and
would discard most of the only effect actually observed.  A worked example of
the cost: replaying 2026-08-25 for the 2026-08-26 session, 共进股份
(603118.SH) was eligible - scheduled that day, no prior guidance - and ranked
11th of 430 by traded value; it then limit-up'd.  That single name is *not* the
reason for the chosen K and must never become one; it is recorded because it
shows the ranking carries little information, which is the argument for a
loose cap plus human review rather than a tight automated one.  The same
replay also shows a real false negative in the other direction: 江铜
(600362.SH) was excluded for prior guidance and limit-up'd anyway, exactly as
a 1.60% - not 0% - rate implies it sometimes will.
"""

from __future__ import annotations

from datetime import date
from typing import Any

DEFAULT_TOP_K = 20
MODEL_VERSION = "disclosure-day-watch-v1"
STRATEGY_KEY = "disclosure_day_watch"
PROPOSAL_SOURCE = "disclosure_day_watch"


def next_trading_session(connection: Any, as_of_date: date) -> date | None:
    """Return the first open session strictly after ``as_of_date``."""
    row = connection.execute(
        """SELECT calendar_date FROM quant.market_trade_calendar
            WHERE calendar_date>%s AND is_open ORDER BY calendar_date LIMIT 1""",
        (as_of_date,),
    ).fetchone()
    return row["calendar_date"] if row else None


def scheduled_disclosures(connection: Any, session: date) -> list[dict[str, Any]]:
    """Names registered to report on ``session`` that have not already reported.

    ``modify_date`` supersedes ``pre_date`` when a company reschedules, so the
    effective scheduled date is the later of the two.  A row whose
    ``actual_date`` is already on or before ``as_of_date`` has published and is
    no longer a forward-looking catalyst; that is filtered by the caller
    through ``actual_date IS NULL OR actual_date>=session``.
    """
    rows = connection.execute(
        """SELECT s.symbol, s.period, s.pre_date, s.modify_date, s.actual_date,
                  (SELECT count(*) FROM quant.earnings_forecasts f
                    WHERE f.symbol=s.symbol AND f.period=s.period) forecast_count,
                  (SELECT count(*) FROM quant.earnings_express e
                    WHERE e.symbol=s.symbol AND e.period=s.period) express_count,
                  (SELECT f.forecast_type FROM quant.earnings_forecasts f
                    WHERE f.symbol=s.symbol AND f.period=s.period
                    ORDER BY f.ann_date DESC LIMIT 1) latest_forecast_type
             FROM quant.disclosure_schedule s
            WHERE coalesce(s.modify_date, s.pre_date)=%s
              AND (s.actual_date IS NULL OR s.actual_date>=%s)
            ORDER BY s.symbol""",
        (session, session),
    ).fetchall()
    return [dict(row) for row in rows]


def rank_disclosure_watch(candidates: list[dict[str, Any]], liquidity: dict[str, dict[str, Any]],
                          traded_value: dict[str, float | None], *, top_k: int = DEFAULT_TOP_K) -> dict[str, Any]:
    """Select the unguided, liquidity-eligible names with the largest traded value.

    Traded value is a tradability proxy and explicitly not a return forecast;
    the event study behind this module found no feature that ranks *within* the
    unguided set, so this only decides which names are worth scarce slots.
    """
    eligible: list[dict[str, Any]] = []
    excluded = {"illiquid": 0, "prior_guidance": 0}
    for candidate in candidates:
        symbol = str(candidate["symbol"])
        guided = bool(candidate.get("forecast_count") or 0) or bool(candidate.get("express_count") or 0)
        screen = liquidity.get(symbol) or {"eligible": False, "flags": ["liquidity_context_missing"]}
        if not screen["eligible"]:
            excluded["illiquid"] += 1
            continue
        if guided:
            excluded["prior_guidance"] += 1
            continue
        eligible.append({
            "symbol": symbol,
            "median_daily_amount": traded_value.get(symbol),
            "evidence": {
                "proposal_source": PROPOSAL_SOURCE,
                "reporting_period": str(candidate["period"]),
                "scheduled_date": str(candidate.get("modify_date") or candidate.get("pre_date")),
                "rescheduled": candidate.get("modify_date") is not None,
                "prior_guidance": False,
                "median_daily_amount_yuan": traded_value.get(symbol),
                "liquidity_flags": screen["flags"],
                "selection_basis": "scheduled disclosure with no prior 预告/快报; ranked by traded value",
                "measured_lift": {
                    "window": "2026-07-20..2026-08-25 (27 sessions)",
                    "same_day_limit_up_rate": 0.0377, "liquid_base_rate": 0.0165,
                    "note": "one reporting season only; not an established edge",
                },
            },
        })
    eligible.sort(key=lambda item: (item["median_daily_amount"] is not None,
                                    item["median_daily_amount"] or 0.0), reverse=True)
    return {"selected": eligible[:max(0, top_k)], "eligible_total": len(eligible),
            "considered": len(candidates), "excluded": excluded}


__all__ = [
    "DEFAULT_TOP_K", "MODEL_VERSION", "PROPOSAL_SOURCE", "STRATEGY_KEY",
    "next_trading_session", "rank_disclosure_watch", "scheduled_disclosures",
]
