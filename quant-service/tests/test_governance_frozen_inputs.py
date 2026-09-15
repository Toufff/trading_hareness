import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from app.strategy_governance.frozen_inputs import freeze_input


class FrozenInputTests(unittest.TestCase):
    def payload(self):
        return dict(day='2026-09-11', rows=[{'symbol':'A','trade_date':'2026-09-11','close':10}],
                    sessions=['2026-09-11'], events={}, price_histories={}, history_health={})

    def test_real_atomic_archive_is_content_addressed_and_idempotent(self):
        with tempfile.TemporaryDirectory() as temp:
            a=freeze_input(**self.payload(),root=Path(temp))
            b=freeze_input(**self.payload(),root=Path(temp))
            self.assertEqual(a,b)
            raw=Path(a['input_path']).read_bytes()
            self.assertEqual(hashlib.sha256(raw).hexdigest(),a['input_hash'])
            self.assertEqual(json.loads(raw)['rows'],self.payload()['rows'])
            self.assertEqual(len(list(Path(temp).rglob('*.json'))),1)
            self.assertEqual(len(list(Path(temp).rglob('*.tmp'))),0)

    def test_changed_data_is_not_an_overwrite(self):
        with tempfile.TemporaryDirectory() as temp:
            a=freeze_input(**self.payload(),root=Path(temp))
            changed=self.payload();changed['rows'][0]['close']=11
            b=freeze_input(**changed,root=Path(temp))
            self.assertNotEqual(a['input_hash'],b['input_hash'])
            self.assertEqual(json.loads(Path(a['input_path']).read_bytes())['rows'][0]['close'],10)

    def test_future_or_sensitive_inputs_fail_before_writing(self):
        with tempfile.TemporaryDirectory() as temp:
            for field, value in [('trade_date','2026-09-12'),('api_key','private-fixture')]:
                payload=self.payload();payload['rows'][0][field]=value
                with self.assertRaises(ValueError):freeze_input(**payload,root=Path(temp))
            self.assertFalse(list(Path(temp).rglob('*.json')))

    def test_missing_config_does_not_write_to_development(self):
        with patch.dict('os.environ',{},clear=True):
            result=freeze_input(**self.payload())
        self.assertEqual(result['status'],'unconfigured')

    def test_existing_corrupt_archive_is_not_overwritten(self):
        with tempfile.TemporaryDirectory() as temp:
            a=freeze_input(**self.payload(),root=Path(temp))
            Path(a['input_path']).write_text('corrupt',encoding='utf-8')
            with self.assertRaises(ValueError):freeze_input(**self.payload(),root=Path(temp))
            self.assertEqual(Path(a['input_path']).read_text(),'corrupt')

    def test_cutoff_is_explicit_and_invalid_dates_are_not_silently_accepted(self):
        with tempfile.TemporaryDirectory() as temp:
            payload=self.payload()
            receipt=freeze_input(**payload,root=Path(temp),information_cutoff='2026-09-11T18:20:00+08:00')
            self.assertEqual(json.loads(Path(receipt['input_path']).read_bytes())['information_cutoff'],'2026-09-11T18:20:00+08:00')
            payload['rows'][0]['trade_date']='2026-09-10-not-a-date'
            with self.assertRaises(ValueError):freeze_input(**payload,root=Path(temp))
            payload=self.payload();payload['price_histories']={'A':[{'trade_date':'2026-09-12'}]}
            with self.assertRaises(ValueError):freeze_input(**payload,root=Path(temp))


if __name__=='__main__':unittest.main()
