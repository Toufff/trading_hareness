from __future__ import annotations

import unittest
from datetime import date

from app.post_close_refresh import run_refresh


class PostCloseRefreshTests(unittest.IsolatedAsyncioTestCase):
    async def test_optional_stage_receipt_wrapper_is_used(self):
        seen: list[str] = []

        async def run_db(action, *args, **kwargs):
            result = action(*args)
            return await result if hasattr(result, "__await__") else result

        async def record_stage(name, day, action):
            seen.append(f"{name}:{day}")
            result = action()
            return await result if hasattr(result, "__await__") else result

        result = await run_refresh(
            object(), db=object(), lease_key="lease", lease_seconds=lambda: 60,
            run_database_blocking=run_db,
            acquire_lease=lambda *_: True,
            renew_lease=lambda *_: True,
            release_lease=lambda *_: None,
            actions={"one": lambda: {"status": "completed"}}, stage_order=("one",),
            trade_date=date(2026, 8, 21), safe_error_detail=lambda value, _limit: value,
            json_safe=lambda value: value, record_stage=record_stage,
        )
        self.assertEqual(result["status"], "completed")
        self.assertEqual(seen, ["one:2026-08-21"])


if __name__ == "__main__":
    unittest.main()
