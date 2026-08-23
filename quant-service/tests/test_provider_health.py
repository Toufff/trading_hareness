from __future__ import annotations

import unittest

from app.provider_health import record_provider_api_capability


class ProviderCapabilityEvidenceTests(unittest.TestCase):
    def test_later_failure_preserves_verified_note_and_records_latest_observation(self):
        class Connection:
            def __init__(self):
                self.statement = ""
                self.params = ()

            def execute(self, statement, params):
                self.statement, self.params = statement, params

        connection = Connection()
        record_provider_api_capability(
            connection, "tushare_super_get", "daily", "failed", note="ConnectionError: upstream closed",
        )
        self.assertIn("quant.provider_api_capabilities.availability='verified'", connection.statement)
        self.assertIn("EXCLUDED.availability IN ('failed','empty')", connection.statement)
        metadata = connection.params[-1].obj
        self.assertEqual(metadata["last_observation"], "failed")
        self.assertEqual(metadata["last_observation_note"], "ConnectionError: upstream closed")

    def test_verified_observation_keeps_its_evidence_note(self):
        class Connection:
            def __init__(self): self.params = ()
            def execute(self, _statement, params): self.params = params

        connection = Connection()
        record_provider_api_capability(
            connection, "tushare_super_get", "daily", "verified", 12, "verified rows",
        )
        metadata = connection.params[-1].obj
        self.assertEqual(metadata["verified_note"], "verified rows")
        self.assertEqual(metadata["last_row_count"], 12)


if __name__ == "__main__":
    unittest.main()
