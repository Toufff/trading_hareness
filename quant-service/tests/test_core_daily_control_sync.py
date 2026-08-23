from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import date
import unittest

from fastapi import HTTPException

from app.core_daily_control_sync import CoreDailyControlDependencies, sync


@dataclass
class _Request:
    api_name: str
    params: dict[str, str]
    max_rows: int


class CoreDailyControlSyncTests(unittest.TestCase):
    def test_no_resolved_equity_keeps_the_compatibility_path_disabled(self) -> None:
        async def resolve(_symbols: list[str]) -> list[str]:
            return ["000300.SH"]

        async def fetch(_request: _Request) -> dict[str, object]:
            raise AssertionError("disabled sync must not call a provider")

        result = asyncio.run(sync(date(2026, 8, 21), None, CoreDailyControlDependencies(resolve, fetch, _Request)))

        self.assertEqual(result, {"status": "disabled", "reason": "no explicit equity universe", "requests": []})

    def test_refreshes_only_the_current_date_controls_and_reports_partial_failures(self) -> None:
        calls: list[_Request] = []

        async def resolve(symbols: list[str]) -> list[str]:
            self.assertEqual(symbols, ["000001.SZ", "000300.SH"])
            return symbols

        async def fetch(request: _Request) -> dict[str, object]:
            calls.append(request)
            if request.api_name == "stk_limit":
                raise HTTPException(status_code=502, detail="upstream")
            return {"api_name": request.api_name}

        result = asyncio.run(sync(
            date(2026, 8, 21), ["000001.SZ", "000300.SH"],
            CoreDailyControlDependencies(resolve, fetch, _Request),
        ))

        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["symbols"], ["000001.SZ"])
        self.assertEqual(result["failures"], ["stk_limit"])
        self.assertEqual([request.api_name for request in calls], [
            "trade_cal", "daily_basic", "adj_factor", "stk_limit", "suspend_d",
        ])
        self.assertEqual(calls[1].params["start_date"], "20260821")
        self.assertEqual(calls[0].params["start_date"], "20260101")
