from pathlib import Path
import tempfile
import unittest
from app.strategy_governance.source_archive import archive_sources,verify_source_archive
from app.strategy_governance.rules import GovernanceError


class SourceArchiveTests(unittest.TestCase):
    def test_real_source_deterministic_archive_and_tamper(self):
        with tempfile.TemporaryDirectory() as temp:
            first=archive_sources(output_root=temp);second=archive_sources(output_root=temp)
            self.assertEqual(first,second)
            self.assertEqual(verify_source_archive(first,first['code_hash'],first['runner_hash'])['status'],'verified')
            self.assertLess(first['uncompressed_bytes'],2_000_000)
            Path(first['path']).write_bytes(b'changed')
            with self.assertRaises(GovernanceError):verify_source_archive(first,first['code_hash'],first['runner_hash'])

    def test_legacy_missing_source_is_explicit(self):
        with self.assertRaisesRegex(GovernanceError,'missing_source_archive'):
            verify_source_archive(None,'a'*64,'b'*64)


if __name__=='__main__':unittest.main()
