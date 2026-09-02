"""Point-in-time indicator construction for xiaojie-leader-flow-v1.

The strategy module is a pure decision function over a snapshot of named
fields.  Nothing produced those fields from live data, so it could only ever be
called by hand with values a human typed.  This module builds them from
evidence the intraday scan already holds.

The critical enabler is that the licensed all-A cross-section fetched by every
scan carries far more than the price we were using: ``open_price``,
``high_price``, ``low_price``, ``prev_price``, cumulative ``volume`` and
``turnover``, for all ~5,500 symbols, every 30 seconds.  From that alone the
session VWAP (turnover/volume), the drawdown from the session high, and every
limit-board state are computable without a single extra provider call.

Scope is deliberately the leader pool - names at or near their limit - rather
than the whole market.  That is the playbook's own domain, it keeps the
per-scan cost bounded on a two-core edge box, and it matches the only
conditional base rate that matters for this strategy: a name that was limit-up
yesterday hit the limit again the next session 18.57% of the time on
2026-08-26, against a 0.94% market-wide rate.

What this module does *not* do: it never decides.  It assembles inputs and
hands them to ``xiaojie_leader_flow.evaluate_snapshot``, which remains the sole
place a decision is made and remains research-only.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Mapping

from .intraday_derived_flow_metrics import SESSION_MINUTES, session_elapsed_minutes
from .market_rules import LIMIT_TOLERANCE, is_at_limit
from .xiaojie_leader_flow import evaluate_snapshot
#: Names within this distance of their limit still belong to the leader pool:
#: a board that broke is exactly what the return-flow and re-seal modes watch.
NEAR_LIMIT_PCT = 3.0
#: Hard bound on how many candidates one scan evaluates.
#: A safety bound on how many names one scan evaluates, not a filter.  It
#: bound at exactly 150 of 159 qualifying names on 2026-08-27's morning
#: session and silently dropped one that later sealed, so the headroom is
#: widened and any truncation is now reported rather than swallowed.
MAX_CANDIDATES = 250
#: A sector needs this many limit-ups before it counts as a 主线板块.
MAIN_SECTOR_MIN_LIMIT_UPS = 3
#: Market-wide sentiment floor: below this share of names above their own MA5,
#: with limit-ups this scarce, the tape is at a 冰点 rather than merely weak.
ICEPOINT_BREADTH_MAX = 0.25
ICEPOINT_MAX_LIMIT_UPS = 20
#: A right-side confirmation needs breadth, volume and direction together.
RIGHT_SIDE_BREADTH_MIN = 0.55
RIGHT_SIDE_VOLUME_RATIO_MIN = 1.0
#: A breakout must clear the prior high on expanded volume, not drift over it.
BREAKOUT_VOLUME_RATIO_MIN = 1.5
#: "Oversold" is measured against the name's own recent range, not a fixed drop.
OVERSOLD_DECLINE_MIN_PCT = 15.0
#: A supplement candidate follows a leader; it is not itself the leader.
SUPPLEMENT_MIN_RANK = 3
#: The deepest sector rank that still counts as front row.  At 2 it excluded
#: rank 3 in a 14-board main sector (002942.SZ, 2026-08-28), which reads as a
#: follow-on in a two-name theme but as core in a broad one.
FRONT_ROW_MAX_RANK = 3
#: "Pulled back to VWAP" means price has returned to within this band of it.
VWAP_BAND_PCT = 1.0
#: ...but only counts as a pullback if the name first traded this far above
#: VWAP.  Without it a 一字板 - where open, high, low and close are the same
#: price, so VWAP is identically that price - satisfies the band trivially and
#: reports a retracement that never happened.  Observed on 2026-08-26:
#: 002084.SZ and 002742.SZ both showed a 0.00% VWAP distance for exactly this
#: reason, and names sitting at their session high qualified alongside them.
PULLBACK_MIN_EXTENSION_PCT = 2.0


def _number(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed == parsed else None


def snapshot_fields(row: Mapping[str, Any]) -> dict[str, float | None]:
    """Lift the OHLC/volume fields the adapter keeps only inside ``raw``."""
    raw = row.get("raw") if isinstance(row.get("raw"), dict) else {}
    return {
        "price": _number(row.get("price")),
        "open": _number(raw.get("open_price")),
        "high": _number(raw.get("high_price")),
        "low": _number(raw.get("low_price")),
        "prev_close": _number(raw.get("prev_price")),
        "volume": _number(row.get("volume")),
        "turnover": _number(row.get("turnover")),
        "pct_change": _number(row.get("pct_change")),
    }


def session_vwap(fields: Mapping[str, Any]) -> float | None:
    """Turnover over volume is the session VWAP; both are cumulative."""
    volume, turnover = fields.get("volume"), fields.get("turnover")
    if not volume or not turnover or volume <= 0:
        return None
    return turnover / volume


def board_state(fields: Mapping[str, Any], limit_up: float | None) -> dict[str, bool]:
    """Classify a name's relationship to its upper limit right now.

    ``touched`` uses the session high, so a board that broke is still
    recognised as having been sealed earlier - which is what separates a
    return-flow candidate from a name that never reached the limit at all.
    """
    price, high = fields.get("price"), fields.get("high")
    if limit_up is None or price is None:
        return {"touched": False, "sealed": False, "broken": False}
    touched = high is not None and is_at_limit(high, limit_up)
    sealed = is_at_limit(price, limit_up)
    return {"touched": touched, "sealed": sealed, "broken": touched and not sealed}


def market_regime_inputs(rows: list[dict[str, Any]], references: Mapping[str, Any],
                        *, market_volume_baseline: float | None,
                        elapsed_session_minutes: int, session_minutes: int = 240) -> dict[str, Any]:
    """Derive the two regime gates from the cross-section itself.

    The playbook states them as index conditions, but a single index level is
    not available inside the scan without another provider call, and one index
    is a narrower statement than the market it stands for.  Both are therefore
    computed market-wide from evidence already in hand, and named for what they
    actually measure:

    ``index_volume_expansion`` compares the market's cumulative volume against
    its own recent daily average, pro-rated for how much of the session has
    elapsed - the same construction the watch-basket volume ratio uses.

    ``index_above_support`` becomes "the median listed name is above its own
    five-day average", which is a breadth-of-trend reading rather than one
    index's level.  It is deliberately stricter than an index print: a
    cap-weighted index can hold its support while most of the market rolls over.
    """
    above = below = 0
    traded_volume = 0.0
    for row in rows:
        symbol = str(row.get("symbol") or "")
        fields = snapshot_fields(row)
        price, volume = fields["price"], fields["volume"]
        if volume:
            traded_volume += volume
        ma5 = (references.get(symbol) or {}).get("ma5")
        if price is None or not ma5:
            continue
        if price >= float(ma5):
            above += 1
        else:
            below += 1
    breadth_above_ma5 = above / (above + below) if (above + below) else None
    volume_ratio = None
    if market_volume_baseline and market_volume_baseline > 0 and elapsed_session_minutes > 0:
        expected = market_volume_baseline * (elapsed_session_minutes / session_minutes)
        if expected > 0:
            volume_ratio = traded_volume / expected
    return {
        "index_volume_ratio": volume_ratio,
        "index_above_support": (breadth_above_ma5 >= 0.5) if breadth_above_ma5 is not None else None,
        "breadth_above_ma5": breadth_above_ma5,
        "market_volume": traded_volume,
        "elapsed_session_minutes": elapsed_session_minutes,
        # Right side: breadth, volume and direction confirming together.  Any
        # one of them alone is the condition the playbook warns about - a
        # breakout with no volume, or volume with no follow-through.
        "right_side_confirmed": bool(
            breadth_above_ma5 is not None and breadth_above_ma5 >= RIGHT_SIDE_BREADTH_MIN
            and volume_ratio is not None and volume_ratio >= RIGHT_SIDE_VOLUME_RATIO_MIN
        ),
    }


def market_gate_inputs(rows: list[dict[str, Any]], *, index_volume_ratio: float | None,
                       index_above_support: bool | None,
                       main_sector_present: bool) -> dict[str, Any]:
    """Market-wide breadth and regime fields, computed once per scan."""
    up = down = 0
    for row in rows:
        pct = _number(row.get("pct_change"))
        if pct is None:
            continue
        if pct > 0:
            up += 1
        elif pct < 0:
            down += 1
    return {
        "breadth_up_count": up, "breadth_down_count": down,
        "index_volume_ratio": index_volume_ratio,
        "index_above_support": index_above_support,
        "main_sector_present": main_sector_present,
    }


def leader_pool(rows: list[dict[str, Any]], limits: Mapping[str, float],
                *, max_candidates: int = MAX_CANDIDATES) -> list[str]:
    """Names at or near their upper limit, strongest first.

    Ordering by how close a name is to its limit means the bound truncates the
    weakest candidates rather than an arbitrary slice.
    """
    scored: list[tuple[float, str]] = []
    for row in rows:
        symbol = str(row.get("symbol") or "")
        limit_up = limits.get(symbol)
        fields = snapshot_fields(row)
        price, high = fields["price"], fields["high"]
        if not symbol or limit_up is None or limit_up <= 0 or price is None:
            continue
        reference = max(price, high) if high is not None else price
        distance = (limit_up - reference) / limit_up * 100
        if distance <= NEAR_LIMIT_PCT:
            scored.append((distance, symbol))
    scored.sort()
    return [symbol for _distance, symbol in scored[:max(0, max_candidates)]]


def sector_context(pool: list[str], rows_by_symbol: Mapping[str, dict[str, Any]],
                   membership: Mapping[str, set[str]],
                   limits: Mapping[str, float]) -> dict[str, Any]:
    """Identify main sectors and rank each candidate inside them.

    A sector qualifies on the count of names actually locked at the limit, not
    on the pool count, so a sector full of names that merely approached the
    board does not read as a leading theme.
    """
    sealed_by_sector: dict[str, int] = {}
    for symbol in pool:
        fields = snapshot_fields(rows_by_symbol.get(symbol) or {})
        if not board_state(fields, limits.get(symbol))["sealed"]:
            continue
        for sector in membership.get(symbol, set()):
            sealed_by_sector[sector] = sealed_by_sector.get(sector, 0) + 1
    main_sectors = {sector for sector, count in sealed_by_sector.items()
                    if count >= MAIN_SECTOR_MIN_LIMIT_UPS}
    ranks: dict[str, int] = {}
    for sector in main_sectors:
        members = [symbol for symbol in pool if sector in membership.get(symbol, set())]
        members.sort(key=lambda symbol: snapshot_fields(rows_by_symbol.get(symbol) or {}).get("pct_change") or -999,
                     reverse=True)
        for position, symbol in enumerate(members, start=1):
            if symbol not in ranks or position < ranks[symbol]:
                ranks[symbol] = position
    # A supplement only makes sense while its sector's leader is still intact:
    # once the rank-1 name breaks, the rotation is a falling tide, not a relay.
    leader_intact: dict[str, bool] = {}
    for sector in main_sectors:
        members = [symbol for symbol in pool if sector in membership.get(symbol, set())]
        if not members:
            continue
        leader = min(members, key=lambda symbol: ranks.get(symbol, 999))
        holding = board_state(snapshot_fields(rows_by_symbol.get(leader) or {}),
                              limits.get(leader))["sealed"]
        for symbol in members:
            leader_intact[symbol] = leader_intact.get(symbol, False) or holding
    return {"main_sectors": main_sectors, "ranks": ranks,
            "sealed_by_sector": sealed_by_sector, "leader_intact": leader_intact,
            "strength_percentile": sector_strength_percentiles(rows_by_symbol, membership)}


def sector_strength_percentiles(rows_by_symbol: Mapping[str, dict[str, Any]],
                                membership: Mapping[str, set[str]],
                                *, min_members: int = 5) -> dict[str, float]:
    """Rank every sector by mean member return, then map each symbol to its best.

    The strategy gates on "is this name in a top-decile sector", which is a
    cross-sectional statement about the whole market and cannot be derived from
    the leader pool alone - a pool of limit-up names would rank every sector
    near the top.  Sectors below ``min_members`` observed constituents are
    excluded: a two-name sector's mean is noise, not strength.
    """
    totals: dict[str, list[float]] = {}
    for symbol, row in rows_by_symbol.items():
        pct = _number(row.get("pct_change"))
        if pct is None:
            continue
        for sector in membership.get(symbol, set()):
            totals.setdefault(sector, []).append(pct)
    means = {sector: sum(values) / len(values)
             for sector, values in totals.items() if len(values) >= min_members}
    if not means:
        return {}
    ordered = sorted(means.items(), key=lambda item: item[1])
    denominator = max(1, len(ordered) - 1)
    sector_percentile = {sector: index / denominator for index, (sector, _mean) in enumerate(ordered)}
    best: dict[str, float] = {}
    for symbol in rows_by_symbol:
        scores = [sector_percentile[sector] for sector in membership.get(symbol, set())
                  if sector in sector_percentile]
        if scores:
            best[symbol] = max(scores)
    return best


def track_ma5_break(state: dict[str, Any], symbol: str, price: float | None,
                    ma5: float | None, observed_at: datetime) -> dict[str, Any]:
    """Maintain how long a name has been below its MA5 within one session.

    The exit rule is "broke MA5 and did not recover within 15 minutes", which
    is not answerable from a single snapshot: it needs the moment the break
    started.  State is keyed per session by the caller and holds only the first
    timestamp below the line, so a name that recovers and breaks again is timed
    from the second break rather than the first.
    """
    if price is None or not ma5:
        return {"duration_minutes": None, "recovered": None}
    if price >= float(ma5):
        state.pop(symbol, None)
        return {"duration_minutes": 0.0, "recovered": True}
    started = state.setdefault(symbol, observed_at)
    return {"duration_minutes": max(0.0, (observed_at - started).total_seconds() / 60.0),
            "recovered": False}


def candidate_snapshot(symbol: str, row: Mapping[str, Any], *, market: Mapping[str, Any],
                       reference: Mapping[str, Any], sectors: Mapping[str, Any],
                       limits: Mapping[str, float],
                       ma5_break: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Assemble one candidate's point-in-time snapshot for the decision function."""
    fields = snapshot_fields(row)
    price = fields["price"]
    limit_up = limits.get(symbol)
    board = board_state(fields, limit_up)
    vwap = session_vwap(fields)
    in_main = bool(reference.get("sectors", set()) & set(sectors.get("main_sectors", set())))
    rank = sectors.get("ranks", {}).get(symbol)

    drawdown = None
    if price is not None and fields["high"]:
        drawdown = (fields["high"] - price) / fields["high"] * 100
    rebound = None
    if board["broken"] and price is not None and fields["low"]:
        rebound = (price - fields["low"]) / fields["low"] * 100
    elapsed_minutes = int(market.get("elapsed_session_minutes") or 0)
    vwap_distance = ((price - vwap) / vwap * 100) if (vwap and price is not None) else None
    # A pullback needs something to pull back from: the session must have
    # extended above VWAP, and price must now have returned toward it.
    extended_above_vwap = bool(
        vwap and fields["high"] is not None
        and (fields["high"] - vwap) / vwap * 100 >= PULLBACK_MIN_EXTENSION_PCT
    )
    pulled_back = bool(
        extended_above_vwap and vwap_distance is not None
        and 0 <= vwap_distance <= VWAP_BAND_PCT
    )

    # --- inputs for the modes added after the first distillation -----------
    ma5 = reference.get("ma5")
    ma20 = reference.get("ma20")
    # "距 5 日线至少 3%" is a distance, and the rule compares it against a
    # positive floor.  A signed value would make the icepoint trial demand a
    # name 3% *above* its MA5, which is the opposite of a left-side entry -
    # and the mode's own left_side_signal already requires price below the
    # line, so magnitude is the meaning that leaves the rule coherent.
    signed_distance_from_ma5 = (((price - float(ma5)) / float(ma5) * 100)
                                if (ma5 and price is not None) else None)
    distance_from_ma5 = abs(signed_distance_from_ma5) if signed_distance_from_ma5 is not None else None
    mean_volume = reference.get("mean_volume_5d")
    own_volume_ratio = None
    if mean_volume and fields["volume"] and elapsed_minutes > 0:
        expected = float(mean_volume) * (elapsed_minutes / SESSION_MINUTES)
        if expected > 0:
            own_volume_ratio = fields["volume"] / expected
    high_20d_value = reference.get("high_20d")
    breakout_confirmed = bool(
        high_20d_value and price is not None and price >= float(high_20d_value)
        and own_volume_ratio is not None and own_volume_ratio >= BREAKOUT_VOLUME_RATIO_MIN
    )
    # Oversold is relative to the name's own 20-session range, so a stock that
    # merely drifted does not qualify alongside one that halved.
    low_20d_value, reference_close = reference.get("low_20d"), reference.get("close_10_sessions_ago")
    decline_pct = (((float(reference_close) - price) / float(reference_close) * 100)
                   if (reference_close and price is not None) else None)
    oversold_rebound = bool(
        decline_pct is not None and decline_pct >= OVERSOLD_DECLINE_MIN_PCT
        and low_20d_value and price is not None and price > float(low_20d_value)
        and fields["prev_close"] and price > float(fields["prev_close"])
    )
    left_side_signal = bool(
        ma5 and price is not None and price < float(ma5)
        and fields["low"] is not None and price > float(fields["low"])
    )
    trend_support_holds = bool(ma20 and price is not None and price >= float(ma20))
    prior = reference.get("prior_bar") or {}
    prior_one_word = bool(
        prior.get("limit_up") and prior.get("open") is not None
        and prior.get("open") == prior.get("high") == prior.get("low") == prior.get("close")
        and is_at_limit(prior["close"], prior["limit_up"])
    )
    reverse_wrap = bool(
        prior.get("open") is not None and price is not None and price > float(prior["open"])
        and prior.get("pre_close") is not None and float(prior["close"]) < float(prior["pre_close"])
    )
    high_20d = reference.get("high_20d")
    breakout = bool(high_20d and price is not None and price >= float(high_20d))
    ma5 = reference.get("ma5")
    opened_below_limit = bool(
        limit_up is not None and fields["low"] is not None
        and not is_at_limit(fields["low"], limit_up)
    )

    return {
        # market gate
        "index_above_support": market.get("index_above_support"),
        "index_volume_ratio": market.get("index_volume_ratio"),
        "breadth_up_count": market.get("breadth_up_count"),
        "breadth_down_count": market.get("breadth_down_count"),
        "main_sector_present": in_main,
        # leader confirmation
        "sector_strength_percentile": sectors.get("strength_percentile", {}).get(symbol),
        "candidate_strength_rank": rank if rank is not None else 99,
        "is_back_row": bool(rank is not None and rank > FRONT_ROW_MAX_RANK),
        # entry modes
        "prior_one_word_board": prior_one_word,
        # A return flow is a process - the board opened and then resealed -
        # which a single instant cannot witness: "broken" and "sealed" are
        # mutually exclusive at any moment, so this pair was structurally
        # unreachable and the mode never fired (002942.SZ's textbook T-board
        # on 2026-08-28 produced no candidate).  The session low is the
        # memory a snapshot already carries: for yesterday's one-word name,
        # any trade below today's limit means the seal opened, so a sealed
        # price above a sub-limit low is the completed return flow - and it
        # survives a collector restart, which an in-process state would not.
        "limit_up_return_flow": opened_below_limit,
        "re_seal_confirmed": board["sealed"] and opened_below_limit,
        "reverse_wrap_confirmed": reverse_wrap,
        "drawdown_from_high_pct": drawdown,
        "post_limitup_break_rebound_pct": rebound,
        "support_or_vwap_holds": bool(vwap_distance is not None and vwap_distance >= 0),
        "leader_pullback_to_vwap": pulled_back,
        "breakout_or_reverse_wrap": breakout or reverse_wrap,
        # right-side breakout
        "index_right_side_confirmed": bool(market.get("right_side_confirmed")),
        "breakout_confirmed": breakout_confirmed,
        # icepoint left-side trial
        "icepoint": bool(market.get("icepoint")),
        "left_side_signal": left_side_signal,
        "distance_from_ma5_pct": distance_from_ma5,
        # oversold rebound
        "oversold_rebound_confirmed": oversold_rebound,
        # supplement rotation
        "supplement_candidate": bool(
            in_main and rank is not None and rank >= SUPPLEMENT_MIN_RANK
            and fields["pct_change"] is not None and fields["pct_change"] > 0
        ),
        "leader_not_broken": bool(sectors.get("leader_intact", {}).get(symbol)),
        # ETF trend - the licensed all-A snapshot is an equity cross-section and
        # carries no funds, so is_etf is never true from this pipeline.  It is
        # emitted as a real False rather than omitted so the mode fails closed
        # instead of reading an absent field as unknown.
        "is_etf": False,
        "trend_support_holds": trend_support_holds,
        # exit context
        "limit_up_break": board["broken"],
        "sector_strength_fades": bool(reference.get("sector_strength_fades")),
        "box_support_broken": bool(ma5 and price is not None and price < float(ma5) * 0.97),
        "ma5_break_duration_minutes": (ma5_break or {}).get("duration_minutes"),
        "ma5_recovered": (ma5_break or {}).get("recovered"),
        "days_without_new_high": reference.get("days_without_new_high"),
        "days_without_rise": reference.get("days_without_rise"),
        # observability, not consumed by the decision function
        "_evidence": {
            "vwap": vwap, "vwap_distance_pct": vwap_distance, "limit_up": limit_up,
            "extended_above_vwap": extended_above_vwap, "pulled_back_to_vwap": pulled_back,
            "candidate_strength_rank": rank,
            "own_volume_ratio": own_volume_ratio,
            "distance_from_ma5_pct": distance_from_ma5,
            "signed_distance_from_ma5_pct": signed_distance_from_ma5,
            "decline_from_10_sessions_pct": decline_pct,
            "board": board, "session_high": fields["high"], "price": price,
            "pct_change": fields["pct_change"],
        },
    }


def evaluate_pool(rows: list[dict[str, Any]], *, limits: Mapping[str, float],
                  membership: Mapping[str, set[str]], references: Mapping[str, Any],
                  observed_at: datetime, ma5_break_state: dict[str, Any] | None = None,
                  market_volume_baseline: float | None = None,
                  elapsed_session_minutes: int | None = None,
                  index_volume_ratio: float | None = None,
                  index_above_support: bool | None = None,
                  max_candidates: int = MAX_CANDIDATES) -> dict[str, Any]:
    """Build snapshots for the leader pool and run the decision function over it."""
    rows_by_symbol = {str(row.get("symbol") or ""): dict(row) for row in rows}
    # Ranked once without the bound so the caller can see when the bound binds:
    # a cap that silently truncates reads as full coverage in every status it
    # reports, which is how the 2026-08-27 drop went unnoticed.
    qualified = leader_pool(rows, limits, max_candidates=len(rows))
    pool = qualified[:max(0, max_candidates)]
    sectors = sector_context(pool, rows_by_symbol, membership, limits)
    # Explicit overrides exist for replay; live scans derive both.
    regime = market_regime_inputs(
        rows, references,
        market_volume_baseline=market_volume_baseline,
        elapsed_session_minutes=(elapsed_session_minutes
                                 if elapsed_session_minutes is not None
                                 else session_elapsed_minutes(observed_at)),
    )
    # A 冰点 is a market-wide state: few boards holding and most names below
    # their own short average.  It is computed from the sealed count rather
    # than the pool size, since a pool of names merely approaching the limit
    # says nothing about how much risk the tape is actually taking.
    sealed_now = sum(1 for symbol in pool
                     if board_state(snapshot_fields(rows_by_symbol[symbol]), limits.get(symbol))["sealed"])
    regime["sealed_limit_ups"] = sealed_now
    regime["icepoint"] = bool(
        sealed_now <= ICEPOINT_MAX_LIMIT_UPS
        and regime["breadth_above_ma5"] is not None
        and regime["breadth_above_ma5"] <= ICEPOINT_BREADTH_MAX
    )
    if index_volume_ratio is None:
        index_volume_ratio = regime["index_volume_ratio"]
    if index_above_support is None:
        index_above_support = regime["index_above_support"]
    market = market_gate_inputs(
        rows, index_volume_ratio=index_volume_ratio, index_above_support=index_above_support,
        main_sector_present=bool(sectors["main_sectors"]),
    )
    market["right_side_confirmed"] = regime["right_side_confirmed"]
    market["icepoint"] = regime["icepoint"]
    market["elapsed_session_minutes"] = regime["elapsed_session_minutes"]
    evaluations: list[dict[str, Any]] = []
    break_state = ma5_break_state if ma5_break_state is not None else {}
    for symbol in pool:
        reference = dict(references.get(symbol) or {})
        reference.setdefault("sectors", membership.get(symbol, set()))
        ma5_break = track_ma5_break(
            break_state, symbol, snapshot_fields(rows_by_symbol[symbol])["price"],
            reference.get("ma5"), observed_at,
        )
        snapshot = candidate_snapshot(symbol, rows_by_symbol[symbol], market=market,
                                      reference=reference, sectors=sectors, limits=limits,
                                      ma5_break=ma5_break)
        evidence = snapshot.pop("_evidence")
        result = evaluate_snapshot(snapshot)
        evaluations.append({"symbol": symbol, "decision": result["decision"], "mode": result["mode"],
                            "position": result["position"], "exit": result["exit"],
                            "risk_flags": result["risk_flags"], "reasons": result["reasons"],
                            "market_gate": result["market_gate"], "evidence": evidence})
    candidates = [item for item in evaluations if item["decision"] == "research_candidate"]
    return {
        "observed_at": observed_at.isoformat(), "pool_size": len(pool),
        "pool_qualified": len(qualified), "pool_truncated": len(qualified) - len(pool),
        "evaluated": len(evaluations), "candidates": candidates,
        "main_sector_count": len(sectors["main_sectors"]),
        "market_gate": market, "regime": regime, "evaluations": evaluations,
    }


__all__ = [
    "BREAKOUT_VOLUME_RATIO_MIN", "FRONT_ROW_MAX_RANK", "ICEPOINT_BREADTH_MAX", "ICEPOINT_MAX_LIMIT_UPS",
    "LIMIT_TOLERANCE", "MAIN_SECTOR_MIN_LIMIT_UPS", "MAX_CANDIDATES", "NEAR_LIMIT_PCT",
    "OVERSOLD_DECLINE_MIN_PCT", "RIGHT_SIDE_BREADTH_MIN", "RIGHT_SIDE_VOLUME_RATIO_MIN",
    "SUPPLEMENT_MIN_RANK",
    "PULLBACK_MIN_EXTENSION_PCT", "VWAP_BAND_PCT", "board_state", "candidate_snapshot", "evaluate_pool", "leader_pool",
    "market_gate_inputs", "market_regime_inputs", "sector_context",
    "sector_strength_percentiles",
    "session_vwap", "snapshot_fields", "track_ma5_break",
]
