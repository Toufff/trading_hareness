"""WP6: lease fencing for the synchronous durable-lease primitives.

Audit (section B, HIGH): a lease loses effect the moment renew fails, but a
write already dispatched to the bounded blocking executor keeps running to
completion regardless (a started thread cannot be cancelled). ``fence`` is a
classic fencing token: it increments only on a genuine ``acquire`` (a new
ownership epoch), stays stable across every renewal by the same holder, and
a write path captures it once and cheaply detects a stale holder before
committing.
"""

from __future__ import annotations

import unittest
import uuid

from app.runtime_leases import (
    LeaseLostError,
    acquire_runtime_lease,
    check_runtime_lease_fence,
    current_runtime_lease_fence,
    release_runtime_lease,
    renew_runtime_lease,
)


class _Result:
    def __init__(self, row=None):
        self.row = row

    def fetchone(self):
        return self.row


class _Connection:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple]] = []
        self.fence = 0
        self.holder_id: uuid.UUID | None = None
        self.expired = True

    def execute(self, query, params):
        self.calls.append((query, params))
        stripped = query.lstrip()
        if stripped.startswith("DELETE"):
            self.holder_id = None
            return _Result()
        if stripped.startswith("INSERT"):
            if not self.expired:
                return _Result(None)
            self.fence += 1
            self.holder_id = params[1]
            self.expired = False
            return _Result({"fence": self.fence})
        if stripped.startswith("UPDATE"):
            _lease_seconds, _lease_key, holder_id = params
            if self.holder_id != holder_id or self.expired:
                return _Result(None)
            return _Result({"fence": self.fence})
        if stripped.startswith("SELECT"):
            if self.holder_id is None:
                return _Result(None)
            return _Result({"fence": self.fence})
        raise AssertionError(f"unexpected query: {query}")


class _Transaction:
    def __init__(self, connection: _Connection) -> None:
        self.connection = connection

    def __enter__(self) -> _Connection:
        return self.connection

    def __exit__(self, *_args) -> bool:
        return False


class _Database:
    def __init__(self) -> None:
        self.connection = _Connection()

    def transaction(self):
        return _Transaction(self.connection)


class RuntimeLeaseFencingTests(unittest.TestCase):
    def test_acquire_starts_a_fresh_lease_at_fence_one(self) -> None:
        database = _Database()
        holder = uuid.uuid4()
        fence = acquire_runtime_lease(database, "post_close_refresh_v1", holder, 1800)
        self.assertEqual(fence, 1)

    def test_acquire_query_increments_fence_and_returns_it(self) -> None:
        database = _Database()
        holder = uuid.uuid4()
        acquire_runtime_lease(database, "post_close_refresh_v1", holder, 1800)
        query, _params = database.connection.calls[0]
        self.assertIn("fence=quant.runtime_leases.fence+1", query)
        self.assertIn("RETURNING fence", query)

    def test_renew_keeps_the_fence_stable_across_many_renewals(self) -> None:
        database = _Database()
        holder = uuid.uuid4()
        fence = acquire_runtime_lease(database, "post_close_refresh_v1", holder, 1800)
        for _ in range(20):
            self.assertEqual(renew_runtime_lease(database, "post_close_refresh_v1", holder, 1800), fence)

    def test_renew_query_does_not_increment_fence(self) -> None:
        database = _Database()
        holder = uuid.uuid4()
        acquire_runtime_lease(database, "post_close_refresh_v1", holder, 1800)
        renew_runtime_lease(database, "post_close_refresh_v1", holder, 1800)
        query, _params = database.connection.calls[1]
        self.assertNotIn("fence=quant.runtime_leases.fence+1", query)
        self.assertIn("RETURNING fence", query)

    def test_renew_by_a_different_holder_fails_and_does_not_touch_the_fence(self) -> None:
        database = _Database()
        holder = uuid.uuid4()
        fence = acquire_runtime_lease(database, "post_close_refresh_v1", holder, 1800)
        self.assertIsNone(renew_runtime_lease(database, "post_close_refresh_v1", uuid.uuid4(), 1800))
        self.assertEqual(current_runtime_lease_fence(database, "post_close_refresh_v1"), fence)

    def test_a_new_holder_taking_over_an_expired_lease_advances_the_fence(self) -> None:
        database = _Database()
        first_holder = uuid.uuid4()
        first_fence = acquire_runtime_lease(database, "post_close_refresh_v1", first_holder, 1800)

        # Simulate expiry: the first holder's lease lapsed.
        database.connection.expired = True
        second_holder = uuid.uuid4()
        second_fence = acquire_runtime_lease(database, "post_close_refresh_v1", second_holder, 1800)

        self.assertEqual(second_fence, first_fence + 1)

    def test_release_then_reacquire_still_advances_the_fence_rather_than_resetting_it(self) -> None:
        database = _Database()
        holder = uuid.uuid4()
        first_fence = acquire_runtime_lease(database, "post_close_refresh_v1", holder, 1800)
        release_runtime_lease(database, "post_close_refresh_v1", holder)
        database.connection.expired = True
        second_fence = acquire_runtime_lease(database, "post_close_refresh_v1", uuid.uuid4(), 1800)
        self.assertGreater(second_fence, first_fence)


class CheckRuntimeLeaseFenceTests(unittest.TestCase):
    def test_passes_when_the_live_fence_still_matches(self) -> None:
        database = _Database()
        holder = uuid.uuid4()
        fence = acquire_runtime_lease(database, "post_close_refresh_v1", holder, 1800)
        check_runtime_lease_fence(database, "post_close_refresh_v1", fence)  # must not raise

    def test_raises_lease_lost_when_a_new_holder_has_taken_over(self) -> None:
        database = _Database()
        first_holder = uuid.uuid4()
        first_fence = acquire_runtime_lease(database, "post_close_refresh_v1", first_holder, 1800)
        database.connection.expired = True
        acquire_runtime_lease(database, "post_close_refresh_v1", uuid.uuid4(), 1800)

        with self.assertRaises(LeaseLostError):
            check_runtime_lease_fence(database, "post_close_refresh_v1", first_fence)

    def test_raises_lease_lost_when_the_lease_row_no_longer_exists(self) -> None:
        database = _Database()
        holder = uuid.uuid4()
        fence = acquire_runtime_lease(database, "post_close_refresh_v1", holder, 1800)
        release_runtime_lease(database, "post_close_refresh_v1", holder)

        with self.assertRaises(LeaseLostError):
            check_runtime_lease_fence(database, "post_close_refresh_v1", fence)


if __name__ == "__main__":
    unittest.main()
