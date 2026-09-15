"""Executable negative cases for atomic, independently reviewed strategy changes."""
import unittest
from copy import deepcopy
from datetime import datetime, timezone, timedelta
from app.strategy_governance.rules import new_issue, advance, digest, compare, approval_config, GovernanceError


def actor(role, name=None): return {'id':name or role, 'roles':[role]}


def issue():
    return new_issue(dict(title='close quality',problem='wick blindness',hypothesis='close location helps',
        scope='expansion',out_of_scope='all other strategies',dedupe_key='wick-v1',evidence=[{'source':'fixture'}]),actor('observer'))


def spec():
    return dict(input_hash='a'*64,baseline_code_hash='b'*64,candidate_code_hash='c'*64,baseline_config_hash='e'*64,
        holdout_id='frozen-future',data_start='2026-09-01',data_end='2026-09-30',baseline_generation=0,
        allowed_strategy_keys=['accumulation'],minimum_sessions=2,minimum_observations=3,criteria=[{'metric':'false_positive_rate','operator':'<=','threshold':.2}],
        candidate_config={'ranking_factors':{'version':1,'factors':[]}})


def evidence():
    s=spec()
    return {**{k:s[k] for k in ('input_hash','baseline_code_hash','candidate_code_hash','holdout_id')},
        'measurement_artifact':'fixture.json','measurement_hash':'d'*64,'method':'counterexample replay',
        'limitations':'engineering only','sessions':['2026-09-01','2026-09-02'],'observations':4,
        'metrics':{'false_positive_rate':.1}}


def step(s, action, payload, role): return advance(s,s['revision'],action,payload,actor(role))


def designed():
    s=step(issue(),'review',dict(reproduction='repeat',counterexample='weak',alternative_explanation='data',verdict='confirmed'),'reviewer')
    return step(s,'design',dict(change='one',tradeoffs='missed',failure_condition='worse',rollback='old',spec=spec()),'designer')


def ready():
    s=step(designed(),'experiment',dict(artifact='fixture',artifact_hash=digest(spec())),'implementer')
    s=step(s,'observe',evidence(),'evaluator')
    s=step(s,'validate',dict(independent_reproduction='repeat',leakage_review='frozen',adverse_cases='checked',
        limitations='not profitability',measurement_hash='d'*64),'validator')
    return step(s,'ready',dict(summary='review me',risk='sample',rollback='old'),'release_preparer')


class GovernanceRulesTests(unittest.TestCase):
    def test_cannot_skip_or_self_review(self):
        with self.assertRaises(GovernanceError): advance(issue(),1,'ready',{},actor('release_preparer'))
        with self.assertRaises(GovernanceError): advance(issue(),1,'review',{},actor('reviewer','observer'))

    def test_optimistic_revision(self):
        with self.assertRaises(GovernanceError): advance(issue(),0,'review',{},actor('reviewer'))

    def test_frozen_spec_hash(self):
        with self.assertRaises(GovernanceError): step(designed(),'experiment',dict(artifact='x',artifact_hash='x'),'implementer')

    def test_different_input_cannot_compare(self):
        e=evidence(); e['input_hash']='f'*64
        with self.assertRaises(GovernanceError): compare(spec(),e)

    def test_passed_flag_not_enough(self):
        e=evidence(); e['metrics']={};e['passed']=True
        self.assertEqual(compare(spec(),e)['status'],'failed')

    def test_dates_are_independent(self):
        e=evidence();e['sessions']=['2026-09-01']*20
        self.assertEqual(compare(spec(),e)['status'],'insufficient')

    def test_actor_separation(self):
        s=step(designed(),'experiment',dict(artifact='fixture',artifact_hash=digest(spec())),'implementer')
        with self.assertRaises(GovernanceError): advance(s,s['revision'],'observe',evidence(),actor('evaluator','implementer'))
        s=step(s,'observe',evidence(),'evaluator')
        with self.assertRaises(GovernanceError): advance(s,s['revision'],'validate',{},actor('validator','designer'))

    def test_human_hash_expiry_and_baseline_binding(self):
        s=ready(); h=actor('human')
        self.assertEqual(approval_config(s,s['revision'],digest(spec()),0,h),spec()['candidate_config'])
        for revision,hash_,generation in [(s['revision']-1,digest(spec()),0),(s['revision'],'bad',0),(s['revision'],digest(spec()),1)]:
            with self.assertRaises(GovernanceError): approval_config(s,revision,hash_,generation,h)
        with self.assertRaises(GovernanceError): approval_config(s,s['revision'],digest(spec()),0,actor('validator'))
        with self.assertRaises(GovernanceError): approval_config(s,s['revision'],digest(spec()),0,h,now=datetime.now(timezone.utc)+timedelta(days=8))

    def test_rework_retains_experiment_and_invalidates_ready(self):
        s=ready(); old=deepcopy(s)
        changed=step(s,'rework',{'reason':'new evidence'},'reviewer')
        self.assertEqual(s,old)
        self.assertEqual(changed['state'],'discovered')
        self.assertEqual(len(changed['experiments']),1)
        self.assertNotIn('ready',changed)
        with self.assertRaises(GovernanceError): approval_config(changed,s['revision'],digest(spec()),0,actor('human'))


if __name__ == '__main__': unittest.main()
