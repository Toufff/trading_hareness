"""Read-only peer minute baskets; bounded fan-out without orphaned retry pools."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, wait
from datetime import datetime, timezone
import threading
import time
from typing import Any
from zoneinfo import ZoneInfo

from .longhu_vendor_source import intraday_source, longhu_security_id
from .runtime_executors import ExecutorSaturatedError, run_akshare_blocking

DEADLINE_EXCEEDED = "minute_batch_deadline_exceeded"


def current_session_rows(rows: list[dict[str, Any]], observed_at: datetime) -> list[dict[str, Any]]:
    """Preserve owner normalization, adding peer aliases only after date validation.

    The 49ecf02 contract means today's Shanghai calendar date, not the latest
    settled trading day. Overnight/weekend stale tapes are errors, never retagged.
    """
    expected = observed_at.astimezone(ZoneInfo("Asia/Shanghai")).date().isoformat()
    if not rows or any(not isinstance(row, dict) or row.get("session_date") != expected for row in rows):
        raise ValueError("minute_batch_stale_or_missing_session")
    return [dict(row, trade_date=expected.replace("-", ""),
                 trade_time=f"{expected} {row['time'][:2]}:{row['time'][2:]}:00") for row in rows]


class MinuteBatchService:
    """One active basket per API process, including its timed-out physical calls.

    Only one outer bounded-executor slot is used. A basket has at most 12
    vendor workers, and each worker owns its own provider/session. A deadline
    stops new stock submissions; in-flight non-cancellable HTTP calls retain
    batch admission until they really finish. Repeated timeouts cannot spawn
    unbounded private thread pools.
    """

    def __init__(self, *, source_factory=intraday_source, blocking=run_akshare_blocking,
                 now=lambda: datetime.now(timezone.utc)):
        self.source_factory, self.blocking, self.now = source_factory, blocking, now
        self._gate = threading.BoundedSemaphore(1)
        self._idle = threading.Event()
        self._idle.set()

    def wait_idle(self, timeout: float) -> bool:
        return self._idle.wait(timeout)

    async def read(self, symbols: list[str], deadline_seconds: float) -> dict[str, Any]:
        return await self.blocking(self._fetch, symbols, time.monotonic() + deadline_seconds,
                                   timeout_seconds=deadline_seconds + 2.5)

    def _fetch(self, symbols: list[str], stop_at: float) -> dict[str, Any]:
        if not self._gate.acquire(blocking=False):
            raise ExecutorSaturatedError("minute batch still has active provider calls")
        self._idle.clear()
        lock = threading.Lock()
        pending = 1  # Coordinator sentinel: callbacks cannot release admission during submission.
        result: dict[str, Any] = {}
        index = 0
        pool = None
        futures = []

        def finished(_future=None):
            nonlocal pending
            with lock:
                pending -= 1
                if pending == 0:
                    self._gate.release()
                    self._idle.set()

        def worker(source):
            nonlocal index
            while True:
                with lock:
                    if index >= len(symbols) or time.monotonic() >= stop_at:
                        return
                    symbol = symbols[index]
                    index += 1
                try:
                    if not longhu_security_id(symbol):
                        value = "minute_batch_invalid_symbol"
                    else:
                        if source is None:
                            source = self.source_factory()
                        value = current_session_rows(source.stock_minutes(symbol), self.now())
                except ValueError:
                    value = "minute_batch_stale_or_invalid_rows"
                except Exception as error:
                    # Request exceptions may contain upstream credentials in their URL.
                    value = f"minute_batch_fetch_failed:{type(error).__name__}"
                with lock:
                    if time.monotonic() <= stop_at:
                        result[symbol] = value

        try:
            if time.monotonic() >= stop_at:
                return {symbol: DEADLINE_EXCEEDED for symbol in symbols}
            first_source = self.source_factory()
            configured_workers = getattr(getattr(first_source, "config", None), "workers", 4)
            workers = min(12, max(1, int(configured_workers)), len(symbols) or 1)
            pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="longhu-minute-batch")
            for number in range(workers):
                with lock:
                    pending += 1
                try:
                    future = pool.submit(worker, first_source if number == 0 else None)
                except BaseException:
                    finished()
                    raise
                futures.append(future)
                future.add_done_callback(finished)
            wait(futures, timeout=max(0, stop_at - time.monotonic()))
            with lock:
                return {symbol: result.get(symbol, DEADLINE_EXCEEDED) for symbol in symbols}
        finally:
            if pool is not None:
                pool.shutdown(wait=False, cancel_futures=True)
            finished()


minute_batch_service = MinuteBatchService()
