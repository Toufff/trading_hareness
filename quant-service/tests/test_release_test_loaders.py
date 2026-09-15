from pathlib import Path
import unittest


class ReleaseTestLoaders(unittest.TestCase):
    def test_release_gate_runs_pytest_once_for_both_python_styles_and_frontend_unit_tests(self):
        source=(Path(__file__).resolve().parents[2]/'scripts/windows/publish-stock-release.ps1').read_text(encoding='utf-8')
        self.assertNotIn("@('-m', 'unittest', 'discover', '-s', 'tests', '-q')",source)
        self.assertIn("@('-m', 'pytest', 'tests', '-q', '--disable-warnings')",source)
        self.assertEqual(source.count("@('-m', 'pytest', 'tests', '-q', '--disable-warnings')"), 1)
        self.assertIn("-Arguments @('run', 'test')",source)


if __name__=='__main__':unittest.main()
