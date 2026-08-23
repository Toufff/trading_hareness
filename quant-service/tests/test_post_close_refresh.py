from __future__ import annotations

import unittest
from datetime import date

from app.post_close_refresh import record_stage_with_receipt, run_refresh


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

    async def test_core_daily_controls_is_a_real_post_close_stage(self):
        calls: list[str] = []

        async def run_db(action, *args, **_kwargs):
            return action(*args)

        result = await run_refresh(
            object(), db=object(), lease_key="lease", lease_seconds=lambda: 60,
            run_database_blocking=run_db, acquire_lease=lambda *_: True,
            renew_lease=lambda *_: True, release_lease=lambda *_: None,
            actions={"core_daily_controls": lambda: calls.append("controls") or {"status": "completed"}},
            stage_order=("core_daily_controls",), trade_date=date(2026, 8, 21),
            safe_error_detail=lambda value, _limit: value, json_safe=lambda value: value,
        )
        self.assertEqual(calls, ["controls"])
        self.assertEqual(result["stages"]["core_daily_controls"]["status"], "completed")

    async def test_completed_stage_receipt_skips_action_after_restart(self):
        class Result:
            def fetchone(self):
                return {"run_id": "receipt-1", "status": "completed", "output_summary": {"status": "completed"}}

        class Connection:
            def execute(self, *_args, **_kwargs):
                return Result()

        class Database:
            def transaction(self):
                class Context:
                    def __enter__(self): return Connection()
                    def __exit__(self, *_args): return False
                return Context()

        async def run_db(action, *args, **_kwargs):
            result = action(*args)
            return await result if hasattr(result, "__await__") else result

        called = False

        def action():
            nonlocal called
            called = True
            return {"status": "completed"}

        result = await record_stage_with_receipt(
            "daily", date(2026, 8, 21), action, db=Database(),
            run_database_blocking=run_db, safe_error_detail=lambda value, _limit: value,
        )
        self.assertFalse(called)
        self.assertTrue(result["resumed_from_receipt"])


if __name__ == "__main__":
    unittest.main()
