"""Demand-driven recovery for an empty psycopg pool with a full stale queue.

Use only public pool APIs. Never replace a live pool or replay a transaction.
An ordinary busy pool (connections checked out) is deliberately left alone.
"""
from __future__ import annotations

import asyncio
import logging
import time

logger = logging.getLogger(__name__)


class EmptyPoolRecovery:
    def __init__(self, *, clock=time.monotonic) -> None:
        self._clock = clock
        self._next_check = 0.0
        self._lock = asyncio.Lock()

    async def ensure_progress(self, pool) -> None:
        if pool.closed or pool.get_stats().get('pool_size', 0) != 0:
            return
        if self._clock() < self._next_check:
            return
        async with self._lock:
            if pool.closed or pool.get_stats().get('pool_size', 0) != 0:
                return
            if self._clock() < self._next_check:
                return
            self._next_check = self._clock() + 5.0
            logger.warning('empty_async_pool_recovery_requested', extra={
                'task': 'database_recovery',
                'waiting': pool.get_stats().get('requests_waiting', 0),
            })
            # check() schedules a refill even when getconn() rejects a full
            # waiting queue. No private queue mutation and no pool recreation.
            await asyncio.wait_for(pool.check(), timeout=1.0)
