from __future__ import annotations

import asyncio
import unittest
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any

from app.baostock_daily_sync import sync


@dataclass
class _CapturedBar:
    symbol: str
    trading_date: date
    open: Any = None
    high: Any = None
    low: Any = None
    close: Any = None
    pre_close: Any = None
    volume: Any = None
    amount: Any = None
    is_st: Any = None
    source: str = "baostock"


def _decimal_or_none(value: Any) -> Decimal | None:
    return None if value in (None, "") else Decimal(str(value))


class _Request:
    symbols = ["000001.SZ"]
    trade_date = date(2026, 8, 10)


class _Transaction:
    def __enter__(self) -> "_Transaction":
        return self

    def __exit__(self, *args: Any) -> None:
        return None

    def execute(self, *_args: Any, **_kwargs: Any) -> "_Transaction":
        return self

    def fetchone(self) -> None:
        return None


class _Db:
    def transaction(self) -> _Transaction:
        return _Transaction()


class BaostockUnitConversionTests(unittest.TestCase):
    def test_shares_and_yuan_are_converted_to_lots_and_thousand_yuan(self) -> None:
        # BaoStock reports volume in raw shares and amount in yuan; the
        # canonical daily contract (matching Tushare) is lots (100 shares)
        # and thousand yuan.  A row with 100,000 shares / 1,250,000 yuan must
        # be stored as 1,000 lots / 1,250 thousand-yuan, not the raw values.
        raw_rows = [{
            "code": "sz.000001", "date": "2026-08-10", "open": "10", "high": "11", "low": "9",
            "close": "10.5", "preclose": "10", "volume": "100000", "amount": "1250000", "isST": "0",
        }]
        captured: list[Any] = []

        async def fetch_rows(_symbols: Any, _trade_date: Any, **_kwargs: Any) -> tuple[list[dict[str, str]], list[str]]:
            return raw_rows, []

        async def run_public_blocking(func: Any, *args: Any, **kwargs: Any) -> Any:
            return await func(*args, **{key: value for key, value in kwargs.items() if key != "timeout_seconds"})

        async def run_database_blocking(func: Any, *args: Any, **kwargs: Any) -> Any:
            return func(*args, **{key: value for key, value in kwargs.items() if key != "timeout_seconds"})

        def persist_daily_bar_batch(bars: list[Any]) -> int:
            captured.extend(bars)
            return len(bars)

        result = asyncio.run(sync(
            _Request(),
            resolve_symbols=lambda symbols: asyncio.sleep(0, result=list(symbols)),
            cn_today=lambda: date(2026, 8, 10),
            open_provider_capabilities=lambda *_a, **_k: asyncio.sleep(0, result=set()),
            run_database_blocking=run_database_blocking,
            run_public_blocking=run_public_blocking,
            fetch_rows=fetch_rows,
            baostock_code=lambda symbol: f"{symbol[-2:].lower()}.{symbol[:6]}",
            daily_bar_type=_CapturedBar,
            decimal_or_none=_decimal_or_none,
            persist_daily_bar_batch=persist_daily_bar_batch,
            db=_Db(),
            safe_error_detail=lambda message, limit: message[:limit],
            record_provider_failure=lambda *_a, **_k: None,
            record_provider_success=lambda *_a, **_k: None,
            executor_saturated_error=RuntimeError,
        ))

        self.assertEqual(result["status"], "completed")
        self.assertEqual(len(captured), 1)
        self.assertEqual(captured[0].volume, Decimal("1000"))
        self.assertEqual(captured[0].amount, Decimal("1250"))


if __name__ == "__main__":
    unittest.main()
