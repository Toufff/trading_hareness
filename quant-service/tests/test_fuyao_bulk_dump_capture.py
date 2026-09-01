from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import unittest

from app.fuyao_bulk_dump_capture import DumpDescriptor, descriptor_from_envelope, manifest_for_file


class FuyaoBulkDumpCaptureTests(unittest.TestCase):
    def test_descriptor_accepts_short_lived_url_without_exposing_it_in_manifest(self) -> None:
        descriptor = descriptor_from_envelope(
            "a_share_daily_k_10y_dump",
            {"request_id": "req-1", "data": {
                "presigned_url": "https://example.invalid/private?signature=secret",
                "presigned_url_expires_at": "2026-09-01T00:00:00+08:00", "expires_in_seconds": 600,
            }},
        )
        manifest = manifest_for_file(
            descriptor, Path("a_share_daily_k_10y_dump.parquet"), size=12, sha256="abc",
            retrieved_at=datetime(2026, 8, 31, tzinfo=timezone.utc),
        )
        self.assertEqual(manifest["projection_status"], "captured_unprojected")
        self.assertNotIn("signature=secret", str(manifest))

    def test_descriptor_rejects_missing_url_and_unknown_dump(self) -> None:
        with self.assertRaisesRegex(ValueError, "no valid presigned_url"):
            descriptor_from_envelope("a_share_daily_k_10y_dump", {"data": {}})
        with self.assertRaisesRegex(ValueError, "unsupported"):
            descriptor_from_envelope("unknown_dump", {"data": {"presigned_url": "https://example.invalid"}})


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
