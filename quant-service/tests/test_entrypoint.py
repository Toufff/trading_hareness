import sys
import unittest
from unittest.mock import patch

import entrypoint


class _Cursor:
    def __init__(self, connection):
        self._connection = connection
        self._last = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        self._connection.events.append(("sql", " ".join(sql.split()), params))
        self._last = sql

    def fetchone(self):
        if "pg_try_advisory_lock" in self._last:
            return (self._connection.lock_answers.pop(0),)
        if "pg_advisory_unlock" in self._last:
            return (True,)
        raise AssertionError(f"unexpected fetch for {self._last}")


class _Connection:
    """Fake psycopg connection recording lock / unlock / close order."""

    def __init__(self, lock_answers=(True,)):
        self.events = []
        self.lock_answers = list(lock_answers)
        self.closed = False

    def cursor(self):
        return _Cursor(self)

    def close(self):
        self.closed = True
        self.events.append(("close",))


class _Exec(Exception):
    """Raised by the fake execvp so the test can observe the hand-off."""


class EntrypointTests(unittest.TestCase):
    def test_migration_command_uses_the_active_python_environment(self):
        command = entrypoint.migration_command()
        self.assertEqual(command[0], sys.executable)
        self.assertEqual(command[1:3], ["-m", "alembic"])
        self.assertEqual(command[-2:], ["upgrade", "head"])
        self.assertIn("alembic.ini", command)

    def test_main_locks_then_migrates_then_releases_then_execs(self):
        connection = _Connection()
        order = []

        def fake_run(command, check):
            order.append(("run", tuple(command), check))
            connection.events.append(("run",))

        def fake_execvp(file, argv):
            connection.events.append(("exec", file, tuple(argv)))
            raise _Exec()

        with patch.object(entrypoint, "database_connection", return_value=connection), \
                patch.object(entrypoint.subprocess, "run", fake_run), \
                patch.object(entrypoint.os, "execvp", fake_execvp):
            with self.assertRaises(_Exec):
                entrypoint.main(["uvicorn", "app.main:app"])

        kinds = [event[0] if event[0] != "sql" else event[1].split("(")[0] for event in connection.events]
        self.assertEqual(kinds, [
            "SELECT pg_try_advisory_lock",
            "run",
            "SELECT pg_advisory_unlock",
            "close",
            "exec",
        ])
        self.assertEqual(connection.events[0][2], (entrypoint.MIGRATION_ADVISORY_LOCK_KEY,))
        self.assertEqual(connection.events[2][2], (entrypoint.MIGRATION_ADVISORY_LOCK_KEY,))
        self.assertEqual(order, [("run", tuple(entrypoint.migration_command()), True)])
        self.assertEqual(connection.events[-1], ("exec", "uvicorn", ("uvicorn", "app.main:app")))

    def test_failed_migration_releases_lock_and_never_execs(self):
        connection = _Connection()

        def failing_run(command, check):
            raise RuntimeError("alembic failed")

        with patch.object(entrypoint, "database_connection", return_value=connection), \
                patch.object(entrypoint.subprocess, "run", failing_run), \
                patch.object(entrypoint.os, "execvp") as execvp:
            with self.assertRaisesRegex(RuntimeError, "alembic failed"):
                entrypoint.main(["uvicorn"])

        execvp.assert_not_called()
        self.assertTrue(any(event[0] == "sql" and "pg_advisory_unlock" in event[1] for event in connection.events))
        self.assertTrue(connection.closed)

    def test_lock_wait_times_out_without_running_migrations(self):
        connection = _Connection(lock_answers=[False, False, False])
        clock = iter([0.0, 0.0, 5.0, 10.0, 100.0])

        with patch.object(entrypoint, "database_connection", return_value=connection), \
                patch.object(entrypoint.time, "monotonic", lambda: next(clock)), \
                patch.object(entrypoint.time, "sleep", lambda _seconds: None), \
                patch.dict(entrypoint.os.environ, {"QUANT_MIGRATION_LOCK_TIMEOUT_SECONDS": "10"}), \
                patch.object(entrypoint.subprocess, "run") as run, \
                patch.object(entrypoint.os, "execvp") as execvp:
            with self.assertRaisesRegex(RuntimeError, "migration lock"):
                entrypoint.main(["uvicorn"])

        run.assert_not_called()
        execvp.assert_not_called()
        self.assertTrue(connection.closed)

    def test_usage_error_without_service_command(self):
        with self.assertRaises(SystemExit):
            entrypoint.main([])


if __name__ == "__main__":
    unittest.main()
