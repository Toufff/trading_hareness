"""Evidence gathering for one discipline plan: read-only database + live quotes.

``generator.generate`` is pure, so everything it needs has to be frozen first.
This module is the only place in the package that touches I/O, and it never
writes: every statement here is a ``SELECT``.  What it returns is a
``GenerationInputs`` whose ``fingerprint()`` is the ``inputs_hash`` stored with
the plan, so a later reviewer can prove the plan came from exactly this
evidence.

Fail closed, never invent: a missing broker snapshot, a missing sector
membership, an unavailable live quote and a not-yet-migrated plan table each
produce ``None`` plus a recorded reason, not a guessed value.
"""

from __future__ import annotations

import hashlib
import json
from datetime import date, datetime
from typing import Any, Awaitable, Callable
from zoneinfo import ZoneInfo

from ..agent_paper.context import fetch_live_quotes, fetch_minutes
from .generator import CalendarInfo, GenerationInputs
from .templates import DEFAULT_RISK_PER_TRADE_PCT

SHANGHAI = ZoneInfo("Asia/Shanghai")
SECTOR_TAXONOMY = "longhu_ths_industry"
DAILY_BAR_COUNT = 60
CALENDAR_LOOKAHEAD = 10
EXCHANGE = "SSE"
PLANS_TABLE = "discipline_plans"
SECTOR_FLOW_TABLE = "sector_flow_daily_features"
VERIFIED_EXACT = "verified_exact"


# --------------------------------------------------------------------------
# Pure helpers
# --------------------------------------------------------------------------
def canonical_json(payload: Any) -> str:
    """The one canonical JSON form every discipline hash is taken over."""
    return json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)


def inputs_hash(inputs: GenerationInputs | dict[str, Any]) -> str:
    """sha256 of the canonical JSON; identical to ``GenerationInputs.fingerprint``."""
    payload = inputs.model_dump(mode="json") if isinstance(inputs, GenerationInputs) else inputs
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


def _number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if result == result and abs(result) != float("inf") else None


def _as_date(value: Any) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def closure_gaps(sessions: list[date]) -> list[dict[str, Any]]:
    """Consecutive non-trading calendar days between adjacent open sessions.

    An ordinary weekend is two closed days and therefore never forces a holiday
    line; the National Day break is eight and does.  The gap is measured
    between the two sessions the exchange actually opens, not from a weekday
    count, so a make-up Saturday session shortens the gap on its own.
    """
    ordered = sorted({day for day in sessions if isinstance(day, date)})
    gaps: list[dict[str, Any]] = []
    for previous, following in zip(ordered, ordered[1:]):
        closed = (following - previous).days - 1
        if closed >= 1:
            gaps.append({"last_trading_date": previous.isoformat(),
                         "resume_date": following.isoformat(), "closed_days": closed})
    return gaps[:20]


def forming_bar(day: date, quote: dict[str, Any] | None, minutes: dict[str, Any] | None) -> dict[str, Any] | None:
    """Today's unfinished session as one bar, or ``None`` when nothing is live.

    The minute tape gives open/high/low (minute closes, not tick extremes) and
    the depth quote gives the last price and the cumulative turnover.  The bar
    is marked ``forming`` so a reader never mistakes it for a settled row, and
    the extremes are widened to contain open and close so it survives
    ``stage.normalize_bars`` instead of being silently dropped.
    """
    tape = minutes if isinstance(minutes, dict) and _number(minutes.get("last")) is not None else None
    quote = quote if isinstance(quote, dict) else None
    last = _number((quote or {}).get("price"))
    if last is None and tape is not None:
        last = _number(tape.get("last"))
    if last is None or last <= 0:
        return None
    open_ = _number((tape or {}).get("open")) or last
    candidates = [value for value in (last, open_, _number((tape or {}).get("high")),
                                      _number((tape or {}).get("low"))) if value is not None]
    volume_lot = _number((quote or {}).get("cumulative_volume_lot"))
    source = "live_quote+minutes" if quote is not None and tape is not None else (
        "live_quote" if quote is not None else "minutes")
    return {
        "trading_date": day.isoformat(), "open": open_, "high": max(candidates), "low": min(candidates),
        "close": last, "volume": volume_lot * 100 if volume_lot is not None else None,
        "amount": _number((quote or {}).get("cumulative_amount")),
        "synthetic": False, "forming": True, "source": source,
        "vwap": _number((tape or {}).get("vwap")),
    }


def merge_forming_bar(settled: list[dict[str, Any]],
                      forming: dict[str, Any] | None) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Append today's forming bar, replacing a settled row for the same date.

    Returns the merged series and the basis note that goes into the frozen
    inputs: ``settled_only`` is a legitimate outcome (no live quote, after
    hours, provider down), not an error.
    """
    rows = sorted(settled, key=lambda row: str(row.get("trading_date")))
    if forming is None:
        return rows, {"bars_basis": "settled_only", "forming_source": None, "forming_date": None,
                      "settled_bars": len(rows)}
    day = str(forming["trading_date"])
    merged = [row for row in rows if str(row.get("trading_date")) != day] + [dict(forming)]
    merged.sort(key=lambda row: str(row.get("trading_date")))
    return merged, {"bars_basis": "settled_plus_forming", "forming_source": forming.get("source"),
                    "forming_date": day, "settled_bars": len(rows)}


def position_for(positions: list[dict[str, Any]], symbol: str) -> dict[str, Any] | None:
    """The holding row for ``symbol`` in a broker snapshot, if any shares are held."""
    for row in positions:
        if row.get("symbol") == symbol and int(row.get("quantity") or 0) > 0:
            return row
    return None


def account_equity(snapshot: dict[str, Any] | None) -> tuple[Any, Any]:
    """(equity, cash) from a verified snapshot; total_asset wins, else cash + market value."""
    if not snapshot:
        return None, None
    cash = snapshot.get("cash")
    total = snapshot.get("total_asset")
    if total is None and cash is not None and snapshot.get("total_market_value") is not None:
        total = cash + snapshot["total_market_value"]
    return total, cash


# --------------------------------------------------------------------------
# Read-only database access
# --------------------------------------------------------------------------
def _rows(connection: Any, sql: str, params: tuple[Any, ...]) -> list[dict[str, Any]]:
    return [dict(row) for row in connection.execute(sql, params).fetchall()]


def _one(connection: Any, sql: str, params: tuple[Any, ...]) -> dict[str, Any] | None:
    row = connection.execute(sql, params).fetchone()
    return dict(row) if row else None


def table_exists(connection: Any, table: str, *, schema: str = "quant") -> bool:
    """Guard a read against a table whose migration has not been applied yet."""
    row = connection.execute(
        """SELECT 1 AS present FROM information_schema.tables
            WHERE table_schema=%s AND table_name=%s LIMIT 1""", (schema, table),
    ).fetchone()
    return row is not None


def latest_broker_snapshot(connection: Any, account_key: str, at: datetime) -> dict[str, Any] | None:
    """The newest ``verified_exact`` portfolio snapshot available at ``at``."""
    return _one(connection, """
        SELECT snapshot_id,account_key,source,source_snapshot_key,observed_at,verification,
               cash,total_asset,total_market_value,content_hash
          FROM quant.broker_portfolio_snapshots
         WHERE account_key=%s AND verification=%s AND observed_at<=%s
         ORDER BY observed_at DESC LIMIT 1""", (account_key, VERIFIED_EXACT, at))


def broker_positions(connection: Any, snapshot_id: Any) -> list[dict[str, Any]]:
    rows = _rows(connection, """
        SELECT symbol,name,quantity,sellable_quantity,average_cost,market_price,market_value,
               unrealized_pnl,position_weight_pct
          FROM quant.broker_position_snapshots WHERE snapshot_id=%s ORDER BY symbol""", (snapshot_id,))
    for row in rows:
        row["quantity"] = int(row["quantity"] or 0)
        row["sellable_quantity"] = int(row["sellable_quantity"] or 0)
    return rows


def settled_daily_bars(connection: Any, symbol: str, day: date, count: int = DAILY_BAR_COUNT) -> list[dict[str, Any]]:
    """The last ``count`` settled sessions strictly before ``day``.

    Same query shape as ``agent_paper.context.daily_bars`` (one window pass over
    ``quant.canonical_bars_daily``), but it keeps the full date and the volume
    because the stage metrics need both.
    """
    rows = _rows(connection, """
        SELECT symbol,trading_date,open,high,low,close,pre_close,volume,amount,
               is_suspended,quality_status FROM (
          SELECT *, row_number() OVER (PARTITION BY symbol ORDER BY trading_date DESC) AS rn
            FROM quant.canonical_bars_daily WHERE symbol = ANY(%s) AND trading_date < %s) ranked
         WHERE rn <= %s ORDER BY symbol,trading_date""", ([symbol], day, count))
    return [{
        "trading_date": row["trading_date"].isoformat() if isinstance(row["trading_date"], date)
        else str(row["trading_date"]),
        "open": _number(row["open"]), "high": _number(row["high"]), "low": _number(row["low"]),
        "close": _number(row["close"]), "volume": _number(row["volume"]), "amount": _number(row["amount"]),
        "synthetic": False, "forming": False,
    } for row in rows if not row.get("is_suspended")]


def sector_membership(connection: Any, symbol: str, day: date, at: datetime,
                      *, taxonomy: str = SECTOR_TAXONOMY) -> dict[str, Any] | None:
    """Point-in-time industry membership; absent membership disables sector lines."""
    row = _one(connection, """
        SELECT m.taxonomy_key,m.sector_key,s.label,m.effective_from,m.available_at
          FROM quant.sector_membership_history m
          LEFT JOIN quant.sectors s ON s.taxonomy_key=m.taxonomy_key AND s.sector_key=m.sector_key
         WHERE m.taxonomy_key=%s AND m.symbol=%s AND m.effective_from<=%s
           AND (m.effective_to IS NULL OR m.effective_to>=%s) AND m.available_at<=%s
         ORDER BY m.effective_from DESC LIMIT 1""", (taxonomy, symbol, day, day, at))
    if row is None:
        return None
    return {"taxonomy": row["taxonomy_key"], "code": row["sector_key"], "name": row.get("label"),
            "effective_from": str(row.get("effective_from") or "")}


def sector_daily_change(connection: Any, taxonomy: str, sector_key: str, day: date) -> float | None:
    """Settled industry change for ``day``; ``None`` when the feature row is absent."""
    if not table_exists(connection, SECTOR_FLOW_TABLE):
        return None
    row = _one(connection, """
        SELECT change_pct,status FROM quant.sector_flow_daily_features
         WHERE taxonomy_key=%s AND sector_key=%s AND trading_date=%s LIMIT 1""", (taxonomy, sector_key, day))
    return _number(row.get("change_pct")) if row else None


def lane_membership(connection: Any, symbol: str, at: datetime) -> dict[str, Any] | None:
    """Lane attribution and ``formal_state`` from the latest scan that saw ``symbol``.

    The intraday scan wins when it is newer, because it carries the reference
    and support levels a ``new_buy`` plan needs; the post-close candidate is the
    fallback.  Stage classification only records this - it never overrides the
    branch the daily data selected.
    """
    intraday = _one(connection, """
        SELECT run_id,cutoff,result FROM quant.intraday_strategy_scans
         WHERE state='completed' AND cutoff<=%s ORDER BY cutoff DESC LIMIT 1""", (at,))
    for lane in ((intraday or {}).get("result") or {}).get("lanes") or []:
        for item in lane.get("items") or lane.get("top") or []:
            if isinstance(item, dict) and item.get("symbol") == symbol:
                return {
                    "source": "intraday_strategy_scan", "run_id": str(intraday["run_id"]),
                    "observed_at": str(intraday["cutoff"]),
                    "lane": item.get("lane") or lane.get("key"), "formal_state": item.get("formal_state"),
                    "state": item.get("state"), "reference": _number(item.get("reference")),
                    "support": _number(item.get("support")), "reason": item.get("reason") or item.get("current_reason"),
                }
    candidate = _one(connection, """
        SELECT run_id,candidate_type,rank,score,structure,reason_codes,discovered_at
          FROM quant.post_close_strategy_candidates
         WHERE symbol=%s AND discovered_at<=%s ORDER BY discovered_at DESC LIMIT 1""", (symbol, at))
    if candidate is None:
        return None
    metrics = (candidate.get("structure") or {}).get("metrics") or {}
    return {
        "source": "post_close_strategy_candidates", "run_id": str(candidate["run_id"]),
        "observed_at": str(candidate["discovered_at"]), "lane": candidate.get("candidate_type"),
        "formal_state": None, "state": None,
        "reference": _number(metrics.get("resistance_price")), "support": _number(metrics.get("support_price")),
        "reason": ",".join(str(code) for code in (candidate.get("reason_codes") or [])) or None,
    }


def recommendation_note(connection: Any, symbol: str, at: datetime) -> dict[str, Any] | None:
    """The reviewed pool's note for ``symbol`` - context for the human, never a price source."""
    row = _one(connection, """
        SELECT decision_id,as_of_date,result FROM quant.recommendation_pool_decisions
         WHERE created_at<=%s ORDER BY created_at DESC LIMIT 1""", (at,))
    if row is None:
        return None
    for item in (row.get("result") or {}).get("recommended") or []:
        if isinstance(item, dict) and item.get("symbol") == symbol:
            return {"decision_id": str(row["decision_id"]), "as_of_date": str(row.get("as_of_date") or ""),
                    **{key: item.get(key) for key in ("priority", "trigger", "invalidation", "why_now", "stage")}}
    return None


def trading_calendar(connection: Any, day: date, *, lookahead: int = CALENDAR_LOOKAHEAD,
                     exchange: str = EXCHANGE) -> tuple[CalendarInfo, bool]:
    """Next ``lookahead`` open sessions after ``day`` plus whether ``day`` itself is open."""
    rows = _rows(connection, """
        SELECT calendar_date FROM quant.market_trade_calendar
         WHERE exchange=%s AND is_open AND calendar_date>%s ORDER BY calendar_date LIMIT %s""",
                 (exchange, day, lookahead))
    upcoming = [value for value in (_as_date(row["calendar_date"]) for row in rows) if value is not None]
    today = _one(connection, """
        SELECT is_open FROM quant.market_trade_calendar WHERE exchange=%s AND calendar_date=%s""", (exchange, day))
    return CalendarInfo(upcoming_trading_dates=upcoming,
                        closure_gaps=closure_gaps([day, *upcoming])), bool((today or {}).get("is_open"))


def previous_active_plan(connection: Any, account_key: str, symbol: str) -> dict[str, Any] | None:
    """The newest active plan for this account/symbol, or ``None`` before the migration.

    Guarded by ``information_schema`` so a dry run works on a database that has
    not applied ``20260918_0105`` yet.
    """
    if not table_exists(connection, PLANS_TABLE):
        return None
    row = _one(connection, """
        SELECT plan_id,plan_key,as_of_at,trading_date,stage,sizing,lines,status
          FROM quant.discipline_plans
         WHERE account_key=%s AND symbol=%s AND status='active'
         ORDER BY as_of_at DESC LIMIT 1""", (account_key, symbol))
    if row is None:
        return None
    return summarize_previous_plan(row)


def summarize_previous_plan(row: dict[str, Any]) -> dict[str, Any]:
    """Only the two numbers the ratchet rule needs: the prior hard stop and trail."""
    sizing = row.get("sizing") or {}
    trail = None
    for line in row.get("lines") or []:
        if isinstance(line, dict) and line.get("kind") == "trail":
            trail = _number((line.get("action") or {}).get("value"))
    return {"plan_id": str(row["plan_id"]), "plan_key": row.get("plan_key"),
            "as_of_at": str(row.get("as_of_at") or ""), "stage": row.get("stage"),
            "hard_stop": _number(sizing.get("hard_stop")), "trail": trail}


def instrument_name(connection: Any, symbol: str) -> str | None:
    row = _one(connection, "SELECT name FROM quant.instruments WHERE symbol=%s", (symbol,))
    return (row or {}).get("name")


# --------------------------------------------------------------------------
# Orchestration
# --------------------------------------------------------------------------
def gather_evidence(connection: Any, *, account_key: str, symbol: str, as_of: datetime) -> dict[str, Any]:
    """Every read-only fact one plan is derived from, in one pass."""
    day = as_of.astimezone(SHANGHAI).date()
    snapshot = latest_broker_snapshot(connection, account_key, as_of)
    positions = broker_positions(connection, snapshot["snapshot_id"]) if snapshot else []
    holding = position_for(positions, symbol)
    sector = sector_membership(connection, symbol, day, as_of)
    calendar, day_is_open = trading_calendar(connection, day)
    equity, cash = account_equity(snapshot)
    return {
        "trading_day": day, "day_is_open": day_is_open,
        "snapshot": snapshot, "position": holding, "equity": equity, "cash": cash,
        "bars": settled_daily_bars(connection, symbol, day),
        "sector": ({**sector, "change_pct": sector_daily_change(connection, sector["taxonomy"], sector["code"], day)}
                   if sector else None),
        "lane": lane_membership(connection, symbol, as_of),
        "recommendation": recommendation_note(connection, symbol, as_of),
        "calendar": calendar,
        "previous_plan": previous_active_plan(connection, account_key, symbol),
        "name": (holding or {}).get("name") or instrument_name(connection, symbol),
    }


async def live_forming_bar(symbol: str, day: date, *,
                           fetch_quotes: Callable[[list[str]], Awaitable[dict[str, dict[str, Any]]]] = fetch_live_quotes,
                           fetch_minute: Callable[[list[str], date], Awaitable[dict[str, dict[str, Any]]]] = fetch_minutes,
                           ) -> dict[str, Any] | None:
    """Today's unfinished bar from the licensed live reads; ``None`` when unavailable."""
    try:
        quotes = await fetch_quotes([symbol])
    except Exception:  # noqa: BLE001 - an absent quote degrades to settled-only, never to a guess
        quotes = {}
    try:
        minutes = await fetch_minute([symbol], day)
    except Exception:  # noqa: BLE001
        minutes = {}
    tape = minutes.get(symbol)
    return forming_bar(day, quotes.get(symbol), tape if isinstance(tape, dict) and "status" not in tape else None)


def build_generation_inputs(*, run_id: str, account_key: str, symbol: str, as_of: datetime,
                            evidence: dict[str, Any], forming: dict[str, Any] | None = None,
                            risk_per_trade_pct: Any = DEFAULT_RISK_PER_TRADE_PCT,
                            lowered_reason: str | None = None) -> GenerationInputs:
    """Freeze gathered evidence into the hashable input record.  Pure."""
    bars, basis = merge_forming_bar(evidence.get("bars") or [], forming)
    snapshot = evidence.get("snapshot") or {}
    position = evidence.get("position")
    refs = [f"bars_basis:{basis['bars_basis']}"]
    if basis["forming_source"]:
        refs.append(f"forming_bar:{basis['forming_source']}:{basis['forming_date']}")
    if evidence.get("recommendation"):
        refs.append(f"recommendation_decision:{evidence['recommendation']['decision_id']}")
    if snapshot.get("source_snapshot_key"):
        refs.append(f"broker_snapshot_key:{snapshot['source_snapshot_key']}")
    return GenerationInputs(
        run_id=run_id, account_key=account_key, symbol=symbol,
        name=evidence.get("name") or symbol, as_of=as_of, bars=bars,
        equity=evidence["equity"], cash=evidence.get("cash"),
        position=({"snapshot_id": str(snapshot.get("snapshot_id") or "unknown-snapshot"),
                   "observed_at": snapshot.get("observed_at") or as_of,
                   "quantity": int(position.get("quantity") or 0),
                   "sellable_quantity": int(position.get("sellable_quantity") or 0),
                   "average_cost": position.get("average_cost"),
                   "market_price": position.get("market_price"),
                   "market_value": position.get("market_value")} if position else None),
        sector=evidence.get("sector"), lane=evidence.get("lane"),
        calendar=evidence.get("calendar") or CalendarInfo(),
        previous_plan=evidence.get("previous_plan"),
        risk_per_trade_pct=risk_per_trade_pct, lowered_reason=lowered_reason,
        evidence_refs=refs,
    )


async def collect(connection_factory: Callable[[], Any], *, run_id: str, account_key: str, symbol: str,
                  as_of: datetime, risk_per_trade_pct: Any = DEFAULT_RISK_PER_TRADE_PCT,
                  lowered_reason: str | None = None, allow_live: bool = True,
                  **live_sources: Any) -> GenerationInputs:
    """Read the database once, add today's forming bar when the session is open."""
    with connection_factory() as connection:
        evidence = gather_evidence(connection, account_key=account_key, symbol=symbol, as_of=as_of)
    forming = None
    if allow_live and evidence["day_is_open"]:
        forming = await live_forming_bar(symbol, evidence["trading_day"], **live_sources)
    return build_generation_inputs(run_id=run_id, account_key=account_key, symbol=symbol, as_of=as_of,
                                   evidence=evidence, forming=forming,
                                   risk_per_trade_pct=risk_per_trade_pct, lowered_reason=lowered_reason)


__all__ = [
    "CALENDAR_LOOKAHEAD", "DAILY_BAR_COUNT", "SECTOR_TAXONOMY", "account_equity", "broker_positions",
    "build_generation_inputs", "canonical_json", "closure_gaps", "collect", "forming_bar",
    "gather_evidence", "instrument_name", "inputs_hash", "lane_membership", "latest_broker_snapshot",
    "live_forming_bar", "merge_forming_bar", "position_for", "previous_active_plan",
    "recommendation_note", "sector_daily_change", "sector_membership", "settled_daily_bars",
    "summarize_previous_plan", "table_exists", "trading_calendar",
]
