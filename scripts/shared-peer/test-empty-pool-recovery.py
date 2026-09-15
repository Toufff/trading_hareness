"""Real PostgreSQL regression; isolated pools only, no application/schema writes.

Run inside the peer image with its existing PG environment. Three cycles reproduce
psycopg_pool's zero-size/full-expired-queue state and recover through the public
check() guard, without replacing the pool or restarting the process.
"""
import asyncio
import json
import time

from psycopg_pool import AsyncConnectionPool, TooManyRequests
from app.empty_pool_recovery import EmptyPoolRecovery


async def cycle(number):
    pool = AsyncConnectionPool(
        kwargs={"host": "127.0.0.1", "port": 1, "connect_timeout": 1},
        min_size=1, max_size=1, max_waiting=64, timeout=0.15,
        reconnect_timeout=0.2, open=False,
    )
    guard = EmptyPoolRecovery()
    await pool.open()
    try:
        async def borrow():
            try:
                async with pool.connection():
                    pass
            except Exception:
                pass
        await asyncio.gather(*(borrow() for _ in range(64)))
        deadline = time.monotonic() + 5
        while pool.get_stats()['pool_size'] != 0 and time.monotonic() < deadline:
            await asyncio.sleep(0.1)
        before = pool.get_stats()
        assert before['pool_size'] == 0 and before['requests_waiting'] == 64, before
        # Re-enable the real database on THIS disposable pool only.
        pool.kwargs = {}
        try:
            await pool.getconn()
        except TooManyRequests:
            pass
        else:
            raise AssertionError('The original deadlock was not reproduced')
        started = time.monotonic()
        recovered = False
        while time.monotonic() - started < 10:
            await guard.ensure_progress(pool)
            try:
                async with pool.connection() as connection:
                    cursor = await connection.execute('SELECT 1')
                    assert (await cursor.fetchone())[0] == 1
                recovered = True
                break
            except TooManyRequests:
                await asyncio.sleep(0.1)
        assert recovered, pool.get_stats()
        return {'cycle': number, 'before': before, 'after': pool.get_stats(),
                'recovery_seconds': round(time.monotonic() - started, 3)}
    finally:
        await pool.close()


async def main():
    results = [await cycle(i) for i in range(1, 4)]
    print(json.dumps({'passed': True, 'cycles': results}))


if __name__ == '__main__':
    asyncio.run(main())
