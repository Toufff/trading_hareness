import importlib.util
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('release_schema_guard', ROOT / 'scripts' / 'check-release-schema.py')
guard = importlib.util.module_from_spec(spec)
spec.loader.exec_module(guard)


class ReleaseSchemaGuardTests(unittest.TestCase):
    def test_candidate_recognizes_applied_revision(self):
        self.assertTrue(guard.recognizes_revision(ROOT, '20260910_0090'))

    def test_unknown_revision_cannot_roll_back(self):
        self.assertFalse(guard.recognizes_revision(ROOT, '20991231_unknown'))

    def test_absent_candidate_cannot_roll_back(self):
        self.assertFalse(guard.recognizes_revision(ROOT / 'nonexistent-runtime', '20260910_0090'))
