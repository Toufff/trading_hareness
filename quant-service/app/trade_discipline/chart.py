"""Pure projections behind the read-only discipline chart, history and evaluation routes.

Nothing here reads a database or a provider.  The async read repository fetches
rows; these functions turn them into what the discipline card UI draws:

* ``daily_chart``: the plan's own price basis.  The generator derives every
  line from raw, *unadjusted* ``quant.canonical_bars_daily`` rows (the last 60
  settled sessions through the plan's trading date, suspended rows dropped,
  then ``stage.normalize_bars``), so the chart returns bars in exactly that
  basis, with the same MA/ATR definitions, and says so in ``price_basis``.  A
  corporate action inside the window is *reported*, never silently adjusted,
  because adjusting only the chart would move the candles off the lines.
* ``minute_chart``: one session's 1-minute bars with VWAP, or an empty list and
  the reason.
* ``evaluation_timeline`` / ``stop_ladder``: the stored evaluation rows and the
  plan history reshaped per line / per plan.

Every price in the output was either read from a stored row or recomputed with
the plan's own recorded formula inputs; the UI derives no price of its own.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from ..stock_workbench_indicators import _atr
from .stage import ATR_PERIOD, normalize_bars
from .templates import ATR_TARGET_MULTIPLE, STOP_PCT_TARGET, WEEKEND_CLOSURE_DAYS

SHANGHAI = ZoneInfo("Asia/Shanghai")
PLAN_BAR_COUNT = 60
PRICE_BASIS = {
    "kind": "raw_unadjusted",
    "table": "quant.canonical_bars_daily",
    "method": "same_as_generator",
    "note": ("与纪律卡生成器同一口径：quant.canonical_bars_daily 未复权原始价，计划交易日及之前取最近 60 根已结算日线"
             "（剔除停牌），均线为收盘简单均值，ATR14 为 14 日真实波幅均值。生成器不做复权，图表也不做。"),
}
CORPORATE_ACTION_TOLERANCE = 0.011
LONGHU_MINUTE_SOURCE = "longhu_intraday_minutes"
STRUCTURE_LABEL: dict[str, str] = {
    "low20": "low20", "today_low": "当日低点", "prev_low": "昨日低点", "recent_low": "近5日收盘低",
    "prior_high": "前5日收盘高（平台）", "low10_close": "10日最低收盘", "ma10": "MA10",
    "lane_reference": "lane 结构参考", "last_close": "计划日收盘",
}


# --------------------------------------------------------------------------
# small helpers
# --------------------------------------------------------------------------
def _number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if result == result and abs(result) != float("inf") else None


def _day(value: Any) -> date | None:
    if isinstance(value, datetime):
        return value.astimezone(SHANGHAI).date() if value.tzinfo else value.date()
    if isinstance(value, date):
        return value
    text = str(value or "").strip()
    if len(text) == 8 and text.isdigit():
        text = f"{text[:4]}-{text[4:6]}-{text[6:]}"
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return None


as_day = _day


def _iso(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return str(value)


def _round(value: float | None, digits: int = 4) -> float | None:
    return None if value is None else round(value, digits)


def _rolling_mean(values: list[float], window: int) -> list[float | None]:
    out: list[float | None] = []
    for index in range(len(values)):
        if index + 1 < window:
            out.append(None)
        else:
            chunk = values[index + 1 - window:index + 1]
            out.append(sum(chunk) / window)
    return out


def plan_dates(plan: dict[str, Any]) -> tuple[date, date]:
    """``(trading_date, valid_until date)`` of a stored plan row."""
    trading_day = _day(plan.get("trading_date"))
    valid_until = _day(plan.get("valid_until"))
    if trading_day is None:
        raise ValueError("plan row has no trading_date")
    return trading_day, valid_until or trading_day


# --------------------------------------------------------------------------
# daily chart
# --------------------------------------------------------------------------
def plan_window_rows(before: list[dict[str, Any]], after: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The generator's own 60-session selection (suspended rows dropped *after* the limit), plus later rows."""
    ordered_before = sorted(before, key=lambda row: str(row.get("trading_date")))[-PLAN_BAR_COUNT:]
    rows = [row for row in ordered_before if not row.get("is_suspended")]
    rows += [row for row in sorted(after, key=lambda row: str(row.get("trading_date")))
             if not row.get("is_suspended")]
    return rows


def corporate_actions(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Sessions whose ``pre_close`` is not the previous close: an ex-rights/dividend adjustment day."""
    found: list[dict[str, Any]] = []
    ordered = sorted(rows, key=lambda row: str(row.get("trading_date")))
    for previous, row in zip(ordered, ordered[1:]):
        pre_close, prior = _number(row.get("pre_close")), _number(previous.get("close"))
        if pre_close is None or prior is None:
            continue
        if abs(pre_close - prior) > CORPORATE_ACTION_TOLERANCE:
            found.append({"date": _iso(_day(row.get("trading_date"))), "pre_close": pre_close,
                          "previous_close": prior})
    return found


def calendar_axis(bar_dates: list[date], open_days: list[date], *, plan_day: date, last: date) -> dict[str, Any]:
    """Session slots for the chart: every stored bar, then the open sessions still ahead up to ``last``.

    * ``sessions``: bar dates (a calendar gap never hides a stored bar) followed
      by the exchange's open sessions after the last bar, up to ``valid_until``.
    * ``closures``: from the plan's trading date on, every run of closed days
      between two sessions that is not an ordinary Friday-to-Monday weekend
      (the Mid-Autumn 09-25..09-27), with its dates, labelled 休市.
    * ``suspensions``: gaps between two stored bars that contain open exchange
      sessions - the stock did not trade (or no bar is stored); never a closure.
    """
    bars = sorted({day for day in bar_dates if day is not None})
    open_set = {day for day in open_days if day is not None}
    last_bar = bars[-1] if bars else None
    future = sorted(day for day in open_set if (last_bar is None or day > last_bar) and day <= last)
    sessions = [*bars, *future]
    closures: list[dict[str, Any]] = []
    suspensions: list[dict[str, Any]] = []
    for previous, following in zip(sessions, sessions[1:]):
        closed = (following - previous).days - 1
        if closed <= 0:
            continue
        between = [previous + timedelta(days=offset) for offset in range(1, closed + 1)]
        missing = [day for day in between if day in open_set]
        if missing:
            suspensions.append({"from": previous.isoformat(), "to": following.isoformat(),
                                "missing_sessions": len(missing), "label": "停牌或无日线"})
            continue
        if following <= plan_day:
            continue
        if closed == WEEKEND_CLOSURE_DAYS and previous.weekday() == 4:
            continue
        if all(day.weekday() >= 5 for day in between):
            continue
        closures.append({
            "last_trading_date": previous.isoformat(), "resume_date": following.isoformat(),
            "closed_days": closed, "dates": [day.isoformat() for day in between], "label": "休市",
        })
    return {"sessions": [day.isoformat() for day in sessions], "future": [day.isoformat() for day in future],
            "closures": closures, "suspensions": suspensions}


def bar_series(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Normalized bars with change %, MA5/10/20 and ATR14, exactly as ``stage.daily_metrics`` defines them."""
    normalized = normalize_bars([{**row, "trading_date": _iso(_day(row.get("trading_date")))} for row in rows])
    if not normalized:
        return []
    closes = [row["close"] for row in normalized]
    ma5, ma10, ma20 = _rolling_mean(closes, 5), _rolling_mean(closes, 10), _rolling_mean(closes, 20)
    atr = _atr(normalized, ATR_PERIOD)
    out: list[dict[str, Any]] = []
    for index, row in enumerate(normalized):
        previous = closes[index - 1] if index else None
        out.append({
            "date": row["trading_date"], "open": row["open"], "high": row["high"], "low": row["low"],
            "close": row["close"], "volume": row["volume"], "amount": row["amount"],
            "change_pct": None if previous in (None, 0) else _round((row["close"] / previous - 1) * 100, 3),
            "ma5": _round(ma5[index]), "ma10": _round(ma10[index]), "ma20": _round(ma20[index]),
            "atr14": _round(atr[index]),
            # display band only (close +/- 1 x ATR14); never a discipline line
            "atr_upper": None if atr[index] is None else _round(row["close"] + atr[index]),
            "atr_lower": None if atr[index] is None else _round(row["close"] - atr[index]),
        })
    return out


def _bar_on(bars: list[dict[str, Any]], day: str) -> dict[str, Any] | None:
    return next((bar for bar in bars if bar["date"] == day), None)


def _last_date_matching(bars: list[dict[str, Any]], key: str, value: float) -> str | None:
    for bar in reversed(bars):
        if abs(float(bar[key]) - value) < 0.005:
            return bar["date"]
    return None


def structure_points(plan: dict[str, Any], bars: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The structure points the plan's hard stop / entry were derived from, located on a bar where possible."""
    trading_day = str(plan.get("trading_date"))[:10]
    history = [bar for bar in bars if bar["date"] <= trading_day]
    last20, last10, last6 = history[-20:], history[-10:], history[-6:-1]
    last5 = history[-5:]
    metrics = plan.get("metrics") or {}
    hard = next((line for line in plan.get("lines") or []
                 if isinstance(line, dict) and line.get("kind") == "hard_stop"), None)
    inputs = ((hard or {}).get("derivation") or {}).get("inputs") or {}
    points: list[dict[str, Any]] = []

    def add(key: str, price: Any, day: str | None, role: str, source: str) -> None:
        value = _number(price)
        if value is None or any(point["key"] == key for point in points):
            return
        points.append({"key": key, "label": f"{STRUCTURE_LABEL.get(key, key)} {value:.2f}",
                       "price": round(value, 4), "date": day, "role": role, "source": source})

    locate = {
        "low20": lambda value: _last_date_matching(last20, "low", value),
        "today_low": lambda value: trading_day if _bar_on(history, trading_day) else None,
        "prev_low": lambda value: history[-2]["date"] if len(history) >= 2 else None,
        "recent_low": lambda value: _last_date_matching(last5, "close", value),
        "prior_high": lambda value: _last_date_matching(last6, "close", value),
        "low10_close": lambda value: _last_date_matching(last10, "close", value),
    }
    for key in ("low20", "today_low", "prev_low", "recent_low", "prior_high", "low10_close"):
        if key in inputs:
            add(key, inputs[key], locate[key](float(inputs[key])), "hard_stop_structure",
                f"hard_stop.derivation.inputs.{key}")
    if "low20" not in inputs and metrics.get("low20") is not None:
        add("low20", metrics["low20"], locate["low20"](float(metrics["low20"])), "context", "metrics.low20")
    entry = metrics.get("entry") if isinstance(metrics.get("entry"), dict) else None
    if entry:
        add("lane_reference", entry.get("lane_reference"), None, "entry", "metrics.entry.lane_reference")
        add("last_close", entry.get("last_close"), entry.get("last_close_date"), "entry",
            "metrics.entry.last_close")
    return points


def hard_stop_terms(plan: dict[str, Any]) -> dict[str, Any] | None:
    """The four ``min`` terms of the daily hard stop, recomputed from its recorded inputs."""
    hard = next((line for line in plan.get("lines") or [] if isinstance(line, dict)
                 and line.get("kind") == "hard_stop"
                 and ((line.get("confirm") or {}).get("basis") or "daily") == "daily"), None)
    if hard is None:
        return None
    derivation = hard.get("derivation") or {}
    inputs = derivation.get("inputs") or {}
    reference = _number(inputs.get("reference_price"))
    atr14 = _number(inputs.get("atr14"))
    buffer = _number(inputs.get("buffer_pct"))
    structure = _number(inputs.get("structure_value"))
    if reference is None or atr14 is None or buffer is None:
        return None
    multiple = _number(inputs.get("atr_target_multiple")) or ATR_TARGET_MULTIPLE
    pct = _number(inputs.get("stop_pct_target")) or STOP_PCT_TARGET
    terms = [
        {"term": "structure", "label": "结构点", "value": _round(structure)},
        {"term": "buffer", "label": f"波动缓冲 {buffer * 100:.2f}%", "value": _round(reference * (1 - buffer))},
        {"term": "atr", "label": f"{multiple}×ATR14", "value": _round(reference - multiple * atr14)},
        {"term": "pct", "label": f"{pct:.0%}", "value": _round(reference * (1 - pct))},
    ]
    return {"price": _number(hard.get("price")), "formula": derivation.get("formula"),
            "rule_id": derivation.get("rule_id"), "binding_term": inputs.get("binding_term"),
            "structure_source": inputs.get("structure_source"), "reference_price": reference,
            "atr14": atr14, "terms": terms}


def daily_chart(plan: dict[str, Any], *, before: list[dict[str, Any]], after: list[dict[str, Any]],
                open_days: list[date], today: date) -> dict[str, Any]:
    """The daily chart payload for one stored plan."""
    trading_day, valid_until = plan_dates(plan)
    rows = plan_window_rows(before, after)
    bars = bar_series(rows)
    actions = corporate_actions(rows)
    axis = calendar_axis([_day(bar["date"]) for bar in bars], open_days,
                         plan_day=trading_day, last=max(valid_until, trading_day))
    last_bar = bars[-1] if bars else None
    plan_bar = _bar_on(bars, trading_day.isoformat())
    metrics = plan.get("metrics") or {}
    after_plan = [item for item in actions if item["date"] and item["date"] > trading_day.isoformat()]
    return {
        "basis": "daily",
        "plan_id": _iso(plan.get("plan_id")), "symbol": plan.get("symbol"), "name": plan.get("name"),
        "plan_kind": plan.get("plan_kind"), "trading_date": trading_day.isoformat(),
        "valid_until": valid_until.isoformat(), "as_of": today.isoformat(),
        "price_basis": {**PRICE_BASIS, "corporate_actions": actions,
                        "lines_comparable": not after_plan,
                        "warning": (f"{after_plan[0]['date']} 发生除权/除息，此后 K 线与生成日未复权价格的纪律线不可直接比较"
                                    if after_plan else None)},
        "plan_bar_matches_metrics": (None if plan_bar is None or metrics.get("close") is None
                                     else abs(plan_bar["close"] - float(metrics["close"])) < 0.005),
        "bars": bars,
        "sessions": axis["sessions"],
        "future_sessions": axis["future"],
        "closures": axis["closures"],
        "suspensions": axis["suspensions"],
        "structure_points": structure_points(plan, bars),
        "hard_stop_terms": hard_stop_terms(plan),
        "latest": None if last_bar is None else {"date": last_bar["date"], "close": last_bar["close"],
                                                 "atr14": last_bar["atr14"]},
        "live_orders": False,
    }


# --------------------------------------------------------------------------
# minute chart
# --------------------------------------------------------------------------
def _minute_time(row: dict[str, Any]) -> str | None:
    bar_time = row.get("bar_time")
    if isinstance(bar_time, datetime):
        return bar_time.astimezone(SHANGHAI).strftime("%H:%M")
    raw = str(row.get("minute_bucket") or row.get("time") or row.get("trade_time") or "").strip()
    if len(raw) >= 16 and raw[10] in " T":
        raw = raw[11:16]
    digits = raw.replace(":", "")
    if len(digits) >= 4 and digits[:4].isdigit():
        return f"{digits[:2]}:{digits[2:4]}"
    return None


def minute_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """1-minute bars with the vendor VWAP when it is recorded, else the cumulative amount/volume VWAP.

    Longhu reports volume in lots (100 shares) and amount in yuan, so the
    cumulative fallback is ``Σamount / (Σvolume_lot × 100)``.
    """
    out: list[dict[str, Any]] = []
    cumulative_amount = cumulative_shares = 0.0
    for row in rows:
        raw = row.get("raw") if isinstance(row.get("raw"), dict) else {}
        time_text = _minute_time(row) or _minute_time(raw)
        values = {key: _number(row.get(key) if row.get(key) is not None else raw.get(key))
                  for key in ("open", "high", "low", "close")}
        if time_text is None or any(value is None for value in values.values()):
            continue
        volume_lot = _number(next((value for value in (raw.get("volume_lot"), row.get("volume_lot"), row.get("volume"))
                                   if value is not None), None))
        amount = _number(row.get("amount") if row.get("amount") is not None else raw.get("amount"))
        if volume_lot and amount:
            cumulative_amount += amount
            cumulative_shares += volume_lot * 100
        vendor_vwap = _number(raw.get("vwap") if raw else row.get("vwap"))
        vwap = vendor_vwap if vendor_vwap else (cumulative_amount / cumulative_shares if cumulative_shares else None)
        out.append({"time": time_text, **values, "volume": volume_lot, "amount": amount,
                    "vwap": _round(vwap), "is_complete": raw.get("is_complete", row.get("is_complete"))})
    out.sort(key=lambda item: item["time"])
    return out


def minute_chart(plan: dict[str, Any], *, day: date, rows: list[dict[str, Any]], source: str | None,
                 reason: str | None) -> dict[str, Any]:
    bars = minute_rows(rows)
    return {
        "basis": "minute", "plan_id": _iso(plan.get("plan_id")), "symbol": plan.get("symbol"),
        "date": day.isoformat(), "source": source if bars else None,
        "rows": bars, "count": len(bars),
        "reason": None if bars else (reason or "该交易日没有已入库的 longhu 分钟线"),
        "price_basis": {"kind": "raw_unadjusted", "note": "分钟线为当日原始成交价，与日线未复权口径一致"},
        "live_orders": False,
    }


# --------------------------------------------------------------------------
# evaluation timeline / plan history
# --------------------------------------------------------------------------
def evaluation_timeline(plan: dict[str, Any], evaluations: list[dict[str, Any]]) -> dict[str, Any]:
    """Every stored evaluation reshaped per line, with the state transitions a chart marks."""
    lines = [line for line in plan.get("lines") or [] if isinstance(line, dict)]
    ordered = sorted(evaluations, key=lambda row: (str(row.get("as_of_at")), str(row.get("created_at"))))
    per_line: list[dict[str, Any]] = [
        {"index": index, "kind": line.get("kind"), "label": line.get("label"),
         "basis": (line.get("confirm") or {}).get("basis") or "daily", "states": []}
        for index, line in enumerate(lines)]
    index_by_key = {(line.get("kind"), line.get("label")): index for index, line in enumerate(lines)}
    transitions: list[dict[str, Any]] = []
    plan_state_changes: list[dict[str, Any]] = []
    previous_plan_state: str | None = None
    last_state: dict[tuple[int, str], str] = {}
    for row in ordered:
        basis = row.get("basis") or "daily"
        day = _iso(_day(row.get("trading_date")))
        for position, state in enumerate(row.get("line_states") or []):
            if not isinstance(state, dict):
                continue
            index = index_by_key.get((state.get("kind"), state.get("label")))
            if index is None and position < len(lines) and lines[position].get("kind") == state.get("kind"):
                index = position
            if index is None:
                continue
            entry = {"trading_date": day, "as_of_at": _iso(row.get("as_of_at")), "basis": basis,
                     "state": state.get("state"), "triggered_at": _iso(state.get("triggered_at")),
                     "trigger_price": _number(state.get("trigger_price"))}
            per_line[index]["states"].append(entry)
            before = last_state.get((index, basis), "armed")
            if state.get("state") != before:
                transitions.append({"line_index": index, "kind": state.get("kind"), "label": state.get("label"),
                                    "from": before, "to": state.get("state"), **entry,
                                    "date": _iso(_day(state.get("triggered_at"))) or day})
            last_state[(index, basis)] = str(state.get("state"))
        plan_state = row.get("plan_state")
        if plan_state != previous_plan_state:
            plan_state_changes.append({"trading_date": day, "as_of_at": _iso(row.get("as_of_at")),
                                       "basis": basis, "from": previous_plan_state, "to": plan_state})
            previous_plan_state = plan_state
    return {"count": len(ordered), "evaluations": ordered, "lines": per_line,
            "transitions": transitions, "plan_state_changes": plan_state_changes}


def _hard_stop_of(plan: dict[str, Any]) -> float | None:
    sizing = plan.get("sizing") or {}
    if sizing.get("hard_stop") is not None:
        return _number(sizing.get("hard_stop"))
    prices = [_number(line.get("price")) for line in plan.get("lines") or []
              if isinstance(line, dict) and line.get("kind") == "hard_stop"]
    prices = [price for price in prices if price is not None]
    return min(prices) if prices else None


def stop_ladder(plans: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One step per plan, oldest first: each hard stop holds until the next plan's trading date."""
    ordered = sorted(plans, key=lambda row: (str(row.get("as_of_at")), str(row.get("plan_id"))))
    steps: list[dict[str, Any]] = []
    previous: float | None = None
    for index, plan in enumerate(ordered):
        stop = _hard_stop_of(plan)
        trading_day, valid_until = plan_dates(plan)
        following = ordered[index + 1] if index + 1 < len(ordered) else None
        end = min(valid_until, plan_dates(following)[0]) if following else valid_until
        lowered = previous is not None and stop is not None and stop < previous - 1e-9
        steps.append({"plan_id": _iso(plan.get("plan_id")), "plan_key": plan.get("plan_key"),
                      "status": plan.get("status"), "plan_kind": plan.get("plan_kind"),
                      "trading_date": trading_day.isoformat(), "until": end.isoformat(),
                      "hard_stop": stop, "previous_hard_stop": previous, "lowered": lowered,
                      "lowered_reason": plan.get("lowered_reason"),
                      "supersedes_plan_id": _iso(plan.get("supersedes_plan_id"))})
        if stop is not None:
            previous = stop
    return steps


__all__ = [
    "LONGHU_MINUTE_SOURCE", "PLAN_BAR_COUNT", "PRICE_BASIS", "as_day", "bar_series", "calendar_axis", "corporate_actions",
    "daily_chart", "evaluation_timeline", "hard_stop_terms", "minute_chart", "minute_rows", "plan_dates",
    "plan_window_rows", "stop_ladder", "structure_points",
]
