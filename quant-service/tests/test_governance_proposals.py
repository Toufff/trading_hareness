import hashlib,json
from pathlib import Path
import tempfile
import unittest
from app.strategy_governance.proposals import build_proposal
from app.strategy_governance.rules import GovernanceError,advance
from tests.test_strategy_governance import actor,designed,evidence,digest,spec


class ProposalTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        path=Path(self.temp.name)/'input.json';path.write_text(json.dumps({'date':'2026-09-11','rows':[],'sessions':[],
            'events':{},'price_histories':{},'history_health':{}}),encoding='utf-8')
        self.item={'state':'reviewed','revision':2,'actors':{'observer':'o','reviewer':'r'},
            'issue':{'change_kind':'ranking_config','scope':'accumulation','proposal_purpose':'preference',
                'preference_authorization':{'source':'user','reference':'fixture explicit user request'},
                'host_frozen_input':{'input_path':str(path),'input_hash':hashlib.sha256(path.read_bytes()).hexdigest()}}}
        self.payload=dict(change='one scope preference',tradeoffs='ranking',failure_condition='scope leak',rollback='old',
            purpose='preference',factor_key='small_cap',enabled=True,weight=.12,strategy_key='accumulation')
        self.base={'ranking_factors':{'version':1,'factors':[]}}

    def test_one_atomic_profile_and_independent_designer(self):
        item=build_proposal(self.item,self.payload,actor('proposer'),self.base,0)
        self.assertEqual(item['state'],'proposed');self.assertEqual(item['revision'],3)
        self.assertEqual(item['proposal']['candidate_profile']['factors'][0]['strategies'],['accumulation'])
        with self.assertRaises(GovernanceError):advance(item,3,'design',{},actor('designer','proposer'))

    def test_no_code_conversion_and_no_scope_broadening(self):
        item={**self.item,'issue':{**self.item['issue'],'change_kind':'code'}}
        with self.assertRaises(GovernanceError):build_proposal(item,self.payload,actor('proposer'),self.base,0)
        with self.assertRaises(GovernanceError):build_proposal(self.item,{**self.payload,'strategy_key':'trend'},actor('proposer'),self.base,0)
        with self.assertRaises(GovernanceError):build_proposal(self.item,self.payload,actor('proposer','r'),self.base,0)

    def test_existing_other_lane_weight_cannot_change(self):
        base={'ranking_factors':{'version':1,'factors':[{'key':'small_cap','weight':.2,'strategies':['trend']}]}}
        with self.assertRaises(GovernanceError):build_proposal(self.item,self.payload,actor('proposer'),base,0)

    def test_noop_and_preference_downgrade_rejected(self):
        with self.assertRaises(GovernanceError):build_proposal(self.item,{**self.payload,'enabled':False},actor('proposer'),self.base,0)
        item={**self.item,'issue':{**self.item['issue'],'proposal_purpose':'predictive'}}
        with self.assertRaises(GovernanceError):build_proposal(item,self.payload,actor('proposer'),self.base,0)
        item={**self.item,'issue':{**self.item['issue'],'preference_authorization':{}}}
        with self.assertRaises(GovernanceError):build_proposal(item,self.payload,actor('proposer'),self.base,0)

    def test_predictive_cannot_pass_with_engineering_measurement(self):
        s=designed();s['design']['spec']['requires_effectiveness_validation']=True
        s=advance(s,s['revision'],'experiment',{'artifact':'fixture','artifact_hash':digest(s['design']['spec'])},actor('implementer'))
        s=advance(s,s['revision'],'observe',{**evidence(),'validation_kind':'engineering'},actor('evaluator'))
        with self.assertRaisesRegex(GovernanceError,'Engineering invariants'):
            advance(s,s['revision'],'validate',{},actor('validator'))


if __name__=='__main__':unittest.main()
