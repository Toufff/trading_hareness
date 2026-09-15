"""Opt-in real PostgreSQL acceptance, all writes rolled back in one outer transaction.

Set RUN_GOVERNANCE_DB_TESTS=1 after applying migration 0092. Uses normal DB env.
No permanent experiments or approvals are created; no live config is exposed.
"""
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4
from app.strategy_governance import repository as repo
from app.strategy_governance.rules import digest, GovernanceError
from app.strategy_governance.configuration import code_fingerprint, environment_config
from tests.test_strategy_governance import actor, spec, evidence


class EndRollback(Exception): pass


@unittest.skipUnless(os.getenv('RUN_GOVERNANCE_DB_TESTS')=='1','opt-in real DB rollback test')
class GovernanceDatabaseTests(unittest.TestCase):
    def test_real_persistence_state_machine_activation_and_rollback(self):
        from app.database import Database
        db=Database()
        fixture_baseline = {'ranking_factors':{'version':1,'factors':[{'key':'small_cap','weight':.12,'strategies':['accumulation']}]}}
        with tempfile.TemporaryDirectory() as temp, patch('app.strategy_governance.repository.environment_config', return_value=fixture_baseline):
            with self.assertRaises(EndRollback):
                with db.transaction() as connection:
                    class Isolated:
                        @contextmanager
                        def transaction(self):
                            with connection.transaction(): yield connection
                    isolated=Isolated()
                    existing=repo.resolve_active_config(isolated)
                    generation=existing['generation'] if existing else 0
                    payload=dict(title='acceptance only',problem='fixture',hypothesis='test',scope='fixture',out_of_scope='production',
                        dedupe_key='acceptance:'+str(uuid4()),evidence=[{'source':'test_fixture'}])
                    item=repo.create_issue(isolated,payload,actor('observer'))
                    self.assertEqual(item['id'],repo.create_issue(isolated,payload,actor('observer'))['id'])
                    with self.assertRaises(GovernanceError):repo.transition(isolated,item['id'],0,'review',{},actor('reviewer'))
                    item=repo.transition(isolated,item['id'],1,'review',dict(reproduction='yes',counterexample='yes',alternative_explanation='yes',verdict='confirmed'),actor('reviewer'))
                    frozen=spec();frozen['baseline_generation']=generation
                    frozen['baseline_code_hash']=code_fingerprint()
                    baseline_config=existing['config'] if existing else fixture_baseline
                    frozen['baseline_config_hash']=digest(baseline_config)
                    item=repo.transition(isolated,item['id'],item['revision'],'design',dict(change='one',tradeoffs='none',failure_condition='test',rollback='previous',spec=frozen),actor('designer'))
                    item=repo.transition(isolated,item['id'],item['revision'],'experiment',dict(artifact='fixture',artifact_hash=digest(frozen)),actor('implementer'))
                    measured=evidence();measured['baseline_code_hash']=frozen['baseline_code_hash'];path=Path(temp)/'measurement.json'
                    path.write_text(json.dumps(measured),encoding='utf-8')
                    measured['measurement_artifact']=str(path);measured['measurement_hash']=hashlib.sha256(path.read_bytes()).hexdigest()
                    item=repo.transition(isolated,item['id'],item['revision'],'observe',measured,actor('evaluator'))
                    validation=dict(independent_reproduction='yes',leakage_review='yes',adverse_cases='yes',limitations='fixture only',measurement_hash=measured['measurement_hash'])
                    item=repo.transition(isolated,item['id'],item['revision'],'validate',validation,actor('validator'))
                    item=repo.transition(isolated,item['id'],item['revision'],'ready',dict(summary='test',risk='none',rollback='previous'),actor('release_preparer'))
                    self.assertEqual(repo.resolve_active_config(isolated),existing)
                    with patch('app.strategy_governance.repository.code_fingerprint',return_value='f'*64):
                        with self.assertRaises(GovernanceError):repo.activate(isolated,item['id'],item['revision'],digest(frozen),actor('human'))
                    active=repo.activate(isolated,item['id'],item['revision'],digest(frozen),actor('human'))
                    self.assertEqual(active['state'],'activated')
                    promoted=repo.resolve_active_config(isolated)
                    rolled=repo.rollback(isolated,generation,promoted['generation'],actor('human'),'acceptance rollback')
                    self.assertGreater(rolled['generation'],promoted['generation'])
                    self.assertEqual(rolled['config'],baseline_config)
                    audit=connection.execute('SELECT count(*) AS n FROM quant.strategy_governance_events WHERE item_id=%s',(item['id'],)).fetchone()['n']
                    self.assertEqual(audit,8)
                    raise EndRollback()
        self.assertEqual(repo.resolve_active_config(db),existing)


if __name__=='__main__':unittest.main()
