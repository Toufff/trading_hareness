"""WP6: connection-level guards and the opt-in long-task statement_timeout.

Audit (section B, HIGH): the whole service opened every connection with no
``statement_timeout``/``lock_timeout``/``idle_in_transaction_session_timeout``,
so one PG lock wait or a runaway plan could occupy a bounded executor worker
indefinitely. These tests exercise the pure helpers and the opt-in override
without opening a real PostgreSQL connection.
"""

from __future__ import annotations

import unittest

from app.database import (
    DEFAULT_IDLE_IN_TRANSACTION_SESSION_TIMEOUT_MS,
    DEFAULT_LOCK_TIMEOUT_MS,
    DEFAULT_STATEMENT_TIMEOUT_MS,
    LONG_TASK_STATEMENT_TIMEOUT_MS,
    AsyncDatabase,
    Database,
    connection_options_string,
    connection_statement_timeouts_ms,
)


class ConnectionStatementTimeoutsTests(unittest.TestCase):
    def test_defaults_when_env_is_empty(self) -> None:
        timeouts = connection_statement_timeouts_ms({})
        self.assertEqual(timeouts["statement_timeout_ms"], DEFAULT_STATEMENT_TIMEOUT_MS)
        self.assertEqual(timeouts["lock_timeout_ms"], DEFAULT_LOCK_TIMEOUT_MS)
        self.assertEqual(
            timeouts["idle_in_transaction_session_timeout_ms"], DEFAULT_IDLE_IN_TRANSACTION_SESSION_TIMEOUT_MS,
        )

    def test_env_overrides_are_applied_and_bounded(self) -> None:
        timeouts = connection_statement_timeouts_ms({
            "QUANT_DB_STATEMENT_TIMEOUT_MS": "45000",
            "QUANT_DB_LOCK_TIMEOUT_MS": "999999999",  # above the 300_000 ceiling
            "QUANT_DB_IDLE_IN_TRANSACTION_TIMEOUT_MS": "-5",  # below zero, clamps to 0
        })
        self.assertEqual(timeouts["statement_timeout_ms"], 45_000)
        self.assertEqual(timeouts["lock_timeout_ms"], 300_000)
        self.assertEqual(timeouts["idle_in_transaction_session_timeout_ms"], 0)

    def test_invalid_env_value_falls_back_to_default(self) -> None:
        timeouts = connection_statement_timeouts_ms({"QUANT_DB_STATEMENT_TIMEOUT_MS": "not-a-number"})
        self.assertEqual(timeouts["statement_timeout_ms"], DEFAULT_STATEMENT_TIMEOUT_MS)


class ConnectionOptionsStringTests(unittest.TestCase):
    def test_contains_runtime_profile_and_all_three_guards_plus_timezone(self) -> None:
        options = connection_options_string({}, runtime_profile="research")
        self.assertIn("app.quant_runtime_profile=research", options)
        self.assertIn(f"statement_timeout={DEFAULT_STATEMENT_TIMEOUT_MS}", options)
        self.assertIn(f"lock_timeout={DEFAULT_LOCK_TIMEOUT_MS}", options)
        self.assertIn(
            f"idle_in_transaction_session_timeout={DEFAULT_IDLE_IN_TRANSACTION_SESSION_TIMEOUT_MS}", options,
        )
        self.assertIn("TimeZone=Asia/Shanghai", options)

    def test_reflects_env_overrides(self) -> None:
        options = connection_options_string({"QUANT_DB_STATEMENT_TIMEOUT_MS": "12345"}, runtime_profile="full")
        self.assertIn("statement_timeout=12345", options)


class DatabaseConnectOptionsWiringTests(unittest.TestCase):
    def test_database_construction_wires_the_options_string_without_opening_a_connection(self) -> None:
        database = Database()
        self.assertIn("statement_timeout=", database._connect_kwargs["options"])
        self.assertIn("TimeZone=Asia/Shanghai", database._connect_kwargs["options"])

    def test_async_database_inherits_the_same_options_string(self) -> None:
        database = Database()
        async_database = AsyncDatabase(database)
        self.assertEqual(async_database._connect_kwargs["options"], database._connect_kwargs["options"])

    def test_async_read_pool_defaults_raised_from_1_4_to_2_8(self) -> None:
        async_database = AsyncDatabase(Database())
        self.assertEqual(async_database._pool_settings["min_size"], 2)
        self.assertEqual(async_database._pool_settings["max_size"], 8)

    def test_async_read_pool_max_size_can_now_exceed_the_old_hardcoded_ceiling_of_8(self) -> None:
        import os
        from unittest.mock import patch

        with patch.dict(os.environ, {"QUANT_ASYNC_READ_POOL_MAX_SIZE": "16"}, clear=False):
            async_database = AsyncDatabase(Database())
        self.assertEqual(async_database._pool_settings["max_size"], 16)


class _FakeCursor:
    def fetchone(self):
        return None


class _FakeConnectionTransaction:
    def __enter__(self):
        return None

    def __exit__(self, *_args):
        return False


class _FakeConnection:
    def __init__(self) -> None:
        self.executed: list[tuple[str, object]] = []

    def transaction(self):
        return _FakeConnectionTransaction()

    def execute(self, sql, params=None):
        self.executed.append((sql, params))
        return _FakeCursor()


class _FakePoolConnectionContext:
    def __init__(self, connection: _FakeConnection) -> None:
        self._connection = connection

    def __enter__(self) -> _FakeConnection:
        return self._connection

    def __exit__(self, *_args) -> bool:
        return False


class _FakeSyncPool:
    def __init__(self, connection: _FakeConnection) -> None:
        self._connection = connection

    def open(self, wait=True, timeout=None) -> None:
        pass

    def close(self) -> None:
        pass

    def connection(self):
        return _FakePoolConnectionContext(self._connection)

    def get_stats(self):
        return {}


class _FakeAsyncConnectionTransaction:
    async def __aenter__(self):
        return None

    async def __aexit__(self, *_args):
        return False


class _FakeAsyncConnection:
    def __init__(self) -> None:
        self.executed: list[tuple[str, object]] = []

    def transaction(self):
        return _FakeAsyncConnectionTransaction()

    async def execute(self, sql, params=None):
        self.executed.append((sql, params))
        return _FakeCursor()


class _FakeAsyncPoolConnectionContext:
    def __init__(self, connection: _FakeAsyncConnection) -> None:
        self._connection = connection

    async def __aenter__(self) -> _FakeAsyncConnection:
        return self._connection

    async def __aexit__(self, *_args) -> bool:
        return False


class _FakeAsyncPool:
    def __init__(self, connection: _FakeAsyncConnection) -> None:
        self._connection = connection

    async def open(self, wait=True, timeout=None) -> None:
        pass

    async def close(self) -> None:
        pass

    def connection(self):
        return _FakeAsyncPoolConnectionContext(self._connection)

    def get_stats(self):
        return {}


class TransactionStatementTimeoutOverrideTests(unittest.TestCase):
    def test_transaction_without_override_never_sets_a_local_timeout(self) -> None:
        database = Database()
        fake_connection = _FakeConnection()
        database._pool = _FakeSyncPool(fake_connection)
        database._opened = True
        with database.transaction() as connection:
            pass
        self.assertEqual(connection.executed, [])

    def test_transaction_with_override_issues_set_local_statement_timeout(self) -> None:
        database = Database()
        fake_connection = _FakeConnection()
        database._pool = _FakeSyncPool(fake_connection)
        database._opened = True
        with database.transaction(statement_timeout_ms=12345) as connection:
            pass
        self.assertEqual(connection.executed, [("SET LOCAL statement_timeout = 12345", None)])

    def test_long_transaction_defaults_to_the_shared_long_task_budget(self) -> None:
        database = Database()
        fake_connection = _FakeConnection()
        database._pool = _FakeSyncPool(fake_connection)
        database._opened = True
        with database.long_transaction() as connection:
            pass
        self.assertEqual(
            connection.executed, [(f"SET LOCAL statement_timeout = {LONG_TASK_STATEMENT_TIMEOUT_MS}", None)],
        )

    def test_long_transaction_accepts_an_explicit_override(self) -> None:
        database = Database()
        fake_connection = _FakeConnection()
        database._pool = _FakeSyncPool(fake_connection)
        database._opened = True
        with database.long_transaction(600_000) as connection:
            pass
        self.assertEqual(connection.executed, [("SET LOCAL statement_timeout = 600000", None)])


class AsyncTransactionStatementTimeoutOverrideTests(unittest.IsolatedAsyncioTestCase):
    async def test_async_transaction_with_override_issues_set_local_statement_timeout(self) -> None:
        async_database = AsyncDatabase(Database())
        fake_connection = _FakeAsyncConnection()
        async_database._pool = _FakeAsyncPool(fake_connection)
        async_database._opened = True
        async with async_database.transaction(statement_timeout_ms=7_000) as connection:
            pass
        self.assertEqual(connection.executed, [("SET LOCAL statement_timeout = 7000", None)])

    async def test_async_long_transaction_defaults_to_the_shared_long_task_budget(self) -> None:
        async_database = AsyncDatabase(Database())
        fake_connection = _FakeAsyncConnection()
        async_database._pool = _FakeAsyncPool(fake_connection)
        async_database._opened = True
        async with async_database.long_transaction() as connection:
            pass
        self.assertEqual(
            connection.executed, [(f"SET LOCAL statement_timeout = {LONG_TASK_STATEMENT_TIMEOUT_MS}", None)],
        )


if __name__ == "__main__":
    unittest.main()
