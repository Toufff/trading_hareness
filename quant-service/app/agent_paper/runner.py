"""Intraday loop: match resting orders every pass, ask the model on a fixed cadence.

No database transaction is held across a provider fetch or a model call.  Each
ledger change happens in one short transaction with the account row locked.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta
from decimal import Decimal
from typing import Any, Awaitable, Callable
from zoneinfo import ZoneInfo

from . import repository as repo
from .baseline import build_baseline
from .context import build_context, fetch_live_quotes
from .model import ModelFailure
from .rules import (CENT, MAX_FOCUS_SYMBOLS, MAX_ORDERS_PER_DECISION, SYMBOL_RE, apply_fill, dec, equity, fees_for,
                    limit_crossed, normalize_order, tradability_reasons, walk_book)

SHANGHAI = ZoneInfo("Asia/Shanghai")
MORNING = (time(9, 30), time(11, 30))
AFTERNOON = (time(13, 0), time(15, 0))
DECISION_CUTOFFS = ((time(9, 30), time(11, 27)), (time(13, 0), time(14, 56)))


def in_session(now: datetime) -> bool:
    local = now.astimezone(SHANGHAI).time()
    return MORNING[0] <= local <= MORNING[1] or AFTERNOON[0] <= local <= AFTERNOON[1]


def decision_window(now: datetime) -> bool:
    local = now.astimezone(SHANGHAI).time()
    return any(start <= local <= end for start, end in DECISION_CUTOFFS)


def decision_due(now: datetime, last: datetime | None, minutes: int) -> bool:
    return decision_window(now) and (last is None or now - last >= timedelta(minutes=minutes) - timedelta(seconds=5))


@dataclass
class Runner:
    database: Any
    account_key: str
    model: Any
    fetch_quotes: Callable[[list[str]], Awaitable[dict[str, dict[str, Any]]]] = fetch_live_quotes
    context_builder: Callable[..., Awaitable[tuple[dict[str, Any], dict[str, dict[str, Any]]]]] = build_context
    decision_minutes: int = 5
    clock: Callable[[], datetime] = lambda: datetime.now(SHANGHAI)
    start_at: datetime = datetime(2000, 1, 1, tzinfo=SHANGHAI)
    last_match_at: dict[str, datetime] = field(default_factory=dict)

    def _tx(self):
        return self.database.transaction()

    # ---- ledger mutation -------------------------------------------------------------------------------------
    def _execute(self, connection: Any, *, account: dict[str, Any], order: dict[str, Any], order_id: str,
                 quote: dict[str, Any] | None, now: datetime, fill_price: Decimal | None = None) -> dict[str, Any]:
        """Fill ``order_id`` now from the live book, or at ``fill_price`` for a crossed resting limit."""
        day = now.astimezone(SHANGHAI).date()
        side, symbol, quantity = order["action"], order["symbol"], int(order["quantity"])
        positions = {row["symbol"]: row for row in repo.load_positions(connection, self.account_key)}
        position = positions.get(symbol)
        if fill_price is None:
            reasons = tradability_reasons(side, quote, sellable=int((position or {}).get("sellable_quantity") or 0), quantity=quantity)
            if reasons:
                return {"filled": 0, "reasons": reasons}
            filled, price = walk_book(side, quantity, quote or {}, order.get("limit_price"))
            if not filled or price is None:
                return {"filled": 0, "reasons": ["limit_not_marketable" if order.get("limit_price") is not None else "no_depth"]}
        else:
            sellable = int((position or {}).get("sellable_quantity") or 0)
            if side == "sell" and sellable < quantity:
                return {"filled": 0, "reasons": ["t_plus_one_or_insufficient_sellable_quantity"]}
            filled, price = quantity, fill_price
        fees = fees_for(side, filled, price)
        cash = dec(account["cash"])
        if side == "buy" and cash < price * filled + fees:
            affordable = int((cash - fees) / price / 100) * 100
            if affordable <= 0:
                return {"filled": 0, "reasons": ["insufficient_cash"]}
            filled, fees = affordable, fees_for(side, affordable, price)
        new_cash, new_position = apply_fill(cash, position, side=side, quantity=filled, price=price, fees=fees)
        repo.record_fill(connection, account_key=self.account_key, order_id=order_id, symbol=symbol,
                         name=(quote or {}).get("name") or (position or {}).get("name"), side=side, quantity=filled,
                         price=price.quantize(Decimal("0.0001")), fees=fees, cash=new_cash.quantize(CENT), position=new_position,
                         filled_at=now, trading_date=day, requested=quantity)
        account["cash"] = new_cash
        return {"filled": filled, "price": float(price), "fees": float(fees)}

    # ---- passes ---------------------------------------------------------------------------------------------
    async def match_open_orders(self, now: datetime) -> list[dict[str, Any]]:
        with self._tx() as connection:
            orders = repo.open_orders(connection, self.account_key)
        if not orders:
            return []
        quotes = await self.fetch_quotes([o["symbol"] for o in orders])
        results = []
        with self._tx() as connection:
            account = repo.load_account(connection, self.account_key, for_update=True)
            for order in repo.open_orders(connection, self.account_key):
                spec = {"action": order["side"], "symbol": order["symbol"], "quantity": order["quantity"],
                        "limit_price": dec(order["limit_price"])}
                quote = quotes.get(order["symbol"])
                result = self._execute(connection, account=account, order=spec, order_id=str(order["order_id"]), quote=quote, now=now)
                if not result["filled"]:
                    since = self.last_match_at.get(str(order["order_id"]), order["placed_at"])
                    low, high = repo.quote_range_since(connection, order["symbol"], since, now)
                    if limit_crossed(order["side"], spec["limit_price"], low=low, high=high):
                        result = self._execute(connection, account=account, order=spec, order_id=str(order["order_id"]),
                                               quote=quote, now=now, fill_price=spec["limit_price"])
                self.last_match_at[str(order["order_id"])] = now
                results.append({"order_id": str(order["order_id"]), **result})
        return results

    async def decide(self, now: datetime) -> dict[str, Any]:
        day = now.astimezone(SHANGHAI).date()
        with self._tx() as connection:
            account = repo.load_account(connection, self.account_key)
            positions = repo.load_positions(connection, self.account_key)
            open_rows = repo.open_orders(connection, self.account_key)
            today = repo.day_orders(connection, self.account_key, day)
            recent = repo.recent_decisions(connection, self.account_key)
        context, quotes = await self.context_builder(self.database.transaction, now=now, account=account, positions=positions,
                                                     open_orders=open_rows, today_orders=today, recent_decisions=recent)
        context_json = json.dumps(context, ensure_ascii=False, default=str, separators=(",", ":"))
        context_hash = hashlib.sha256(context_json.encode("utf-8")).hexdigest()
        try:
            result = await asyncio.to_thread(self.model.decide, context_json)
        except ModelFailure as failure:
            with self._tx() as connection:
                repo.insert_decision(connection, account_key=self.account_key, decided_at=now, trading_date=day, status="model_failed",
                                     model=self.model.model, context_hash=context_hash, context_chars=len(context_json), output=None,
                                     error=f"{failure.code}: {failure.detail}", usage=None, duration_ms=None,
                                     context=context, transcript=failure.transcript)
            return {"status": "model_failed", "error": failure.code}
        # Orders execute against the book as it stands after the model answered.
        executed_at = max(self.clock(), now)
        output = result.output
        raw_orders = output.get("orders") if isinstance(output.get("orders"), list) else []
        order_symbols = [str(o.get("symbol") or "").upper() for o in raw_orders if isinstance(o, dict)]
        live = await self.fetch_quotes([s for s in order_symbols if SYMBOL_RE.match(s)]) if order_symbols else {}
        outcomes = []
        with self._tx() as connection:
            account = repo.load_account(connection, self.account_key, for_update=True)
            decision_id = repo.insert_decision(
                connection, account_key=self.account_key, decided_at=now, trading_date=day, status="decided", model=result.model,
                context_hash=context_hash, context_chars=len(context_json), output=output, error=None, usage=result.usage,
                duration_ms=result.duration_ms, context=context, transcript=result.transcript)
            focus = [s.upper() for s in output.get("focus_symbols") or [] if isinstance(s, str) and SYMBOL_RE.match(s.upper())]
            repo.update_memory(connection, self.account_key, {"notes": str(output.get("notes") or "")[:1200],
                                                              "focus_symbols": focus[:MAX_FOCUS_SYMBOLS]})
            positions = {row["symbol"]: row for row in repo.load_positions(connection, self.account_key)}
            open_ids = {str(o["order_id"]) for o in repo.open_orders(connection, self.account_key)}
            # Sells first so same-day proceeds can fund buys, as a broker allows.
            ordered = sorted(raw_orders[:MAX_ORDERS_PER_DECISION], key=lambda o: 0 if isinstance(o, dict) and o.get("action") != "buy" else 1)
            for raw in ordered:
                check = normalize_order(raw, positions=positions, open_order_ids=open_ids)
                if check.order is None:
                    outcomes.append({"order": raw, "status": "rejected", "reasons": check.reasons})
                    continue
                order = check.order
                if order["action"] == "cancel":
                    repo.close_order(connection, order["order_id"], "cancelled", executed_at)
                    outcomes.append({"order_id": order["order_id"], "status": "cancelled"})
                    continue
                quote = live.get(order["symbol"])
                quote_summary = {k: quote.get(k) for k in ("price", "pre_close", "bids", "asks", "trade_time")} if quote else {}
                order_id = repo.insert_order(connection, account_key=self.account_key, decision_id=decision_id, trading_date=day,
                                             order=order, status="open", placed_at=executed_at, reject_reasons=[],
                                             quote=quote_summary, name=(quote or {}).get("name"))
                result_fill = self._execute(connection, account=account, order=order, order_id=order_id, quote=quote, now=executed_at)
                if result_fill["filled"]:
                    status = "filled" if result_fill["filled"] == order["quantity"] else "partially_filled"
                elif order["order_type"] == "limit" and result_fill["reasons"] == ["limit_not_marketable"] and in_session(executed_at):
                    status = "open"
                else:
                    repo.close_order(connection, order_id, "rejected", executed_at, result_fill["reasons"])
                    status = "rejected"
                outcomes.append({"order_id": order_id, "symbol": order["symbol"], "side": order["action"], "status": status, **result_fill})
                positions = {row["symbol"]: row for row in repo.load_positions(connection, self.account_key)}
            repo.record_outcomes(connection, decision_id, outcomes)
        return {"status": "decided", "decision_id": decision_id, "orders": outcomes, "duration_ms": result.duration_ms,
                "market_view": output.get("market_view")}

    async def snapshot_nav(self, now: datetime, price_basis: str = "live_quote", *, attempts: int = 1,
                           retry_seconds: float = 30, sleep: Callable[[float], Awaitable[None]] = asyncio.sleep) -> dict[str, Any]:
        """Record equity only when every position has a live price; never substitute cost."""
        with self._tx() as connection:
            positions = [p for p in repo.load_positions(connection, self.account_key) if int(p["quantity"]) > 0]
        symbols = [p["symbol"] for p in positions]
        prices: dict[str, Decimal] = {}
        for attempt in range(max(1, attempts)):
            quotes = await self.fetch_quotes([s for s in symbols if s not in prices]) if symbols else {}
            prices.update({s: dec(q.get("price")) for s, q in quotes.items() if dec(q.get("price")) > 0})
            if all(s in prices for s in symbols):
                break
            if attempt + 1 < attempts:
                await sleep(retry_seconds)
        missing = [s for s in symbols if s not in prices]
        if missing:
            return {"equity": None, "missing": missing, "recorded": False}
        with self._tx() as connection:
            account = repo.load_account(connection, self.account_key)
            total, market_value, _ = equity(dec(account["cash"]), positions, prices)
            repo.insert_nav(connection, account_key=self.account_key, as_of=now, trading_date=now.astimezone(SHANGHAI).date(),
                            cash=dec(account["cash"]), market_value=market_value.quantize(CENT), equity=total.quantize(CENT),
                            price_basis=price_basis,
                            positions=[{"symbol": p["symbol"], "quantity": int(p["quantity"]),
                                        "price": str(prices[p["symbol"]]), "avg_cost": str(p["average_cost"])} for p in positions])
        return {"equity": float(total), "missing": [], "recorded": True}

    def start_of_day(self, now: datetime) -> bool:
        with self._tx() as connection:
            return repo.roll_t_plus_one(connection, self.account_key, now.astimezone(SHANGHAI).date())

    def end_of_day(self, now: datetime) -> int:
        with self._tx() as connection:
            return repo.expire_orders(connection, self.account_key, now.astimezone(SHANGHAI).date(), now)


def initialize_account(database: Any, *, account_key: str, model: str, source_account: str, start_at: datetime) -> dict[str, Any]:
    start_date = start_at.astimezone(SHANGHAI).date()
    with database.transaction() as connection:
        baseline = build_baseline(connection, source_account=source_account, start_at=start_at)
        closes = connection.execute(
            """SELECT DISTINCT ON (symbol) symbol,close FROM quant.canonical_bars_daily
                WHERE symbol=ANY(%s) AND trading_date<%s ORDER BY symbol,trading_date DESC""",
            ([p["symbol"] for p in baseline["positions"]], start_date),
        ).fetchall()
        prices = {row["symbol"]: dec(row["close"]) for row in closes}
        # A same-day snapshot's own prices are what the human's equity was measured at.
        prices.update({p["symbol"]: dec(p["snapshot_price"]) for p in baseline["positions"] if p.get("snapshot_price")})
        initial, _, missing = equity(dec(baseline["cash"]), baseline["positions"], prices)
        if missing:
            raise ValueError(f"no starting price for {','.join(missing)}; refusing an unpriced starting equity")
        baseline["initial_equity_basis"] = "baseline cash + same-day snapshot prices (else previous closes)"
        repo.create_account(connection, account_key=account_key, model=model, start_date=start_date, baseline=baseline,
                            initial_equity=initial.quantize(CENT))
    return {**baseline, "initial_equity": initial.quantize(CENT)}


async def run_day(runner: Runner, *, now_fn: Callable[[], datetime], sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
                  log: Callable[[dict[str, Any]], None] = lambda _: None, pass_seconds: int = 60) -> dict[str, Any]:
    """Run one trading day until 15:01; safe to restart mid-session."""
    now = now_fn()
    day = now.astimezone(SHANGHAI).date()
    end = datetime.combine(day, time(15, 1), SHANGHAI)
    runner.start_of_day(now)
    decisions = 0
    with runner._tx() as connection:
        last = repo.last_decision_at(connection, runner.account_key)
    last_nav: datetime | None = None
    while now < end:
        if in_session(now):
            try:
                matched = await runner.match_open_orders(now)
                if matched:
                    log({"event": "match", "at": now.isoformat(), "results": matched})
                if decision_due(now, last, runner.decision_minutes) and now >= runner.start_at:
                    outcome = await runner.decide(now)
                    last, decisions = now, decisions + 1
                    log({"event": "decision", "at": now.isoformat(), **outcome})
                if last_nav is None or now - last_nav >= timedelta(minutes=5):
                    await runner.snapshot_nav(now)
                    last_nav = now
            except Exception as error:  # noqa: BLE001 - one failed pass must not end the trading day
                log({"event": "pass_failed", "at": now.isoformat(), "error": f"{type(error).__name__}: {str(error)[:300]}"})
        await sleep(pass_seconds)
        now = now_fn()
    expired = runner.end_of_day(now)
    # Right at 15:00 the quote endpoint can return an empty book; retry for a few minutes.
    nav = await runner.snapshot_nav(now, price_basis="close_live_quote", attempts=6, retry_seconds=30, sleep=sleep)
    summary = {"event": "day_end", "at": now.isoformat(), "decisions": decisions, "expired_orders": expired, **nav}
    log(summary)
    return summary


__all__ = ["Runner", "decision_due", "decision_window", "in_session", "initialize_account", "run_day"]
