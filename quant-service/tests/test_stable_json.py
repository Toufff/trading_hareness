"""Coverage for the two jsonb encoding adapters and where they are required.

``psycopg.types.json.Json`` encodes with the stdlib default, which has no hook
for the types a database row hands back. That single gap stopped the post-close
pipeline twice on 2026-08-27 (a Decimal ``strength``, then a datetime in the
analyst context) and failed 352 intraday monitor passes on 2026-08-28.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
import json
import pathlib
import re
import unittest

from app.stable_json import stable_dumps, stable_json, tolerant_dumps, tolerant_json


APP = pathlib.Path(__file__).resolve().parent.parent / "app"
#: Writes reached by the live intraday scan. A bare ``Json`` here is what the
#: 2026-08-28 failure looked like from the outside: a whole source pass lost to
#: one unencodable field.
SCAN_PATH_MODULES = (
    "intraday_signal_event_persistence.py",
    "intraday_scan_signal_persistence.py",
    "board_rotation_repository.py",
    "board_flow_capture_actions.py",
)


class EncoderContractTests(unittest.TestCase):
    def test_both_adapters_accept_a_decimal(self):
        self.assertIn("0.78", tolerant_dumps({"strength": Decimal("0.78")}))
        self.assertIn("0.78", stable_dumps({"strength": Decimal("0.78")}))

    def test_both_adapters_accept_temporal_types(self):
        payload = {"seen": datetime(2026, 8, 28, 9, 30), "day": date(2026, 8, 28)}
        self.assertIn("2026-08-28", tolerant_dumps(payload))
        self.assertIn("2026-08-28", stable_dumps(payload))

    def test_the_plain_encoder_is_what_fails_on_these(self):
        # The behaviour being worked around, pinned so the reason stays visible.
        with self.assertRaises(TypeError):
            json.dumps({"strength": Decimal("0.78")})

    def test_only_the_stable_form_reorders_keys(self):
        payload = {"b": 1, "a": 2}
        self.assertEqual(tolerant_dumps(payload), '{"b":1,"a":2}')
        self.assertEqual(stable_dumps(payload), '{"a":2,"b":1}')

    def test_the_tolerant_adapter_leaves_bytes_alone_apart_from_the_hook(self):
        # This is the property that makes it safe to retrofit onto a call site
        # whose hashing has not been audited.
        payload = {"z": 1, "a": {"y": 2, "b": 3}}
        self.assertEqual(tolerant_dumps(payload),
                         json.dumps(payload, ensure_ascii=False, separators=(",", ":")))

    def test_chinese_text_is_not_escaped_by_either(self):
        for dumps in (tolerant_dumps, stable_dumps):
            self.assertIn("涨停", dumps({"reason": "涨停"}))

    def test_the_adapters_return_json_wrappers_carrying_their_encoder(self):
        payload = {"strength": Decimal("1.5")}
        self.assertEqual(tolerant_json(payload).dumps(payload), tolerant_dumps(payload))
        self.assertEqual(stable_json(payload).dumps(payload), stable_dumps(payload))


class ScanPathUsesATolerantEncoderTests(unittest.TestCase):
    """A bare ``Json`` on the live scan path is the defect, not a style point.

    Each of these modules writes inside the pass that failed 352 times; one
    unencodable field there costs the whole scan, not one column.
    """

    def _source(self, name: str) -> str:
        return (APP / name).read_text(encoding="utf-8")

    def test_no_scan_path_module_still_calls_the_plain_adapter(self):
        offenders = []
        for name in SCAN_PATH_MODULES:
            source = self._source(name)
            if re.search(r"(?<![A-Za-z_])Json\(", source):
                offenders.append(name)
        self.assertEqual(offenders, [],
                         "these write inside the live scan and must tolerate driver types")

    def test_each_scan_path_module_imports_the_tolerant_adapter(self):
        for name in SCAN_PATH_MODULES:
            with self.subTest(module=name):
                self.assertIn("tolerant_json", self._source(name))

    def test_the_settlement_and_snapshot_writers_use_the_stable_form(self):
        # These two hash what they store, so they need the ordering guarantee
        # rather than only the type hook.
        for name in ("feature_snapshot_repository.py", "recommendation_generation.py"):
            with self.subTest(module=name):
                source = self._source(name)
                self.assertIn("stable_json", source)
                self.assertIsNone(re.search(r"(?<![A-Za-z_])Json\(", source))


if __name__ == "__main__":
    unittest.main()
