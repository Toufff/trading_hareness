from __future__ import annotations

import asyncio
import uuid
import unittest

from app.main import analyse_ingestion, analyse_ingestion_endpoint


class LegacyAnalysisIngestionTests(unittest.TestCase):
    def test_legacy_analysis_endpoint_is_a_remote_archive_only_noop(self) -> None:
        analysis_id = uuid.uuid4()

        direct = analyse_ingestion(analysis_id)
        endpoint = asyncio.run(analyse_ingestion_endpoint(analysis_id))

        self.assertEqual(direct, endpoint)
        self.assertEqual(direct["status"], "ignored")
        self.assertEqual(direct["analysis_id"], str(analysis_id))
        self.assertEqual(direct["reason"], "remote_archive_is_the_only_analyst_source")
