import asyncio
import unittest
from unittest.mock import AsyncMock

from app.empty_pool_recovery import EmptyPoolRecovery


class EmptyPoolRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_zero_pool_kicks_public_check_once_under_concurrency(self):
        clock = [10.0]
        guard = EmptyPoolRecovery(clock=lambda: clock[0])
        pool = type('Pool', (), {'closed': False})()
        pool.get_stats = lambda: {'pool_size': 0, 'requests_waiting': 64}
        pool.check = AsyncMock()
        await asyncio.gather(*(guard.ensure_progress(pool) for _ in range(20)))
        self.assertEqual(pool.check.await_count, 1)
        clock[0] += 5
        await guard.ensure_progress(pool)
        self.assertEqual(pool.check.await_count, 2)

    async def test_busy_healthy_pool_is_not_reset(self):
        pool = type('Pool', (), {'closed': False})()
        pool.get_stats = lambda: {'pool_size': 8, 'pool_available': 0, 'requests_waiting': 64}
        pool.check = AsyncMock()
        await EmptyPoolRecovery().ensure_progress(pool)
        pool.check.assert_not_awaited()

    async def test_closed_pool_is_not_reopened(self):
        pool = type('Pool', (), {'closed': True})()
        pool.get_stats = lambda: {'pool_size': 0}
        pool.check = AsyncMock()
        await EmptyPoolRecovery().ensure_progress(pool)
        pool.check.assert_not_awaited()

    async def test_check_failure_is_bounded_and_not_retried_in_same_request(self):
        pool = type('Pool', (), {'closed': False})()
        pool.get_stats = lambda: {'pool_size': 0}
        pool.check = AsyncMock(side_effect=RuntimeError('unavailable'))
        guard = EmptyPoolRecovery()
        with self.assertRaises(RuntimeError):
            await guard.ensure_progress(pool)
        await guard.ensure_progress(pool)
        self.assertEqual(pool.check.await_count, 1)
