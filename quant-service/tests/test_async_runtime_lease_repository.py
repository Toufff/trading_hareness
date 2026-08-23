from __future__ import annotations

import uuid
import unittest

from app.async_runtime_lease_repository import acquire, release, renew


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
        return _Result({"holder_id": params[1] if query.lstrip().startswith("INSERT") else params[2]})


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

        self.assertTrue(await acquire(database, "background_loop:test", holder, 120))
        self.assertTrue(await renew(database, "background_loop:test", holder, 120))
        await release(database, "background_loop:test", holder)

        acquire_query, acquire_params = database.connection.calls[0]
        renew_query, renew_params = database.connection.calls[1]
        release_query, release_params = database.connection.calls[2]
        self.assertIn("WHERE quant.runtime_leases.expires_at <= now()", acquire_query)
        self.assertEqual(acquire_params, ("background_loop:test", holder, 120))
        self.assertIn("holder_id=%s AND expires_at > now()", renew_query)
        self.assertEqual(renew_params, (120, "background_loop:test", holder))
        self.assertIn("WHERE lease_key=%s AND holder_id=%s", release_query)
        self.assertEqual(release_params, ("background_loop:test", holder))


if __name__ == "__main__":
    unittest.main()
