from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import asyncio
import threading
import time
from types import SimpleNamespace

import pytest

from app.longhu_minute_batch import MinuteBatchService, current_session_rows
from app.runtime_executors import ExecutorSaturatedError


NOW = datetime(2026, 9, 22, 2, 0, tzinfo=timezone.utc)


def rows(day="2026-09-22"):
    return [{"session_date": day, "time": "0931", "close": 10, "amount": 123.45,
             "is_complete": False}]


def test_session_guard_preserves_owner_semantics_and_adds_peer_date_fields():
    original = rows()
    result = current_session_rows(original, NOW)
    assert result[0]["trade_date"] == "20260922"
    assert result[0]["trade_time"] == "2026-09-22 09:31:00"
    assert result[0]["amount"] == 123.45
    assert result[0]["is_complete"] is False
    assert "trade_date" not in original[0]


@pytest.mark.parametrize("value", [[], rows("2026-09-21"), rows(None), rows()+rows("2026-09-21")])
def test_session_guard_rejects_empty_missing_mixed_or_old_dates(value):
    with pytest.raises(ValueError):
        current_session_rows(value, NOW)


def test_session_guard_uses_shanghai_date_at_midnight():
    with pytest.raises(ValueError):
        current_session_rows(rows("2026-09-21"), datetime(2026, 9, 21, 16, 0, tzinfo=timezone.utc))


async def blocking(action, *args, timeout_seconds):
    return await asyncio.to_thread(action, *args)


def service(fetch, **kwargs):
    return MinuteBatchService(
        source_factory=lambda: SimpleNamespace(stock_minutes=fetch, config=SimpleNamespace(workers=2)),
        blocking=blocking, now=lambda: NOW, **kwargs,
    )


def test_one_symbol_failure_is_redacted_without_losing_other_rows():
    def fetch(symbol):
        if symbol == "000002.SZ":
            raise RuntimeError("https://vendor/?Token=SECRET")
        return rows()
    result = asyncio.run(service(fetch).read(["000001.SZ", "000002.SZ", "600519.SH"], 1))
    assert isinstance(result["000001.SZ"], list)
    assert result["000002.SZ"] == "minute_batch_fetch_failed:RuntimeError"
    assert "SECRET" not in str(result)


def test_batch_uses_one_outer_executor_submission():
    calls = []
    async def record(action, *args, timeout_seconds):
        calls.append(timeout_seconds)
        return await blocking(action, *args, timeout_seconds=timeout_seconds)
    instance = service(lambda _: rows())
    instance.blocking = record
    assert len(asyncio.run(instance.read(["000001.SZ", "600519.SH"], 1))) == 2
    assert calls == [3.5]


def test_deadline_does_not_release_capacity_while_provider_threads_still_run():
    started, release = threading.Event(), threading.Event()
    calls = []
    def fetch(symbol):
        calls.append(symbol)
        started.set()
        release.wait(3)
        return rows()
    instance = service(fetch)
    try:
        begin = time.monotonic()
        result = asyncio.run(instance.read(["000001.SZ", "000002.SZ", "600519.SH"], .05))
        assert time.monotonic() - begin < 1
        assert started.is_set()
        assert set(result.values()) == {"minute_batch_deadline_exceeded"}
        with pytest.raises(ExecutorSaturatedError):
            asyncio.run(instance.read(["600519.SH"], .05))
        assert len(calls) == 2
    finally:
        release.set()
    assert instance.wait_idle(2)
    assert len(calls) == 2  # no queued stock starts after the deadline
    assert isinstance(asyncio.run(instance.read(["600519.SH"], 1))["600519.SH"], list)


def test_outer_executor_refusal_releases_batch_capacity():
    instance = service(lambda _: rows())
    async def refused(*args, **kwargs):
        raise ExecutorSaturatedError("busy")
    instance.blocking = refused
    with pytest.raises(ExecutorSaturatedError):
        asyncio.run(instance.read(["600519.SH"], 1))
    instance.blocking = blocking
    assert isinstance(asyncio.run(instance.read(["600519.SH"], 1))["600519.SH"], list)


def test_global_batch_admission_prevents_parallel_fanouts():
    started, release = threading.Event(), threading.Event()
    def fetch(_):
        started.set()
        release.wait(3)
        return rows()
    instance = service(fetch)
    with ThreadPoolExecutor(2) as pool:
        first = pool.submit(asyncio.run, instance.read(["600519.SH"], 2))
        assert started.wait(1)
        try:
            with pytest.raises(ExecutorSaturatedError):
                asyncio.run(instance.read(["000001.SZ"], 1))
        finally:
            release.set()
        assert isinstance(first.result()["600519.SH"], list)


def test_caller_cancellation_does_not_free_running_physical_calls():
    started, release = threading.Event(), threading.Event()
    def fetch(_):
        started.set()
        release.wait(3)
        return rows()
    instance = service(fetch)
    async def scenario():
        task = asyncio.create_task(instance.read(["600519.SH"], 1))
        assert await asyncio.to_thread(started.wait, 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        try:
            with pytest.raises(ExecutorSaturatedError):
                await instance.read(["000001.SZ"], 1)
        finally:
            release.set()
        assert await asyncio.to_thread(instance.wait_idle, 2)
    asyncio.run(scenario())


def test_source_initialization_failure_does_not_leave_admission_locked():
    instance = service(lambda _: rows())
    healthy_factory = instance.source_factory
    def failed():
        raise RuntimeError("provider unavailable")
    instance.source_factory = failed
    with pytest.raises(RuntimeError):
        asyncio.run(instance.read(["600519.SH"], 1))
    assert instance.wait_idle(.1)
    instance.source_factory = healthy_factory
    assert isinstance(asyncio.run(instance.read(["600519.SH"], 1))["600519.SH"], list)
