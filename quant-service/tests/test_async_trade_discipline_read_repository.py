"""The discipline read projections issue SELECTs only, with bounded, parameterised reads.

A fake async pool records every statement; what is asserted is the read-only
shape (no INSERT/UPDATE/DELETE, no broker/order table), the parameters the
routes depend on, and the empty-data behaviour.
"""

from __future__ import annotations

import asyncio
import re
from contextlib import asynccontextmanager
from datetime import date

from app import async_trade_discipline_read_repository as repository

PLAN_ID = "699144a6-00e4-42a2-bdf7-168e601bfef0"


class FakeResult:
    def __init__(self, rows):
        self.rows = rows

    async def fetchall(self):
        return self.rows

    async def fetchone(self):
        return self.rows[0] if self.rows else None


class FakeConnection:
    def __init__(self, responses):
        self.responses = list(responses)
        self.statements: list[tuple[str, tuple]] = []

    async def execute(self, sql, params=None):
        self.statements.append((" ".join(sql.split()), params))
        return FakeResult(self.responses.pop(0) if self.responses else [])


class FakeDatabase:
    def __init__(self, *responses):
        self.connection = FakeConnection(responses)

    @asynccontextmanager
    async def transaction(self):
        yield self.connection


def run(coroutine):
    return asyncio.run(coroutine)


def assert_read_only(database: FakeDatabase):
    for sql, _ in database.connection.statements:
        assert sql.upper().startswith("SELECT"), sql
        assert not re.search(r"\b(INSERT|UPDATE|DELETE|TRUNCATE|ALTER|DROP)\b", sql.upper()), sql


def test_plan_history_reads_every_status_oldest_first():
    database = FakeDatabase([{"plan_id": PLAN_ID}])
    rows = run(repository.plan_history(database, "citics-primary", "600613.SH", limit=999))
    assert rows == [{"plan_id": PLAN_ID}]
    sql, params = database.connection.statements[0]
    assert "FROM quant.discipline_plans" in sql and "status" not in sql.split("WHERE")[1]
    assert "ORDER BY as_of_at,created_at" in sql
    assert params == ("citics-primary", "600613.SH", repository.MAX_LIMIT)
    assert_read_only(database)


def test_plan_evaluations_rejects_a_malformed_id_without_a_query():
    database = FakeDatabase()
    assert run(repository.plan_evaluations(database, "not-a-uuid")) == []
    assert database.connection.statements == []
    run(repository.plan_evaluations(database, PLAN_ID))
    sql, params = database.connection.statements[0]
    assert "FROM quant.discipline_evaluations" in sql and params[0] == PLAN_ID
    assert_read_only(database)


def test_reconciliations_read_verdicts_and_fills_and_tolerate_empty_tables():
    database = FakeDatabase([], [])
    result = run(repository.reconciliations(database, "citics-primary", symbol=None,
                                            start=date(2026, 9, 1), end=date(2026, 9, 30)))
    assert result == {"records": [], "trades": []}
    (records_sql, records_params), (trades_sql, trades_params) = database.connection.statements
    assert "FROM quant.discipline_compliance" in records_sql
    assert "LEFT JOIN quant.broker_trade_records" in records_sql
    assert records_params[:3] == ("citics-primary", None, None)
    assert "FROM quant.broker_trade_records" in trades_sql
    assert trades_params[:5] == ("citics-primary", None, None, date(2026, 9, 1), date(2026, 9, 30))
    assert_read_only(database)


def test_daily_bars_read_the_generator_window_and_the_rows_after_it():
    database = FakeDatabase([{"trading_date": date(2026, 9, 18)}], [])
    result = run(repository.daily_bars_for_plan(database, "600613.SH", date(2026, 9, 18), date(2026, 9, 19)))
    assert result["before"] == [{"trading_date": date(2026, 9, 18)}] and result["after"] == []
    (before_sql, before_params), (after_sql, after_params) = database.connection.statements
    assert "FROM quant.canonical_bars_daily" in before_sql and "trading_date<=%s" in before_sql
    assert "is_suspended" in before_sql and "pre_close" in before_sql
    assert before_params == ("600613.SH", date(2026, 9, 18), 60)
    assert after_params == ("600613.SH", date(2026, 9, 18), date(2026, 9, 19))
    assert "adj_factor" not in before_sql        # raw prices, exactly as the generator reads them
    assert_read_only(database)


def test_stored_minutes_return_only_longhu_rows_and_count_the_rest():
    database = FakeDatabase([], [{"rows": 36}])
    result = run(repository.stored_minutes(database, "600664.SH", date(2026, 9, 16)))
    assert result == {"rows": [], "source": None, "other_source_rows": 36}
    (rows_sql, rows_params), (count_sql, count_params) = database.connection.statements
    assert "FROM quant.intraday_minute_sessions" in rows_sql and "source_name=%s" in rows_sql
    assert rows_params == ("600664.SH", date(2026, 9, 16), "longhu_intraday_minutes")
    assert count_params == ("600664.SH", date(2026, 9, 16), "longhu_intraday_minutes")
    assert_read_only(database)


def test_open_sessions_read_the_exchange_calendar():
    database = FakeDatabase([{"calendar_date": date(2026, 9, 21)}])
    assert run(repository.open_sessions(database, date(2026, 9, 18), date(2026, 9, 28))) == [date(2026, 9, 21)]
    sql, params = database.connection.statements[0]
    assert "FROM quant.market_trade_calendar" in sql and "is_open" in sql
    assert params == ("SSE", date(2026, 9, 18), date(2026, 9, 28))
    assert_read_only(database)


def test_router_dependencies_wire_every_read_projection():
    async def live(symbol):
        return {}
    deps = repository.router_dependencies("db", live_minutes=live)
    assert deps.async_database == "db" and deps.live_minutes is live
    assert deps.plan_history is repository.plan_history
    assert deps.plan_evaluations is repository.plan_evaluations
    assert deps.reconciliations is repository.reconciliations
    assert deps.daily_bars is repository.daily_bars_for_plan
    assert deps.open_sessions is repository.open_sessions
    assert deps.stored_minutes is repository.stored_minutes
    assert deps.latest_plans is repository.latest_plans and deps.read_plan is repository.read_plan
