"""Point-in-time account, plan, order-book, board and breadth context for a B/S review.

Every projection keeps the timestamp at which the row became observable so the
page can show only what existed before a selected order.  Nothing here writes
to the database or reinterprets a missing mapping as a fact.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo


CN = ZoneInfo("Asia/Shanghai")
TAXONOMY_LABELS = {"eastmoney_industry": "行业", "eastmoney_concept": "概念"}


def _num(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _fetch(connection: Any, sql: str, params: tuple[Any, ...]) -> list[dict[str, Any]]:
    return [dict(row) for row in connection.execute(sql, params).fetchall()]


def _day_bounds(day: date) -> tuple[datetime, datetime]:
    start = datetime.combine(day, time(0), tzinfo=CN)
    return start, start + timedelta(days=1)


def event_time(event: dict[str, Any]) -> datetime:
    return event.get("fill_at") or event["order_at"]


# --------------------------------------------------------------------------- account


def fetch_account_snapshots(connection: Any, *, account_key: str, symbol: str, day: date) -> list[dict[str, Any]]:
    start, end = _day_bounds(day)
    return _fetch(connection, """
        SELECT p.observed_at,p.source,p.verification,p.total_asset,p.cash,p.total_market_value,
               s.quantity,s.sellable_quantity,s.average_cost,s.market_price,s.market_value
          FROM quant.broker_portfolio_snapshots p
          LEFT JOIN quant.broker_position_snapshots s ON s.snapshot_id=p.snapshot_id AND s.symbol=%s
         WHERE p.account_key=%s AND p.observed_at >= %s AND p.observed_at < %s
         ORDER BY p.observed_at LIMIT 200
    """, (symbol, account_key, start - timedelta(days=10), end))


def normalize_account_snapshot(row: dict[str, Any]) -> dict[str, Any]:
    """A verified full-account snapshot without the symbol means zero shares."""
    exact = row.get("verification") == "verified_exact"
    held = row.get("quantity") is not None
    quantity = _num(row.get("quantity")) if held else (0.0 if exact else None)
    total = _num(row.get("total_asset"))
    price = _num(row.get("market_price"))
    value = _num(row.get("market_value"))
    if value is None and quantity is not None and price is not None:
        value = quantity * price
    return {
        "observed_at": row["observed_at"], "source": row.get("source"),
        "verification": row.get("verification"), "total_asset": total,
        "cash": _num(row.get("cash")), "quantity": quantity,
        "sellable_quantity": _num(row.get("sellable_quantity")) if held else (0.0 if exact else None),
        "average_cost": _num(row.get("average_cost")), "market_price": price,
        "weight_pct": round(value / total * 100, 2) if value is not None and total else None,
    }


def build_position_path(snapshots: list[dict[str, Any]], events: list[dict[str, Any]], *,
                        previous_trading_day: date | None) -> dict[str, Any]:
    """Infer quantities from the last pre-trade snapshot plus imported fills.

    The baseline is only called verified when it was captured after the prior
    session closed; an older snapshot may miss trades that were never imported.
    """
    rows = [normalize_account_snapshot(row) for row in snapshots]
    ordered = sorted(events, key=event_time)
    if not ordered:
        return {"status": "no_events", "baseline": None, "snapshots": rows, "events": {}, "reconciliation": []}
    first_at = event_time(ordered[0])
    prior = [row for row in rows if row["observed_at"] < first_at and row["quantity"] is not None]
    baseline = prior[-1] if prior else None
    if baseline is None:
        return {"status": "missing", "baseline": None, "snapshots": rows, "events": {}, "reconciliation": []}
    status = "verified"
    if previous_trading_day is None:
        status = "unverified_age"
    else:
        prior_close = datetime.combine(previous_trading_day, time(15, 0), tzinfo=CN)
        if baseline["observed_at"] < prior_close:
            status = "stale"

    def signed(event: dict[str, Any]) -> float:
        quantity = _num(event.get("filled_quantity")) or 0.0
        return quantity if event.get("side") == "buy" else -quantity

    quantity = baseline["quantity"]
    total = baseline["total_asset"]
    per_event: dict[str, Any] = {}
    for event in ordered:
        before = quantity
        quantity = before + signed(event)
        price = _num(event.get("fill_price")) or _num(event.get("order_price"))
        per_event[event["event_key"]] = {
            "before_quantity": before, "after_quantity": quantity,
            "before_weight_pct_estimate": round(before * price / total * 100, 2) if price and total else None,
            "after_weight_pct_estimate": round(quantity * price / total * 100, 2) if price and total else None,
        }
    reconciliation = []
    for row in rows:
        if row["observed_at"] <= baseline["observed_at"] or row["quantity"] is None:
            continue
        inferred = baseline["quantity"] + sum(signed(event) for event in ordered if event_time(event) <= row["observed_at"])
        reconciliation.append({"observed_at": row["observed_at"], "snapshot_quantity": row["quantity"],
                               "inferred_quantity": inferred, "match": abs(inferred - row["quantity"]) < 1e-6})
    return {"status": status, "baseline": baseline, "snapshots": rows, "events": per_event,
            "reconciliation": reconciliation}


# --------------------------------------------------------------------------- plans


def fetch_trade_plans(connection: Any, *, symbol: str, day: date) -> list[dict[str, Any]]:
    start, end = _day_bounds(day)
    return _fetch(connection, """
        SELECT plan_key,plan_kind,as_of_at,valid_until,action,entry_zone,add_trigger,reduce_trigger,
               exit_trigger,stop_price,target_prices,max_position_pct,rationale,risk_flags,content_hash,created_at
          FROM quant.personal_trade_plans
         WHERE symbol=%s AND created_at < %s AND (valid_until IS NULL OR valid_until >= %s)
         ORDER BY created_at LIMIT 50
    """, (symbol, end, start))


def dedupe_plans(plans: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[Any] = set()
    output = []
    for plan in sorted(plans, key=lambda row: row["created_at"]):
        key = plan.get("content_hash") or plan.get("plan_key")
        if key in seen:
            continue
        seen.add(key)
        output.append(plan)
    return output


def active_plan_index(plans: list[dict[str, Any]], at: datetime) -> int | None:
    """Latest plan already written before the order and still valid at it."""
    chosen = None
    for index, plan in enumerate(plans):
        if plan["created_at"] <= at and (plan.get("valid_until") is None or plan["valid_until"] >= at):
            if chosen is None or plan["created_at"] >= plans[chosen]["created_at"]:
                chosen = index
    return chosen


# --------------------------------------------------------------------------- order book


def fetch_order_book(connection: Any, *, symbol: str, day: date) -> list[dict[str, Any]]:
    start, end = _day_bounds(day)
    return _fetch(connection, """
        SELECT observed_at,price,raw->'bids' AS bids,raw->'asks' AS asks,
               raw->>'outer_volume_lot' AS outer_volume_lot,raw->>'inner_volume_lot' AS inner_volume_lot,
               raw->>'cumulative_amount' AS cumulative_amount,raw->>'trade_time' AS trade_time,
               raw->'order_book_features'->>'qi1' AS qi1,raw->'order_book_features'->>'qi5' AS qi5
          FROM quant.intraday_quote_observations
         WHERE symbol=%s AND source_name IN ('longhuvip_order_book','tencent_order_book') AND observed_at >= %s AND observed_at < %s
         ORDER BY observed_at LIMIT 6000
    """, (symbol, start, end))


def compact_order_book(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    def levels(values: Any) -> list[list[float]]:
        output = []
        for level in values or []:
            price, size = _num((level or {}).get("price")), _num((level or {}).get("size"))
            if price and price > 0:
                output.append([price, size or 0.0])
        return output

    return [{
        "t": row["observed_at"], "p": _num(row.get("price")), "b": levels(row.get("bids")),
        "a": levels(row.get("asks")), "outer": _num(row.get("outer_volume_lot")),
        "inner": _num(row.get("inner_volume_lot")), "amount": _num(row.get("cumulative_amount")),
        "qi1": _num(row.get("qi1")), "qi5": _num(row.get("qi5")), "trade_time": row.get("trade_time"),
    } for row in rows]


def order_book_context(book: list[dict[str, Any]], at: datetime, window_seconds: int = 180) -> dict[str, Any]:
    """Latest book at/before the order plus a short active-volume window."""
    history = [row for row in book if row["t"] <= at]
    if not history:
        return {"status": "missing"}
    last = history[-1]
    prices = [row["p"] for row in history if row["p"]]
    window = [row for row in history if row["t"] >= at - timedelta(seconds=window_seconds)]
    net = None
    if len(window) >= 2 and None not in (window[0]["outer"], window[0]["inner"], last["outer"], last["inner"]):
        net = (last["outer"] - last["inner"]) - (window[0]["outer"] - window[0]["inner"])
    bid_depth = sum(size for _, size in last["b"])
    ask_depth = sum(size for _, size in last["a"])
    return {
        "status": "observed", "observed_at": last["t"], "age_seconds": round((at - last["t"]).total_seconds(), 1),
        "price": last["p"], "bids": last["b"], "asks": last["a"], "qi1": last["qi1"], "qi5": last["qi5"],
        "bid_depth_lot": bid_depth, "ask_depth_lot": ask_depth,
        "sampled_high_so_far": max(prices) if prices else None,
        "sampled_low_so_far": min(prices) if prices else None,
        "window_seconds": window_seconds, "window_net_active_lot": net,
        "window_price_change": (last["p"] - window[0]["p"]) if len(window) >= 2 and last["p"] and window[0]["p"] else None,
    }


def order_book_minutes(book: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Per-minute outer-minus-inner lots, differenced only across same-day snapshots."""
    last_by_minute: dict[str, dict[str, Any]] = {}
    for row in book:
        if row["outer"] is None or row["inner"] is None:
            continue
        last_by_minute[row["t"].astimezone(CN).strftime("%H:%M")] = row
    output = []
    previous = None
    for minute in sorted(last_by_minute):
        row = last_by_minute[minute]
        cumulative = row["outer"] - row["inner"]
        output.append({"minute": minute, "net_active_lot": None if previous is None else cumulative - previous,
                       "cumulative_net_active_lot": cumulative, "qi5": row["qi5"], "last_price": row["p"]})
        previous = cumulative
    return output


# --------------------------------------------------------------------------- boards and breadth


def fetch_board_snapshots(connection: Any, *, day: date) -> list[dict[str, Any]]:
    start, end = _day_bounds(day)
    return _fetch(connection, """
        SELECT observed_at,snapshot_minute,status,payload->>'unit' AS unit,payload->'items' AS items
          FROM quant.intraday_board_flow_snapshots
         WHERE observed_at >= %s AND observed_at < %s
         ORDER BY observed_at LIMIT 600
    """, (start, end))


def compact_board_snapshots(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Dictionary-encode labels so a full session of board snapshots stays small."""
    labels: list[list[str]] = []
    index: dict[tuple[str, str], int] = {}
    snapshots = []
    units = set()
    for row in rows:
        units.add(row.get("unit"))
        values = []
        for item in row.get("items") or []:
            label, taxonomy = item.get("label"), item.get("taxonomy_key")
            if not label or not taxonomy:
                continue
            key = (label, taxonomy)
            if key not in index:
                index[key] = len(labels)
                labels.append([label, taxonomy])
            values.append([index[key], _num(item.get("change_pct")), _num(item.get("net_inflow"))])
        snapshots.append({"t": row["observed_at"], "minute": row.get("snapshot_minute"),
                          "status": row.get("status"), "rows": values})
    return {"unit": "/".join(sorted(str(unit) for unit in units if unit)) or None,
            "labels": labels, "snapshots": snapshots}


def board_context(boards: dict[str, Any], at: datetime, pinned: list[str], top: int = 5) -> dict[str, Any]:
    history = [row for row in boards["snapshots"] if row["t"] <= at]
    if not history:
        return {"status": "missing"}
    snapshot = history[-1]
    labels = boards["labels"]
    output: dict[str, Any] = {"status": "observed", "observed_at": snapshot["t"],
                              "age_seconds": round((at - snapshot["t"]).total_seconds(), 1), "taxonomies": {}}
    for taxonomy in TAXONOMY_LABELS:
        rows = [row for row in snapshot["rows"] if labels[row[0]][1] == taxonomy and row[1] is not None]
        if not rows:
            continue
        ranked = sorted(rows, key=lambda row: row[1], reverse=True)
        output["taxonomies"][taxonomy] = {
            "count": len(rows), "up_ratio": round(sum(row[1] > 0 for row in rows) / len(rows), 3),
            "leaders": [[labels[row[0]][0], row[1], row[2]] for row in ranked[:top]],
            "laggards": [[labels[row[0]][0], row[1], row[2]] for row in ranked[-top:][::-1]],
        }
    pinned_rows = []
    for name in pinned:
        for row in snapshot["rows"]:
            label, taxonomy = labels[row[0]]
            if label == name:
                peers = sorted((r[1] for r in snapshot["rows"] if labels[r[0]][1] == taxonomy and r[1] is not None),
                               reverse=True)
                rank = peers.index(row[1]) + 1 if row[1] is not None else None
                pinned_rows.append({"label": label, "taxonomy_key": taxonomy, "change_pct": row[1],
                                    "net_inflow": row[2], "rank": rank, "count": len(peers)})
    output["pinned"] = pinned_rows
    return output


def fetch_market_breadth(connection: Any, *, day: date) -> list[dict[str, Any]]:
    return _fetch(connection, """
        SELECT source_snapshot_minute,observed_at,created_at,status,market_state,concept_count,
               concept_positive_ratio,concept_mean_change_pct,concept_median_flow,
               five_minute_positive_ratio_delta,session_positive_ratio_delta
          FROM quant.market_flow_feature_snapshots
         WHERE exchange_date=%s AND cadence='minute'
         ORDER BY source_snapshot_minute,created_at LIMIT 2000
    """, (day,))


def dedupe_breadth(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Keep the first computation per source minute; later recomputations are not 'as seen'."""
    output: dict[datetime, dict[str, Any]] = {}
    for row in rows:
        minute = row["source_snapshot_minute"]
        if minute in output and output[minute]["available_at"] <= row["created_at"]:
            continue
        output[minute] = {
            "minute": minute, "available_at": max(row["observed_at"], row["created_at"]),
            "status": row["status"], "market_state": row["market_state"],
            "concept_count": row["concept_count"],
            "concept_positive_ratio": _num(row["concept_positive_ratio"]),
            "concept_mean_change_pct": _num(row["concept_mean_change_pct"]),
            "concept_median_flow": _num(row["concept_median_flow"]),
            "five_minute_positive_ratio_delta": _num(row["five_minute_positive_ratio_delta"]),
            "session_positive_ratio_delta": _num(row["session_positive_ratio_delta"]),
        }
    return [output[key] for key in sorted(output)]


def latest_available(rows: list[dict[str, Any]], at: datetime, key: str = "available_at") -> dict[str, Any] | None:
    history = [row for row in rows if row[key] <= at]
    return history[-1] if history else None


def fetch_signals(connection: Any, *, symbol: str, day: date) -> list[dict[str, Any]]:
    start, end = _day_bounds(day)
    return _fetch(connection, """
        SELECT e.observed_at,e.signal_type,e.severity,e.state,e.stage,e.score,
               d.sent_at,d.status AS delivery_status,d.channel
          FROM quant.intraday_signal_events e
          LEFT JOIN quant.intraday_alert_deliveries d ON d.signal_event_id=e.signal_event_id
         WHERE e.symbol=%s AND e.observed_at >= %s AND e.observed_at < %s
         ORDER BY e.observed_at LIMIT 200
    """, (symbol, start, end))


# --------------------------------------------------------------------------- benchmarks


def benchmark_bars_from_session(session: dict[str, Any], *, symbol: str, day: date,
                                fetched_at: datetime) -> tuple[list[dict[str, Any]], str]:
    """Accept a provider tape only when it declares the requested trading day."""
    if session.get("session_date") != day.isoformat():
        return [], f"provider_session_{session.get('session_date') or 'unknown'}"
    bars = []
    for row in session.get("rows") or []:
        clock = str(row.get("time") or "")
        price = _num(row.get("close"))
        # Rows after 15:00 are post-close fixed-price trading, not the continuous session.
        if len(clock) != 4 or not clock.isdigit() or clock > "1500" or price is None:
            continue
        bars.append({
            "bar_time": datetime.combine(day, time(int(clock[:2]), int(clock[2:])), tzinfo=CN),
            "open": price, "high": price, "low": price, "close": price,
            "volume": _num(row.get("volume_lot")), "amount": _num(row.get("amount")),
            "source_name": "longhuvip_minute_review_fetch", "available_at": fetched_at,
        })
    return bars, "review_time_fetch" if bars else "provider_empty"


def reference_close(daily: list[dict[str, Any]], day: date) -> float | None:
    """Prior close for percent lines; board change_pct uses the same basis."""
    for row in daily:
        if row["trading_date"] == day and row.get("pre_close") is not None:
            return _num(row["pre_close"])
    prior = [row for row in daily if row["trading_date"] < day and row.get("close") is not None]
    return _num(prior[-1]["close"]) if prior else None


def previous_trading_day(daily: list[dict[str, Any]], day: date) -> date | None:
    prior = [row["trading_date"] for row in daily if row["trading_date"] < day]
    return max(prior) if prior else None
