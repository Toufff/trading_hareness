"""Unit coverage for the shared repository plumbing in app.repo_common."""

from __future__ import annotations

import unittest

from app.repo_common import (
    SYMBOL_RE,
    async_fetch_all,
    async_fetch_one,
    bounded_limit,
    bounded_offset,
    fetch_all,
    fetch_one,
    next_offset,
    numeric_or_default,
    paginate,
)


class _Result:
    def __init__(self, *, row=None, rows=None):
        self._row, self._rows = row, rows or []

    def fetchone(self):
        return self._row

    def fetchall(self):
        return self._rows

    async def aiofetchone(self):
        return self._row


class _AsyncResult:
    def __init__(self, *, row=None, rows=None):
        self._row, self._rows = row, rows or []

    async def fetchone(self):
        return self._row

    async def fetchall(self):
        return self._rows


class _Connection:
    def __init__(self, result: _Result):
        self.result = result
        self.calls: list[tuple[str, object]] = []

    def execute(self, sql, params=()):
        self.calls.append((sql, params))
        return self.result


class _AsyncConnection:
    def __init__(self, result: _AsyncResult):
        self.result = result
        self.calls: list[tuple[str, object]] = []

    async def execute(self, sql, params=()):
        self.calls.append((sql, params))
        return self.result


class BoundedLimitTests(unittest.TestCase):
    def test_clamps_into_range(self) -> None:
        self.assertEqual(bounded_limit(10, 500), 10)
        self.assertEqual(bounded_limit(50000, 500), 500)
        self.assertEqual(bounded_limit(-5, 500), 1)
        self.assertEqual(bounded_limit(0, 500, minimum=0), 0)

    def test_non_numeric_falls_back_to_minimum(self) -> None:
        self.assertEqual(bounded_limit("not-a-number", 500), 1)
        self.assertEqual(bounded_limit(None, 500, minimum=5), 5)


class BoundedOffsetTests(unittest.TestCase):
    def test_clamps_negative_to_zero(self) -> None:
        self.assertEqual(bounded_offset(-10), 0)
        self.assertEqual(bounded_offset(10), 10)
        self.assertEqual(bounded_offset("bad"), 0)


class NextOffsetTests(unittest.TestCase):
    def test_none_when_page_reaches_the_end(self) -> None:
        self.assertIsNone(next_offset(0, 10, 10))
        self.assertIsNone(next_offset(5, 0, 5))

    def test_advances_when_more_rows_remain(self) -> None:
        self.assertEqual(next_offset(0, 10, 25), 10)
        self.assertEqual(next_offset(10, 10, 25), 20)


class PaginateTests(unittest.TestCase):
    def test_shape_matches_the_duplicated_read_model_contract(self) -> None:
        payload = paginate([1, 2, 3], limit=3, offset=0, total=7)
        self.assertEqual(payload, {"items": [1, 2, 3], "limit": 3, "offset": 0, "total": 7, "next_offset": 3})
        payload = paginate([1, 2], limit=3, offset=6, total=8)
        self.assertIsNone(payload["next_offset"])


class NumericOrDefaultTests(unittest.TestCase):
    def test_none_and_empty_string_use_default(self) -> None:
        self.assertEqual(numeric_or_default(None), 0.0)
        self.assertEqual(numeric_or_default(""), 0.0)
        self.assertEqual(numeric_or_default(None, default=1.5), 1.5)

    def test_coerces_numeric_like_values(self) -> None:
        self.assertEqual(numeric_or_default("3.5"), 3.5)
        self.assertEqual(numeric_or_default(3), 3.0)

    def test_unparseable_value_uses_default(self) -> None:
        self.assertEqual(numeric_or_default("not-a-number", default=2.0), 2.0)


class SymbolRegexTests(unittest.TestCase):
    def test_matches_canonical_a_share_symbols(self) -> None:
        self.assertTrue(SYMBOL_RE.fullmatch("600000.SH"))
        self.assertTrue(SYMBOL_RE.fullmatch("000001.SZ"))
        self.assertTrue(SYMBOL_RE.fullmatch("920001.BJ"))

    def test_rejects_index_or_malformed_codes(self) -> None:
        self.assertIsNone(SYMBOL_RE.fullmatch("sh000300"))
        self.assertIsNone(SYMBOL_RE.fullmatch("60000.SH"))
        self.assertIsNone(SYMBOL_RE.fullmatch("600000.SH "))


class FetchHelperTests(unittest.TestCase):
    def test_fetch_one_and_fetch_all_delegate_to_the_cursor(self) -> None:
        connection = _Connection(_Result(row={"a": 1}, rows=[{"a": 1}, {"a": 2}]))
        self.assertEqual(fetch_one(connection, "SELECT 1", (1,)), {"a": 1})
        self.assertEqual(fetch_all(connection, "SELECT 1", (1,)), [{"a": 1}, {"a": 2}])
        self.assertEqual(connection.calls, [("SELECT 1", (1,)), ("SELECT 1", (1,))])


class AsyncFetchHelperTests(unittest.IsolatedAsyncioTestCase):
    async def test_async_fetch_one_and_fetch_all_delegate_to_the_cursor(self) -> None:
        connection = _AsyncConnection(_AsyncResult(row={"a": 1}, rows=[{"a": 1}, {"a": 2}]))
        self.assertEqual(await async_fetch_one(connection, "SELECT 1"), {"a": 1})
        self.assertEqual(await async_fetch_all(connection, "SELECT 1"), [{"a": 1}, {"a": 2}])
        self.assertEqual(connection.calls, [("SELECT 1", ()), ("SELECT 1", ())])


if __name__ == "__main__":
    unittest.main()
