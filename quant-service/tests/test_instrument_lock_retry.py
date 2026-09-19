"""Contract tests for ``app.instrument_lock_retry.execute_instrument_write``.

The fake below subclasses ``psycopg.Connection`` (without opening one) so the
helper takes its real retry path, and it models the three server behaviours
the helper depends on: a savepoint rollback undoes ``SET LOCAL`` and the
statement, a RELEASE keeps ``SET LOCAL`` in force for the outer transaction,
and the outer transaction stays usable after a rollback to a savepoint.  The
class at the bottom runs the same contract against a real PostgreSQL server
with two sessions (skipped without ``PGHOST``, like the other DB tests).
"""

from __future__ import annotations

import logging
import os
import threading
import time
import unittest
from contextlib import contextmanager

import psycopg
from psycopg import errors as pg_errors

from app.instrument_lock_retry import (
    DEFAULT_INSTRUMENT_LOCK_BUDGET_SECONDS,
    DEFAULT_INSTRUMENT_LOCK_MAX_ATTEMPTS,
    DEFAULT_INSTRUMENT_LOCK_TIMEOUT_MS,
    InstrumentLockPolicy,
    InstrumentLockRetryExhausted,
    execute_instrument_write,
    instrument_lock_counters,
)

WRITER_SQL = "INSERT INTO quant.instruments(symbol) SELECT * FROM unnest(%s::text[]) ORDER BY 1 ON CONFLICT(symbol) DO NOTHING"


class _Result:
    def __init__(self, row=None, rows=(), rowcount=-1):
        self._row, self._rows, self.rowcount = row, list(rows), rowcount

    def fetchone(self):
        return self._row

    def fetchall(self):
        return self._rows


class _ServerConnection(psycopg.Connection):
    """A psycopg ``Connection`` in type only; its behaviour is scripted."""

    def __init__(self, outcomes, *, lock_timeout="5s", deadlock_timeout_ms=1000):  # noqa: D107
        self.outcomes = list(outcomes)   # one per writer attempt: None (success) or an exception
        self.lock_timeout = lock_timeout
        self.deadlock_timeout_ms = deadlock_timeout_ms
        self.events: list[str] = []
        self.writer_timeouts: list[str] = []   # lock_timeout in force at each writer attempt
        self.aborted = False                   # outer transaction state

    def __del__(self):  # never opened, nothing to close
        pass

    @contextmanager
    def transaction(self, *args, **kwargs):
        saved = self.lock_timeout
        self.events.append("SAVEPOINT")
        try:
            yield self
        except BaseException:
            self.lock_timeout = saved            # SET LOCAL undone with the subtransaction
            self.aborted = False                 # ROLLBACK TO SAVEPOINT clears the error
            self.events.append("ROLLBACK TO SAVEPOINT")
            raise
        self.events.append("RELEASE")          # SET LOCAL survives into the outer transaction

    def execute(self, query, params=None, **kwargs):
        if self.aborted:
            raise pg_errors.InFailedSqlTransaction("current transaction is aborted")
        if query.startswith("SELECT current_setting('lock_timeout')"):
            return _Result({"lock_timeout": self.lock_timeout, "deadlock_timeout_ms": self.deadlock_timeout_ms})
        if query.startswith("SELECT set_config('lock_timeout'"):
            self.lock_timeout = params[0]
            return _Result()
        if query.startswith("SELECT a.pid"):
            return _Result(rows=[{"pid": 4242, "application_name": "peer", "xact_age_s": 3.5,
                                  "wait_event_type": None, "query": "INSERT ..."}])
        if query == WRITER_SQL:
            self.writer_timeouts.append(self.lock_timeout)
            self.events.append("WRITE")
            outcome = self.outcomes.pop(0) if self.outcomes else None
            if outcome is not None:
                self.aborted = True
                raise outcome
            return _Result(rowcount=3)
        self.events.append(query)
        return _Result(row={"ok": 1})


class _Clock:
    def __init__(self, step: float = 0.0):
        self.now, self.step = 0.0, step

    def __call__(self) -> float:
        self.now += self.step
        return self.now


def _lock_timeout():
    return pg_errors.LockNotAvailable("canceling statement due to lock timeout")


def _deadlock():
    return pg_errors.DeadlockDetected("deadlock detected")


class ExecuteInstrumentWriteTests(unittest.TestCase):
    def run_write(self, connection, policy=None, clock=None):
        self.sleeps: list[float] = []
        return execute_instrument_write(
            connection, WRITER_SQL, (["000001.SZ"],), writer="test.writer",
            policy=policy or InstrumentLockPolicy(),
            sleep=self.sleeps.append, clock=clock or _Clock(), rng=lambda low, high: high,
        )

    def test_a_clean_write_is_one_savepoint_and_restores_the_timeout(self) -> None:
        connection = _ServerConnection([None])
        self.assertEqual(self.run_write(connection), 3)
        self.assertEqual(connection.events, ["SAVEPOINT", "WRITE", "RELEASE"])
        self.assertEqual(connection.writer_timeouts, ["300ms"])
        self.assertEqual(connection.lock_timeout, "5s", "SET LOCAL leaked into the outer transaction")
        self.assertEqual(self.sleeps, [])

    def test_lock_not_available_is_rolled_back_and_retried(self) -> None:
        connection = _ServerConnection([_lock_timeout(), _lock_timeout(), None])
        self.assertEqual(self.run_write(connection), 3)
        self.assertEqual(connection.events, [
            "SAVEPOINT", "WRITE", "ROLLBACK TO SAVEPOINT",
            "SAVEPOINT", "RELEASE",                  # the holders diagnostic, first retry only, own savepoint
            "SAVEPOINT", "WRITE", "ROLLBACK TO SAVEPOINT",
            "SAVEPOINT", "WRITE", "RELEASE",
        ])
        self.assertEqual(connection.events[-3:], ["SAVEPOINT", "WRITE", "RELEASE"])
        self.assertEqual(connection.writer_timeouts, ["300ms"] * 3)
        self.assertEqual(connection.lock_timeout, "5s")
        self.assertEqual(len(self.sleeps), 2)

    def test_deadlock_detected_is_retried_too(self) -> None:
        connection = _ServerConnection([_deadlock(), None])
        self.assertEqual(self.run_write(connection), 3)
        self.assertEqual(connection.events.count("WRITE"), 2)
        self.assertEqual(connection.lock_timeout, "5s")

    def test_no_other_error_is_retried(self) -> None:
        for error in (
            pg_errors.UniqueViolation("duplicate key"),
            pg_errors.QueryCanceled("canceling statement due to statement timeout"),
            pg_errors.SerializationFailure("could not serialize access"),
            psycopg.OperationalError("server closed the connection"),
            ValueError("client-side bug"),
        ):
            with self.subTest(type(error).__name__):
                connection = _ServerConnection([error])
                with self.assertRaises(type(error)):
                    self.run_write(connection)
                self.assertEqual(connection.events.count("WRITE"), 1)
                self.assertEqual(self.sleeps, [])
                self.assertEqual(connection.lock_timeout, "5s")

    def test_the_outer_transaction_stays_usable_after_a_retry(self) -> None:
        connection = _ServerConnection([_lock_timeout(), None])
        self.run_write(connection)
        self.assertFalse(connection.aborted)
        self.assertEqual(connection.execute("SELECT 1").fetchone(), {"ok": 1})

    def test_the_outer_transaction_stays_usable_after_exhaustion(self) -> None:
        connection = _ServerConnection([_lock_timeout()] * 3)
        with self.assertRaises(InstrumentLockRetryExhausted):
            self.run_write(connection, policy=InstrumentLockPolicy(max_attempts=3))
        self.assertFalse(connection.aborted)
        self.assertEqual(connection.lock_timeout, "5s")
        self.assertEqual(connection.execute("SELECT 1").fetchone(), {"ok": 1})

    def test_exhaustion_by_attempts_is_a_clear_chained_operational_error(self) -> None:
        connection = _ServerConnection([_lock_timeout()] * 10)
        with self.assertRaises(InstrumentLockRetryExhausted) as caught:
            self.run_write(connection, policy=InstrumentLockPolicy(max_attempts=4))
        error = caught.exception
        self.assertIsInstance(error, psycopg.OperationalError)
        self.assertIsInstance(error.__cause__, pg_errors.LockNotAvailable)
        self.assertEqual((error.writer, error.attempts, error.last_sqlstate), ("test.writer", 4, "55P03"))
        self.assertIn("test.writer", str(error))
        self.assertEqual(connection.events.count("WRITE"), 4)
        self.assertEqual(len(self.sleeps), 3)

    def test_exhaustion_by_total_time(self) -> None:
        """Each attempt costs its 300 ms wait, each backoff its sleep; the budget stops it."""
        connection = _ServerConnection([_lock_timeout()] * 1000)
        clock = _Clock(step=0.3)
        with self.assertRaises(InstrumentLockRetryExhausted) as caught:
            execute_instrument_write(
                connection, WRITER_SQL, (["000001.SZ"],), writer="test.writer",
                policy=InstrumentLockPolicy(max_attempts=1000, budget_seconds=10.0),
                sleep=lambda seconds: setattr(clock, "now", clock.now + seconds),
                clock=clock, rng=lambda low, high: high,
            )
        self.assertLess(caught.exception.attempts, 20, "the 10 s budget, not the 1000 attempts, stopped it")
        self.assertLessEqual(caught.exception.elapsed_seconds, 10.0 + 0.3 + 0.3)

    def test_backoff_is_jittered_growing_and_capped(self) -> None:
        policy = InstrumentLockPolicy()
        upper = [policy.backoff_seconds(n, lambda low, high: high) for n in range(1, 10)]
        self.assertEqual(upper[:4], [0.2, 0.4, 0.8, 1.6])
        self.assertEqual(max(upper), 2.0)
        self.assertEqual(policy.backoff_seconds(3, lambda low, high: low), 0.05, "floor, never a hot spin")

    def test_a_timeout_at_or_above_deadlock_timeout_is_clamped_below_it(self) -> None:
        connection = _ServerConnection([None], deadlock_timeout_ms=400)
        with self.assertLogs("app.instrument_lock_retry", logging.WARNING) as logs:
            self.run_write(connection, policy=InstrumentLockPolicy(lock_timeout_ms=1000))
        self.assertEqual(connection.writer_timeouts, ["200ms"])
        self.assertTrue(any("instrument_lock_timeout_clamped" in line for line in logs.output))
        self.assertEqual(InstrumentLockPolicy(lock_timeout_ms=300).effective_lock_timeout_ms(1000), 300)

    def test_policy_defaults_and_environment_bounds(self) -> None:
        self.assertEqual(
            (DEFAULT_INSTRUMENT_LOCK_TIMEOUT_MS, DEFAULT_INSTRUMENT_LOCK_MAX_ATTEMPTS,
             DEFAULT_INSTRUMENT_LOCK_BUDGET_SECONDS), (300, 60, 60.0),
        )
        self.assertEqual(InstrumentLockPolicy.from_env({}), InstrumentLockPolicy())
        policy = InstrumentLockPolicy.from_env({
            "QUANT_INSTRUMENT_LOCK_TIMEOUT_MS": "250", "QUANT_INSTRUMENT_LOCK_MAX_ATTEMPTS": "5",
            "QUANT_INSTRUMENT_LOCK_BUDGET_SECONDS": "90000",
        })
        self.assertEqual((policy.lock_timeout_ms, policy.max_attempts, policy.budget_seconds), (250, 5, 600))
        self.assertEqual(InstrumentLockPolicy.from_env({"QUANT_INSTRUMENT_LOCK_TIMEOUT_MS": "junk"}).lock_timeout_ms, 300)

    def test_retries_and_exhaustion_are_logged_with_the_holders_and_counted(self) -> None:
        before = instrument_lock_counters()
        connection = _ServerConnection([_lock_timeout(), _deadlock(), _lock_timeout()])
        with self.assertLogs("app.instrument_lock_retry", logging.INFO) as logs:
            with self.assertRaises(InstrumentLockRetryExhausted):
                self.run_write(connection, policy=InstrumentLockPolicy(max_attempts=3))
        retries = [record for record in logs.records if record.getMessage() == "instrument_lock_retry"]
        self.assertEqual([record.sqlstate for record in retries], ["55P03", "40P01"])
        self.assertEqual(retries[0].writer, "test.writer")
        self.assertEqual(retries[0].holders[0]["pid"], 4242)
        exhausted = [record for record in logs.records if record.getMessage() == "instrument_lock_retry_exhausted"]
        self.assertEqual(len(exhausted), 1)
        self.assertEqual(exhausted[0].attempts, 3)
        after = instrument_lock_counters()
        self.assertEqual(after["retries"] - before["retries"], 2)
        self.assertEqual(after["lock_timeouts"] - before["lock_timeouts"], 2)
        self.assertEqual(after["deadlocks"] - before["deadlocks"], 1)
        self.assertEqual(after["exhausted"] - before["exhausted"], 1)

    def test_a_cursor_on_a_server_connection_takes_the_retry_path(self) -> None:
        connection = _ServerConnection([_lock_timeout(), None])

        class _Cursor(psycopg.Cursor):
            def __init__(self, conn):  # noqa: D107
                self._conn = conn

            def __del__(self):
                pass

            def execute(self, query, params=None, **kwargs):
                return self._conn.execute(query, params)

        self.assertEqual(self.run_write(_Cursor(connection)), 3)
        self.assertEqual(connection.events.count("WRITE"), 2)
        self.assertEqual(connection.lock_timeout, "5s")

    def test_a_non_psycopg_object_is_executed_once_without_savepoints(self) -> None:
        """The recording fakes other unit tests drive writers with hold no server locks."""
        calls = []

        class _Recorder:
            def execute(self, query, params=None):
                calls.append((query, params))
                return None

        self.assertEqual(self.run_write(_Recorder()), -1)
        self.assertEqual(calls, [(WRITER_SQL, (["000001.SZ"],))])


@unittest.skipUnless(os.getenv("PGHOST"), "requires the compose PostgreSQL service")
class ExecuteInstrumentWriteAgainstPostgresTests(unittest.TestCase):
    """Two real sessions: one holds an UPDATE on an instrument row, the other
    runs ``ON CONFLICT DO NOTHING`` on it through the helper -- the blocking
    shape the 2026-09-19 mechanism check proved.  Scratch database only."""

    symbol = "999811.SZ"
    statement = (
        "INSERT INTO quant.instruments(symbol,exchange,source) "
        "SELECT t.symbol,'SZSE','lock-retry-test' FROM unnest(%s::text[]) AS t(symbol) "
        "ORDER BY 1 ON CONFLICT(symbol) DO NOTHING"
    )

    def _connect(self):
        from psycopg.rows import dict_row

        from app import db_dsn

        params = db_dsn.connection_params()
        return psycopg.connect(
            host=params["host"], port=int(params["port"]), dbname=params["dbname"],
            user=params["user"], password=params["password"], row_factory=dict_row, connect_timeout=8,
        )

    def setUp(self) -> None:
        with self._connect() as connection:
            connection.execute("DELETE FROM quant.instruments WHERE symbol=%s", (self.symbol,))
            connection.execute(
                "INSERT INTO quant.instruments(symbol,exchange,source) VALUES(%s,'SZSE','lock-retry-test')",
                (self.symbol,),
            )

    def tearDown(self) -> None:
        with self._connect() as connection:
            connection.execute("DELETE FROM quant.instruments WHERE symbol=%s", (self.symbol,))

    def _hold_row(self):
        holder = self._connect()
        holder.execute("UPDATE quant.instruments SET updated_at=now() WHERE symbol=%s", (self.symbol,))
        return holder

    def test_waits_out_a_short_holder_and_restores_the_callers_timeout(self) -> None:
        holder = self._hold_row()
        releaser = threading.Timer(1.2, holder.commit)
        with self._connect() as connection:
            try:
                connection.execute("SET LOCAL lock_timeout = '4s'")
                releaser.start()
                started = time.monotonic()
                execute_instrument_write(connection, self.statement, ([self.symbol],), writer="db-test")
                self.assertGreater(time.monotonic() - started, 1.0)
                self.assertEqual(connection.execute("SHOW lock_timeout").fetchone()["lock_timeout"], "4s")
            finally:
                releaser.join()
                holder.close()

    def test_exhaustion_leaves_the_outer_transaction_usable(self) -> None:
        holder = self._hold_row()
        with self._connect() as connection:
            try:
                prior = connection.execute("SHOW lock_timeout").fetchone()["lock_timeout"]
                connection.execute("CREATE TEMP TABLE lock_retry_probe(x int)")
                with self.assertRaises(InstrumentLockRetryExhausted):
                    execute_instrument_write(
                        connection, self.statement, ([self.symbol],), writer="db-test",
                        policy=InstrumentLockPolicy(max_attempts=3, budget_seconds=5),
                    )
                connection.execute("INSERT INTO lock_retry_probe VALUES(1)")
                self.assertEqual(connection.execute("SELECT count(*) AS n FROM lock_retry_probe").fetchone()["n"], 1)
                self.assertEqual(connection.execute("SHOW lock_timeout").fetchone()["lock_timeout"], prior)
            finally:
                connection.rollback()
                holder.rollback()
                holder.close()


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
