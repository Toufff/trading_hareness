"""One-shot Super GET quote capture with injectable persistence boundaries."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable


AsyncCall = Callable[..., Awaitable[Any]]


async def capture(
    symbol: str,
    *,
    call_provider: AsyncCall,
    run_database: AsyncCall,
    persist_quote: Callable[..., None],
    persist_failure: Callable[..., None],
    number: Callable[[Any], float | None],
    safe_error: Callable[[str, int], str],
    is_circuit_open: Callable[[Exception], bool],
    now_utc: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
) -> dict[str, Any]:
    """Capture a lightweight ``rt_k`` sample without a fetch-run ledger.

    A provider circuit-open response is an intentional local protection state,
    not an upstream failure.  It therefore avoids both persistence and health
    failure writes, leaving the existing circuit expiry able to recover.
    """
    observed_at = now_utc()
    started_at = asyncio.get_running_loop().time()
    try:
        result = await call_provider("rt_k", {"ts_code": symbol}, None, "super_get")
        rows = result.rows
        row = next((item for item in rows if str(item.get("ts_code") or "").upper() == symbol), rows[0] if rows else None)
        if row is None:
            return {"status": "empty", "symbol": symbol, "observed_at": observed_at.isoformat()}
        price = number(row.get("close"))
        previous_close = number(row.get("pre_close"))
        if price is None or price <= 0:
            raise ValueError("rt_k returned no valid positive close")
        pct_change = ((price / previous_close) - 1) * 100 if previous_close and previous_close > 0 else None
        latency_ms = round((asyncio.get_running_loop().time() - started_at) * 1000)
        await run_database(
            persist_quote, symbol, observed_at, price, pct_change, row, result.provider.key, latency_ms,
        )
        return {"status": "completed", "symbol": symbol, "observed_at": observed_at.isoformat(), "price": price}
    except Exception as error:  # noqa: BLE001 - the next one-second slot remains useful
        detail = safe_error(str(error), 300)
        if is_circuit_open(error):
            return {"status": "circuit_open", "symbol": symbol, "observed_at": observed_at.isoformat(), "error": detail}
        latency_ms = round((asyncio.get_running_loop().time() - started_at) * 1000)
        await run_database(persist_failure, detail, latency_ms)
        return {"status": "failed", "symbol": symbol, "observed_at": observed_at.isoformat(), "error": detail}


__all__ = ["capture"]
