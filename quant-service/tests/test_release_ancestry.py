import importlib.util
import json
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('release_ancestry', ROOT / 'scripts/check-release-ancestry.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class ReleaseAncestryTests(unittest.TestCase):
    def test_real_git_history_refuses_unmerged_deployed_work(self):
        with tempfile.TemporaryDirectory(prefix='stock-release-ancestry-') as directory:
            root = Path(directory)
            source, platform = root / 'repo', root / 'production'
            source.mkdir(); (platform / 'current').mkdir(parents=True)
            def git(*args):
                return subprocess.check_output(['git', '-C', str(source), *args], text=True).strip()
            git('init', '-q'); git('config', 'user.name', 'Release Test'); git('config', 'user.email', 'test@example.invalid')
            git('commit', '--allow-empty', '-qm', 'base'); base = git('rev-parse', 'HEAD')
            git('checkout', '-qb', 'deployed'); git('commit', '--allow-empty', '-qm', 'deployed work'); deployed=git('rev-parse', 'HEAD')
            git('checkout', '-qb', 'candidate', base); git('commit', '--allow-empty', '-qm', 'candidate work'); candidate=git('rev-parse', 'HEAD')
            (platform / 'release-state.json').write_text(json.dumps({'active_release':'live'}), encoding='utf-8')
            (platform / 'current/release-manifest.json').write_text(json.dumps({'release_id':'live','git_head':deployed,'dirty':False}), encoding='utf-8')
            with self.assertRaisesRegex(ValueError, 'does not contain'):
                module.verify(source,platform,candidate)
            git('merge', '--no-ff', '-m', 'integrate', 'deployed'); merged=git('rev-parse','HEAD')
            self.assertEqual(module.verify(source,platform,merged)['status'],'preserves_active_commit')
            with self.assertRaisesRegex(ValueError,'HEAD changed'):
                module.verify(source,platform,candidate)
            (platform / 'current/release-manifest.json').write_text(json.dumps({'release_id':'wrong','git_head':deployed,'dirty':False}), encoding='utf-8')
            with self.assertRaisesRegex(ValueError,'inconsistent'):
                module.verify(source,platform,merged)

    def test_publish_checks_before_tests_and_before_snapshot(self):
        source=(ROOT / 'scripts/windows/publish-stock-release.ps1').read_text(encoding='utf-8')
        checks=[i for i,line in enumerate(source.splitlines()) if line=='Assert-SourcePreservesActiveRelease']
        self.assertEqual(len(checks),2)
        self.assertLess(checks[0],source.splitlines().index('if (-not $SkipTests) {'))
        self.assertLess(checks[1],source.splitlines().index('$previousState = Get-StockReleaseState -PlatformRoot $platform'))
