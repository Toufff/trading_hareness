from __future__ import annotations

import uuid
import unittest

from app.async_runtime_lease_repository import acquire, check_fence, current_fence, release, renew
from app.runtime_leases import LeaseLostError


class _Result:
    def __init__(self, row=None):
        self.row = row

    async def fetchone(self):
        return self.row


class _Connection:
    def __init__(self):
        self.calls = []

    async def execute(self, query, params):
        self.calls.append((query, params))
        if query.lstrip().startswith("DELETE"):
            return _Result()
        # ``fence`` increments only on a fresh acquire and stays stable
        # across a renewal by the same holder (see runtime_leases.py).
        return _Result({"fence": 1})


class _Transaction:
    def __init__(self, connection):
        self.connection = connection

    async def __aenter__(self):
        return self.connection

    async def __aexit__(self, *_):
        return False


class _Database:
    def __init__(self):
        self.connection = _Connection()

    def transaction(self):
        return _Transaction(self.connection)


class AsyncRuntimeLeaseRepositoryTests(unittest.IsolatedAsyncioTestCase):
    async def test_acquire_renew_release_keep_same_holder_and_expiry_guards(self) -> None:
        database = _Database()
        holder = uuid.uuid4()

        self.assertEqual(await acquire(database, "background_loop:test", holder, 120), 1)
        self.assertEqual(await renew(database, "background_loop:test", holder, 120), 1)
        await release(database, "background_loop:test", holder)

        acquire_query, acquire_params = database.connection.calls[0]
        renew_query, renew_params = database.connection.calls[1]
        release_query, release_params = database.connection.calls[2]
        self.assertIn("WHERE quant.runtime_leases.expires_at <= now()", acquire_query)
        self.assertIn("fence=quant.runtime_leases.fence+1", acquire_query)
        self.assertIn("RETURNING fence", acquire_query)
        self.assertEqual(acquire_params, ("background_loop:test", holder, 120))
        self.assertIn("holder_id=%s AND expires_at > now()", renew_query)
        self.assertIn("RETURNING fence", renew_query)
        self.assertNotIn("fence=quant.runtime_leases.fence+1", renew_query)
        self.assertEqual(renew_params, (120, "background_loop:test", holder))
        self.assertIn("WHERE lease_key=%s AND holder_id=%s", release_query)
        self.assertEqual(release_params, ("background_loop:test", holder))

    async def test_acquire_returns_none_when_the_lease_is_held_elsewhere(self) -> None:
        class _NotAcquiredConnection(_Connection):
            async def execute(self, query, params):
                self.calls.append((query, params))
                return _Result(None)

        database = _Database()
        database.connection = _NotAcquiredConnection()
        self.assertIsNone(await acquire(database, "background_loop:test", uuid.uuid4(), 120))

    async def test_check_fence_passes_when_the_live_fence_still_matches(self) -> None:
        class _FenceConnection(_Connection):
            async def execute(self, query, params):
                self.calls.append((query, params))
                return _Result({"fence": 3})

        database = _Database()
        database.connection = _FenceConnection()
        self.assertEqual(await current_fence(database, "post_close_refresh_v1"), 3)
        await check_fence(database, "post_close_refresh_v1", 3)  # must not raise

    async def test_check_fence_raises_lease_lost_when_the_fence_moved_on(self) -> None:
        class _FenceConnection(_Connection):
            async def execute(self, query, params):
                self.calls.append((query, params))
                return _Result({"fence": 4})

        database = _Database()
        database.connection = _FenceConnection()
        with self.assertRaises(LeaseLostError):
            await check_fence(database, "post_close_refresh_v1", 3)

    async def test_check_fence_raises_lease_lost_when_the_lease_row_is_gone(self) -> None:
        class _FenceConnection(_Connection):
            async def execute(self, query, params):
                self.calls.append((query, params))
                return _Result(None)

        database = _Database()
        database.connection = _FenceConnection()
        with self.assertRaises(LeaseLostError):
            await check_fence(database, "post_close_refresh_v1", 1)


if __name__ == "__main__":
    unittest.main()
