import json
import os
from pathlib import Path
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from app.broker_fact_evidence import load_trace, validate_recovery


class BrokerTraceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        # Pinned instead of `datetime.now(timezone.utc)`: the rules below compare
        # Shanghai calendar dates and bind the observation to the screenshots'
        # mtimes, so a wall-clock base makes this suite fail whenever it runs
        # between 23:40 and 24:00 local -- an offset of +20 minutes then lands on
        # the next Shanghai day, which the deliberate same-day recovery rule
        # rejects.  Mid-morning Shanghai keeps every offset used here inside one
        # day.  The capture files are stamped with the same instant so the
        # observation-to-screenshot binding still holds.
        self.now = datetime(2026, 9, 16, 2, 0, tzinfo=timezone.utc)  # 10:00 Asia/Shanghai
        self.run = {'run_id': 'new-run', 'started_at': self.now - timedelta(seconds=30), 'input_summary': {'phase': 'manual'}}
        image = self.root / 'fresh.png'
        image.write_bytes(b'\x89PNG\r\n\x1a\n' + b'test')
        stamp = self.now.timestamp()
        os.utime(image, (stamp, stamp))
        Path(str(image) + '.capture.json').write_text('{"status":"success"}')
        self.trace = {'schema_version': 'citics-ai-ui-trace-v1', 'controller_model': 'gpt-5.6-luna', 'run_id': 'new-run',
            'phase': 'manual', 'completed_at': self.now.isoformat(), 'final_screenshot': str(image),
            'navigation_proof': {'action': 'holdings_verified', 'source_state': 'asset_panorama', 'fresh_navigation_verified': True},
            'transitions': [{'page_before': 'asset_panorama', 'action': 'tap_stock', 'page_after': 'detailed_holdings',
                'before_screenshot': str(image), 'after_screenshot': str(image), 'verified_by_model': True}]}

    def load(self):
        path = self.root / 'trace.json'
        path.write_text(json.dumps(self.trace))
        return load_trace(path, self.run, self.now)

    def test_valid(self): self.assertEqual(self.load()[1], self.now)
    def test_wrong_run(self):
        self.trace['run_id'] = 'old-run'
        with self.assertRaisesRegex(ValueError, 'MISMATCH'): self.load()
    def test_cached_page(self):
        self.trace['navigation_proof']['source_state'] = 'detailed_holdings'
        with self.assertRaisesRegex(ValueError, 'REENTERED'): self.load()
    def test_old_capture(self):
        p = Path(self.trace['final_screenshot'])
        stale = (self.now - timedelta(days=1)).timestamp()
        os.utime(p, (stale, stale))
        with self.assertRaisesRegex(ValueError, 'OLD_SCREENSHOT'): self.load()
    def test_future_observation(self):
        self.trace['completed_at'] = (self.now + timedelta(minutes=10)).isoformat()
        with self.assertRaisesRegex(ValueError, 'TIME_INVALID'): self.load()
    def test_ui_deadline(self):
        self.run['started_at'] = self.now - timedelta(minutes=6)
        with self.assertRaisesRegex(ValueError, 'DEADLINE'): self.load()

    def test_recovery_requires_manual_and_failed_same_day(self):
        self.run.update(status='failed', finished_at=self.now)
        with self.assertRaisesRegex(ValueError, 'AUTHORIZATION'): validate_recovery(self.run, self.now, False)
        validate_recovery(self.run, self.now, True)
        with self.assertRaisesRegex(ValueError, 'DIFFERENT_DAY'): validate_recovery(self.run, self.now + timedelta(days=1), True)
        self.run['status'] = 'running'
        with self.assertRaisesRegex(ValueError, 'FAILED_CAPTURE'): validate_recovery(self.run, self.now, True)

    def test_static_recovery_preserves_original_observation(self):
        self.run.update(status='failed', finished_at=self.now + timedelta(minutes=2))
        path = self.root / 'trace.json'
        path.write_text(json.dumps(self.trace))
        later = self.now + timedelta(minutes=20)
        with self.assertRaisesRegex(ValueError, 'TIME_INVALID'): load_trace(path, self.run, later)
        _, observed, _ = load_trace(path, self.run, later, recovery=True)
        self.assertEqual(observed, self.now)
        self.assertEqual(self.run['status'], 'failed')

    def test_recovery_cannot_move_ui_completion(self):
        self.run.update(status='failed', finished_at=self.now + timedelta(minutes=10))
        self.trace['completed_at'] = (self.now + timedelta(minutes=8)).isoformat()
        path = self.root / 'trace.json'
        path.write_text(json.dumps(self.trace))
        with self.assertRaisesRegex(ValueError, 'DEADLINE'): load_trace(path, self.run, self.now + timedelta(minutes=20), recovery=True)


if __name__ == '__main__': unittest.main()
