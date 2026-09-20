"""HTTP contract and projections of the read-only discipline chart / history / evaluation / reconciliation routes.

The plan is the real 600613.SH plan stored on 2026-09-18 (hard stop 7.63 =
8.41 - 0.9 x ATR14, low20 7.92) and the bars are the real settled
``canonical_bars_daily`` rows exported read-only; the async repository is a
fake, exactly as in ``test_trade_discipline_router.py``.  The key assertion is
the price basis: the chart's plan-day close, MA5/MA10 and ATR14 must equal the
numbers frozen in the plan, or the lines would not sit on the candles.
"""

from __future__ import annotations

import copy
import json
from datetime import date
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.routers.trade_discipline import TradeDisciplineDependencies, build_trade_discipline_router
from app.trade_discipline import chart

FIXTURES = Path(__file__).parent / "fixtures"
PLAN = json.loads((FIXTURES / "discipline_plan_600613_2026-09-18.json").read_text(encoding="utf-8"))
BARS = json.loads((FIXTURES / "discipline_real_bars_2026-09-18.json").read_text(encoding="utf-8"))["bars"]
PLAN_ID = PLAN["plan_id"]
OPEN_DAYS = [date(2026, 9, day) for day in (21, 22, 23, 24, 28, 29, 30)]
TODAY = date(2026, 9, 19)


def make_client(*, plan=PLAN, before=None, after=None, history=None, evaluations=None, reconciliation=None,
                stored=None, live=None, calls=None):
    calls = calls if calls is not None else []

    async def latest_plans(db, account_key, *, statuses, limit):
        return [plan]

    async def read_plan(db, plan_id):
        return plan if plan is not None and plan_id == plan["plan_id"] else None

    async def latest_evaluation(db, plan_id, *, basis):
        return None

    async def plan_history(db, account_key, symbol, *, limit):
        calls.append(("history", account_key, symbol, limit))
        return history if history is not None else [plan]

    async def plan_evaluations(db, plan_id):
        return evaluations or []

    async def reconciliations(db, account_key, *, symbol, start, end):
        calls.append(("reconciliations", account_key, symbol, start, end))
        return reconciliation or {"records": [], "trades": []}

    async def daily_bars(db, symbol, trading_date, last_day):
        calls.append(("bars", symbol, trading_date, last_day))
        rows = copy.deepcopy(BARS[symbol]) if before is None else before
        return {"before": list(reversed(rows)), "after": after or []}

    async def open_sessions(db, start, end):
        calls.append(("calendar", start, end))
        return [day for day in OPEN_DAYS if start <= day <= end]

    async def stored_minutes(db, symbol, day):
        calls.append(("minutes", symbol, day))
        return stored or {"rows": [], "source": None, "other_source_rows": 0}

    app = FastAPI()
    app.include_router(build_trade_discipline_router(TradeDisciplineDependencies(
        async_database="async-db", latest_plans=latest_plans, read_plan=read_plan,
        latest_evaluation=latest_evaluation, plan_history=plan_history, plan_evaluations=plan_evaluations,
        reconciliations=reconciliations, daily_bars=daily_bars, open_sessions=open_sessions,
        stored_minutes=stored_minutes, live_minutes=live, today=lambda: TODAY)))
    return TestClient(app)


# ---------------------------------------------------------------- daily chart
def test_the_daily_chart_is_in_the_generators_own_price_basis():
    calls = []
    response = make_client(calls=calls).get(f"/api/v1/discipline/plans/{PLAN_ID}/chart")
    assert response.status_code == 200
    payload = response.json()
    assert payload["price_basis"]["kind"] == "raw_unadjusted"
    assert payload["price_basis"]["table"] == "quant.canonical_bars_daily"
    # 600613.SH resumed on 2026-07-01 after a suspension with an adjusted pre-close: a real corporate
    # action inside the generator's window.  It is reported, the raw prices are kept (as the generator
    # used them) and, being before the plan date, it does not break the lines.
    assert payload["price_basis"]["corporate_actions"] == [
        {"date": "2026-07-01", "pre_close": 4.44, "previous_close": 6.28}]
    assert payload["price_basis"]["lines_comparable"] is True
    assert payload["price_basis"]["warning"] is None
    assert payload["bars"][0]["date"] == "2026-04-13" and payload["bars"][0]["close"] == 6.06
    # The generator's window: the last 60 canonical rows through the plan day.  (The plan recorded 58
    # usable rows; two older rows were re-canonicalized on 2026-09-19 - the plan-day values below are
    # what the lines depend on, and they are identical.)
    assert len(payload["bars"]) == 60
    plan_bar = payload["bars"][-1]
    assert plan_bar["date"] == "2026-09-18"
    metrics = PLAN["metrics"]
    assert plan_bar["close"] == metrics["close"] == 8.41
    assert abs(plan_bar["ma5"] - metrics["ma5"]) < 1e-4
    assert abs(plan_bar["ma10"] - metrics["ma10"]) < 1e-4
    assert abs(plan_bar["ma20"] - metrics["ma20"]) < 1e-4
    assert abs(plan_bar["atr14"] - metrics["atr14"]) < 1e-4
    assert payload["plan_bar_matches_metrics"] is True
    # the bar read stops at today, the calendar reaches valid_until
    assert ("bars", "600613.SH", date(2026, 9, 18), TODAY) in calls
    assert payload["future_sessions"] == ["2026-09-21", "2026-09-22", "2026-09-23", "2026-09-24", "2026-09-28"]
    assert payload["valid_until"] == "2026-09-28"
    assert payload["live_orders"] is False


def test_the_mid_autumn_closure_is_a_labelled_slot_and_weekends_are_not():
    payload = make_client().get(f"/api/v1/discipline/plans/{PLAN_ID}/chart").json()
    closures = payload["closures"]
    assert [item["dates"] for item in closures] == [["2026-09-25", "2026-09-26", "2026-09-27"]]
    assert closures[0]["label"] == "休市"
    assert closures[0]["closed_days"] == 3


def test_structure_points_are_located_on_their_bars():
    payload = make_client().get(f"/api/v1/discipline/plans/{PLAN_ID}/chart").json()
    points = {point["key"]: point for point in payload["structure_points"]}
    assert points["low20"]["price"] == 7.92
    assert points["low20"]["date"] == "2026-09-14"          # the real 20-day low
    assert points["low20"]["label"] == "low20 7.92"
    assert points["low20"]["role"] == "hard_stop_structure"


def test_the_hard_stop_terms_show_which_term_bound():
    terms = make_client().get(f"/api/v1/discipline/plans/{PLAN_ID}/chart").json()["hard_stop_terms"]
    assert terms["price"] == 7.63
    assert terms["binding_term"] == "atr"
    by_term = {item["term"]: item["value"] for item in terms["terms"]}
    assert by_term["structure"] == 7.92
    assert abs(by_term["atr"] - (8.41 - 0.9 * PLAN["metrics"]["atr14"])) < 1e-4
    assert min(by_term.values()) == by_term["atr"]
    assert "reference_price - atr_target_multiple * atr14" in terms["formula"]


def test_a_corporate_action_is_reported_never_silently_adjusted():
    rows = copy.deepcopy(BARS["600613.SH"])
    after = [{"trading_date": "2026-09-21", "open": 6.5, "high": 6.7, "low": 6.4, "close": 6.6,
              "pre_close": 6.47, "volume": 1.0e6, "amount": 6.6e6}]
    payload = make_client(before=rows, after=after).get(f"/api/v1/discipline/plans/{PLAN_ID}/chart").json()
    assert payload["price_basis"]["corporate_actions"][-1] == {
        "date": "2026-09-21", "pre_close": 6.47, "previous_close": 8.41}
    assert payload["price_basis"]["lines_comparable"] is False
    assert "除权" in payload["price_basis"]["warning"]
    assert payload["bars"][-1]["close"] == 6.6                      # raw, not rescaled


def test_an_unknown_plan_is_a_404_and_bad_parameters_are_422():
    client = make_client()
    unknown = "00000000-0000-0000-0000-000000000000"
    assert client.get(f"/api/v1/discipline/plans/{unknown}/chart").status_code == 404
    assert client.get(f"/api/v1/discipline/plans/{unknown}/evaluations").status_code == 404
    assert client.get(f"/api/v1/discipline/plans/{PLAN_ID}/chart?basis=weekly").status_code == 422
    assert client.get(f"/api/v1/discipline/plans/{PLAN_ID}/chart?basis=minute&date=18-09-2026").status_code == 422


# ---------------------------------------------------------------- minute chart
def test_stored_longhu_minutes_come_back_with_vwap():
    stored = {"source": "longhu_intraday_minutes", "other_source_rows": 0, "rows": [
        {"minute_bucket": "09:30", "open": 8.1, "high": 8.12, "low": 8.1, "close": 8.12, "volume": 2000.0,
         "amount": 1624000.0, "raw": {"vwap": 8.12, "volume_lot": 2000.0, "is_complete": True}},
        {"minute_bucket": "09:31", "open": 8.12, "high": 8.2, "low": 8.11, "close": 8.18, "volume": 1000.0,
         "amount": 818000.0, "raw": {"volume_lot": 1000.0, "is_complete": True}},
    ]}
    calls = []
    payload = make_client(stored=stored, calls=calls).get(
        f"/api/v1/discipline/plans/{PLAN_ID}/chart?basis=minute&date=2026-09-18").json()
    assert ("minutes", "600613.SH", date(2026, 9, 18)) in calls
    assert payload["basis"] == "minute"
    assert payload["count"] == 2 and payload["reason"] is None
    assert payload["bar_type"] == "ohlc"
    assert payload["rows"][0]["vwap"] == 8.12
    # no vendor VWAP on the second minute: cumulative amount / (lots x 100)
    assert abs(payload["rows"][1]["vwap"] - (1624000.0 + 818000.0) / 300000.0) < 1e-4


def test_no_minutes_is_an_empty_list_with_the_reason():
    payload = make_client(stored={"rows": [], "source": None, "other_source_rows": 36}).get(
        f"/api/v1/discipline/plans/{PLAN_ID}/chart?basis=minute&date=2026-09-16").json()
    assert payload["rows"] == []
    assert "没有已入库的 longhu 分钟线" in payload["reason"]
    assert "36 行其他来源" in payload["reason"]


def test_the_live_longhu_tape_is_used_only_for_its_own_session():
    async def live(symbol):
        # the real Longhu trend shape: one close per minute, no open/high/low
        return {"session_date": "2026-09-18", "rows": [
            {"time": "0930", "close": 8.12, "vwap": 8.12, "volume_lot": 100.0, "amount": 81200.0}]}
    client = make_client(live=live)
    same = client.get(f"/api/v1/discipline/plans/{PLAN_ID}/chart?basis=minute&date=2026-09-18").json()
    assert same["source"] == "longhu_intraday_minutes:live" and same["count"] == 1
    assert same["bar_type"] == "close_only"
    other = client.get(f"/api/v1/discipline/plans/{PLAN_ID}/chart?basis=minute&date=2026-09-17").json()
    assert other["rows"] == [] and "只提供最新交易日 2026-09-18" in other["reason"]


def test_a_failing_live_tape_degrades_to_a_reason():
    async def live(symbol):
        raise TimeoutError("vendor down")
    payload = make_client(live=live).get(
        f"/api/v1/discipline/plans/{PLAN_ID}/chart?basis=minute&date=2026-09-18").json()
    assert payload["rows"] == [] and "TimeoutError" in payload["reason"]


# ---------------------------------------------------------------- history / evaluations / reconciliations
def test_history_returns_every_plan_and_the_stop_ladder():
    older = {**copy.deepcopy(PLAN), "plan_id": "11111111-1111-1111-1111-111111111111", "status": "superseded",
             "as_of_at": "2026-09-17T21:00:00+08:00", "trading_date": "2026-09-17",
             "valid_until": "2026-09-24T15:00:00+08:00", "sizing": {**PLAN["sizing"], "hard_stop": "7.80"}}
    newer = {**copy.deepcopy(PLAN), "lowered_reason": "急跌低点下移，放宽到 0.9×ATR"}
    calls = []
    payload = make_client(history=[older, newer], calls=calls).get(
        "/api/v1/discipline/plans/history?account_key=citics-primary&symbol=600613.SH").json()
    assert calls[0] == ("history", "citics-primary", "600613.SH", 200)
    assert payload["count"] == 2
    ladder = payload["ladder"]
    assert [step["hard_stop"] for step in ladder] == [7.8, 7.63]
    assert ladder[0]["until"] == "2026-09-18"                    # held until the next plan's trading date
    assert ladder[1]["lowered"] is True and ladder[1]["previous_hard_stop"] == 7.8
    assert ladder[1]["lowered_reason"].startswith("急跌低点下移")


def test_history_validates_its_query():
    client = make_client()
    assert client.get("/api/v1/discipline/plans/history?account_key=citics-primary").status_code == 422
    assert client.get("/api/v1/discipline/plans/history?account_key=citics-primary&symbol=600613").status_code == 422


def test_the_evaluation_history_is_reshaped_per_line_with_transitions():
    lines = PLAN["lines"]

    def states(hard_state, triggered_at=None, price=None):
        return [{"kind": line["kind"], "label": line["label"],
                 "state": hard_state if line["kind"] == "hard_stop" and line["confirm"]["basis"] == "daily"
                 else "armed", "triggered_at": triggered_at if line["kind"] == "hard_stop" else None,
                 "trigger_price": price if line["kind"] == "hard_stop" else None} for line in lines]
    rows = [
        {"evaluation_id": "e1", "plan_id": PLAN_ID, "as_of_at": "2026-09-21T15:30:00+08:00",
         "trading_date": "2026-09-21", "basis": "daily", "line_states": states("armed"), "plan_state": "active"},
        {"evaluation_id": "e2", "plan_id": PLAN_ID, "as_of_at": "2026-09-22T15:30:00+08:00",
         "trading_date": "2026-09-22", "basis": "daily",
         "line_states": states("triggered", "2026-09-22T15:00:00+08:00", "7.55"), "plan_state": "exit_signalled"},
    ]
    payload = make_client(evaluations=rows).get(f"/api/v1/discipline/plans/{PLAN_ID}/evaluations").json()
    assert payload["count"] == 2
    hard_index = next(index for index, line in enumerate(lines) if line["kind"] == "hard_stop")
    assert [state["state"] for state in payload["lines"][hard_index]["states"]] == ["armed", "triggered"]
    assert payload["transitions"] == [{
        "line_index": hard_index, "kind": "hard_stop", "label": lines[hard_index]["label"], "from": "armed",
        "to": "triggered", "trading_date": "2026-09-22", "as_of_at": "2026-09-22T15:30:00+08:00",
        "basis": "daily", "state": "triggered", "triggered_at": "2026-09-22T15:00:00+08:00",
        "trigger_price": 7.55, "date": "2026-09-22"}]
    assert [change["to"] for change in payload["plan_state_changes"]] == ["active", "exit_signalled"]


def test_no_evaluations_is_an_empty_history_not_an_error():
    payload = make_client().get(f"/api/v1/discipline/plans/{PLAN_ID}/evaluations").json()
    assert payload["count"] == 0 and payload["transitions"] == [] and payload["evaluations"] == []


def test_reconciliations_default_to_thirty_days_and_return_empty_lists():
    calls = []
    response = make_client(calls=calls).get("/api/v1/discipline/reconciliations?account_key=citics-primary")
    assert response.status_code == 200
    payload = response.json()
    assert payload["items"] == [] and payload["trades"] == [] and payload["verdict_counts"] == {}
    assert calls[0] == ("reconciliations", "citics-primary", None, date(2026, 8, 20), TODAY)


def test_reconciliations_attach_verdicts_to_their_fills():
    raw = {"records": [{"compliance_id": "c1", "plan_id": PLAN_ID, "trade_record_id": "t-1", "verdict": "followed",
                        "line_kind": "exposure", "notes": "按时减仓", "trade_date": date(2026, 9, 21)}],
           "trades": [{"record_id": "t-1", "trade_date": date(2026, 9, 21), "trade_time": "09:35:00",
                       "symbol": "600613.SH", "side": "sell", "quantity": 4600, "price": 8.45},
                      {"record_id": "t-2", "trade_date": date(2026, 9, 21), "trade_time": "10:00:00",
                       "symbol": "600613.SH", "side": "buy", "quantity": 100, "price": 8.30}]}
    payload = make_client(reconciliation=raw).get(
        "/api/v1/discipline/reconciliations?account_key=citics-primary&symbol=600613.SH"
        "&from=2026-09-18&to=2026-09-30").json()
    assert payload["verdict_counts"] == {"followed": 1}
    assert payload["trades"][0]["verdicts"][0]["verdict"] == "followed"
    assert payload["trades"][1]["verdicts"] == []
    assert payload["trades"][0]["trade_date"] == "2026-09-21"


def test_reconciliations_validate_the_window():
    client = make_client()
    base = "/api/v1/discipline/reconciliations?account_key=citics-primary"
    assert client.get(f"{base}&from=2026-09-30&to=2026-09-01").status_code == 422
    assert client.get(f"{base}&from=2025-01-01&to=2026-09-01").status_code == 422
    assert client.get(f"{base}&symbol=bad").status_code == 422


def test_every_new_route_is_get_only():
    app = make_client().app
    routes = {route.path: route.methods for route in app.routes
              if getattr(route, "path", "").startswith("/api/v1/discipline")}
    assert all(methods == {"GET"} for methods in routes.values())
    assert set(routes) == {
        "/api/v1/discipline/alerts/status",
        "/api/v1/discipline/plans/latest", "/api/v1/discipline/plans/history",
        "/api/v1/discipline/plans/{plan_id}", "/api/v1/discipline/plans/{plan_id}/chart",
        "/api/v1/discipline/plans/{plan_id}/evaluations", "/api/v1/discipline/evaluations/latest",
        "/api/v1/discipline/reconciliations",
    }


def test_unconfigured_projections_are_a_503_not_a_crash():
    async def read_plan(db, plan_id):
        return PLAN

    async def unused(*args, **kwargs):
        return []
    app = FastAPI()
    app.include_router(build_trade_discipline_router(TradeDisciplineDependencies(
        async_database="db", latest_plans=unused, read_plan=read_plan, latest_evaluation=unused)))
    client = TestClient(app)
    assert client.get(f"/api/v1/discipline/plans/{PLAN_ID}/chart").status_code == 503
    assert client.get("/api/v1/discipline/reconciliations?account_key=a").status_code == 503


# ---------------------------------------------------------------- pure helpers
def test_calendar_axis_separates_closures_suspensions_and_weekends():
    bars = [date(2026, 4, 13), date(2026, 4, 14), date(2026, 7, 1), date(2026, 9, 17), date(2026, 9, 18)]
    open_days = [date(2026, 4, 15), date(2026, 6, 30), date(2026, 9, 21), date(2026, 9, 22), date(2026, 9, 23),
                 date(2026, 9, 24), date(2026, 9, 28), date(2026, 9, 29), date(2026, 9, 30), date(2026, 10, 8)]
    axis = chart.calendar_axis(bars, open_days, plan_day=date(2026, 9, 18), last=date(2026, 10, 8))
    assert axis["future"] == ["2026-09-21", "2026-09-22", "2026-09-23", "2026-09-24", "2026-09-28", "2026-09-29",
                              "2026-09-30", "2026-10-08"]
    # the Mid-Autumn run and National Day are closures; the 09-18 weekend is not
    assert [(item["last_trading_date"], item["closed_days"]) for item in axis["closures"]] == [
        ("2026-09-24", 3), ("2026-09-30", 7)]
    # a gap with open sessions in it is a suspension, never shaded as 休市
    assert axis["suspensions"] == [{"from": "2026-04-14", "to": "2026-07-01", "missing_sessions": 2,
                                    "label": "停牌或无日线"}]
