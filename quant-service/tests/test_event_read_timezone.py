from __future__ import annotations

import unittest
from pathlib import Path


class LhbEventDateFilterUsesShanghaiTimeZoneTests(unittest.TestCase):
    """occurred_at::date without AT TIME ZONE depends on the unset PG session
    TimeZone; a naive cast could silently compare against UTC calendar dates
    instead of the Shanghai trading date the caller actually means."""

    def test_async_event_read_repository_converts_to_shanghai_before_casting(self) -> None:
        source = Path("app/async_event_read_repository.py").read_text(encoding="utf-8")
        self.assertNotIn("OR occurred_at::date=%s", source)
        self.assertIn("(occurred_at AT TIME ZONE 'Asia/Shanghai')::date=%s", source)

    def test_event_read_model_converts_to_shanghai_before_casting(self) -> None:
        source = Path("app/event_read_model.py").read_text(encoding="utf-8")
        self.assertNotIn("OR occurred_at::date=%s", source)
        self.assertIn("(occurred_at AT TIME ZONE 'Asia/Shanghai')::date=%s", source)


if __name__ == "__main__":
    unittest.main()
