import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from app.strategy_governance.review_evidence import write_packet,load_review_packet,code_excerpt,sanitize,validate_review_packet
from app.strategy_governance.rules import GovernanceError


class ReviewEvidenceTests(unittest.TestCase):
    def test_archive_hash_binding_and_redaction(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);source=root/'rule.py';source.write_text('amount = 100\nratio = amount / 2\n',encoding='utf-8')
            ref=write_packet(issue_key='atomic-A',question='ratio?',cases=[{'candidate':{'amount':100,'token':'secret'}}],
                code=[code_excerpt(source,['ratio'])],facts={},limitations=['fixture'],root=root)
            item={'issue':{'dedupe_key':'atomic-A'},'review_evidence':ref}
            loaded=load_review_packet(item,roots=[root]);self.assertEqual(loaded['status'],'ready')
            self.assertEqual(loaded['packet']['cases'][0]['candidate']['token'],'[REDACTED]')
            self.assertEqual(load_review_packet({**item,'issue':{'dedupe_key':'atomic-B'}},roots=[root])['status'],'invalid')
            Path(ref['path']).write_text('{}',encoding='utf-8')
            self.assertEqual(load_review_packet(item,roots=[root])['status'],'invalid')

    def test_missing_packet_waits_without_loading_or_claiming(self):
        self.assertEqual(load_review_packet({'issue':{'dedupe_key':'x'}})['status'],'missing')
        item={'issue':{'dedupe_key':'x'},'review_evidence':{'status':'missing','issue_key':'x','reason':'no counterexample','path':'does-not-exist'}}
        self.assertEqual(load_review_packet(item)['reason'],'no counterexample')

    def test_paths_only_cannot_be_explicit_evidence(self):
        with self.assertRaises(GovernanceError):validate_review_packet({'path':'somewhere'},{'issue':{'dedupe_key':'x'}})

    def test_sensitive_free_text_scrubbed(self):
        self.assertEqual(sanitize('Bearer secret123'),'[REDACTED]')
        self.assertEqual(sanitize('sra_v1_abcdefghi'),'[REDACTED]')


if __name__=='__main__':unittest.main()
