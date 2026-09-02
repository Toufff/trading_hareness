import unittest

import retention_maintenance


class _Cursor:
    def __init__(self, connection):
        self._connection = connection
        self._last = ("", None)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        self._last = (" ".join(sql.split()), params)
        self._connection.statements.append(self._last)

    def fetchone(self):
        sql, params = self._last
        if "apply_retention_policy" in sql:
            return (self._connection.batches.pop(0),)
        if sql.startswith("SELECT count(*)::bigint FROM quant."):
            return (self._connection.expired,)
        raise AssertionError(sql)

    def fetchall(self):
        sql, params = self._last
        table = params[0]
        return [row for row in self._connection.policies if table is None or row[0] == table]


class _Connection:
    def __init__(self, policies, *, batches=(), expired=0):
        self.policies = policies
        self.batches = list(batches)
        self.expired = expired
        self.statements = []
        self.closed = False

    def cursor(self):
        return _Cursor(self)

    def close(self):
        self.closed = True


POLICIES = [
    ("intraday_quote_observations", "observed_at", 45, 20000, True),
    ("market_bars_minute", "bar_time", 400, 20000, False),
]


class RetentionMaintenanceTests(unittest.TestCase):
    def test_dry_run_reports_expired_rows_without_deleting(self):
        connection = _Connection(POLICIES, expired=1234)
        result = retention_maintenance.run(["--dry-run"], connection=connection)

        self.assertEqual(result["dry_run"], True)
        statuses = {row["table_name"]: row["status"] for row in result["policies"]}
        self.assertEqual(statuses, {"intraday_quote_observations": "dry_run", "market_bars_minute": "disabled"})
        self.assertEqual(result["policies"][0]["expired_rows"], 1234)
        self.assertFalse(any("apply_retention_policy" in sql for sql, _ in connection.statements))
        count_sql = next(sql for sql, _ in connection.statements if sql.startswith("SELECT count(*)"))
        self.assertIn("FROM quant.intraday_quote_observations WHERE observed_at < now() - make_interval", count_sql)
        # The caller-provided connection is left open for the caller.
        self.assertFalse(connection.closed)

    def test_apply_loops_batches_until_a_batch_deletes_nothing(self):
        connection = _Connection(POLICIES, batches=[20000, 20000, 137, 0])
        result = retention_maintenance.run([], connection=connection)

        applied = result["policies"][0]
        self.assertEqual(applied["status"], "applied")
        self.assertEqual(applied["deleted_rows"], 40137)
        self.assertEqual(applied["batches"], 4)
        self.assertTrue(applied["complete"])
        calls = [params for sql, params in connection.statements if "apply_retention_policy" in sql]
        self.assertEqual(calls, [("intraday_quote_observations",)] * 4)
        self.assertEqual(result["policies"][1]["status"], "disabled")

    def test_max_batches_bounds_one_invocation(self):
        connection = _Connection(POLICIES, batches=[20000, 20000, 20000, 20000])
        result = retention_maintenance.run(["--max-batches", "2"], connection=connection)

        applied = result["policies"][0]
        self.assertEqual(applied["batches"], 2)
        self.assertEqual(applied["deleted_rows"], 40000)
        self.assertFalse(applied["complete"])
        self.assertEqual(connection.batches, [20000, 20000])

    def test_table_filter_and_unknown_table(self):
        connection = _Connection(POLICIES, batches=[0])
        result = retention_maintenance.run(["--table", "market_bars_minute"], connection=connection)
        self.assertEqual([row["table_name"] for row in result["policies"]], ["market_bars_minute"])

        with self.assertRaises(SystemExit):
            retention_maintenance.run(["--table", "nope"], connection=_Connection(POLICIES))

    def test_non_positive_max_batches_is_rejected_before_connecting(self):
        with self.assertRaises(SystemExit):
            retention_maintenance.run(["--max-batches", "0"], connection=_Connection(POLICIES))


if __name__ == "__main__":
    unittest.main()
