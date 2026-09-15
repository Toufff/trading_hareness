import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from app.strategy_governance.experiment_runner import read_frozen, measure, prepare_context, run_experiment
from app.strategy_governance.configuration import code_fingerprint
from app.strategy_governance.rules import digest, GovernanceError


class Cursor:
    def fetchone(self):return {'is_open':True}


class DB:
    def transaction(self):return self
    def __enter__(self):return self
    def __exit__(self,*args):pass
    def execute(self,*args):return Cursor()


def frozen():
    return {'date':'2026-09-11','rows':[],'sessions':['2026-09-10','2026-09-11'],
            'events':{},'price_histories':{},'history_health':{}}


class GovernanceExperimentRunnerTests(unittest.TestCase):
    def test_measurements_not_passed_flags(self):
        before={'status':'completed','lanes':[{'key':'accumulation','tracking_candidates':[
            {'symbol':'A','rank_score':2,'buy_authorized':False},{'symbol':'B','rank_score':1,'buy_authorized':False}]}]}
        after={'status':'completed','lanes':[{'key':'accumulation','tracking_candidates':[
            {'symbol':'A','rank_score':2,'buy_authorized':False},{'symbol':'B','rank_score':3,'buy_authorized':False}]}]}
        metrics,details=measure(before,after,{'accumulation'})
        self.assertEqual(metrics['eligibility_changed_count'],0)
        self.assertEqual(metrics['rank_changed_count'],2)
        self.assertEqual(metrics['out_of_scope_changed_count'],0)
        self.assertEqual(measure(before,after,set())[0]['out_of_scope_changed_count'],1)

    def test_future_input_and_byte_changes_detected(self):
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/'input.json';path.write_text(json.dumps(frozen()),encoding='utf-8')
            original=read_frozen(path)[1]
            path.write_text(json.dumps(frozen(),indent=2),encoding='utf-8')
            self.assertNotEqual(original,read_frozen(path)[1])
            f=frozen();f['sessions'].append('2026-09-12');path.write_text(json.dumps(f),encoding='utf-8')
            with self.assertRaises(GovernanceError):read_frozen(path)

    def test_single_snapshot_is_one_session_not_input_history_count(self):
        with tempfile.TemporaryDirectory() as temp, patch('app.strategy_governance.experiment_runner._baseline',return_value=({'ranking_factors':{'version':1,'factors':[]}},0)):
            path=Path(temp)/'input.json';path.write_text(json.dumps(frozen()),encoding='utf-8')
            context=prepare_context(DB(),path,{'version':1,'factors':[]},['accumulation'])
            item={'id':'fixture','state':'experiment','revision':4,'current_experiment':0,'experiments':[{'spec':context['spec']}]}
            with patch('app.strategy_governance.experiment_runner.get_issue',return_value=item):
                result=run_experiment(DB(),'fixture',path,Path(temp)/'result')
            measurement=json.loads((Path(temp)/'result'/'measurement.json').read_text(encoding='utf-8'))
            self.assertEqual(measurement['sessions'],['2026-09-11'])
            self.assertEqual(measurement['observations'],0)
            self.assertEqual(result['comparison']['status'],'insufficient')
            self.assertFalse(result['stage_advanced'])
            self.assertNotIn('return',measurement['metrics'])

    def test_code_runner_refuses_instead_of_fake_success(self):
        item={'state':'experiment','current_experiment':0,'experiments':[{'spec':{'change_kind':'code'}}]}
        with patch('app.strategy_governance.experiment_runner.get_issue',return_value=item):
            with self.assertRaisesRegex(GovernanceError,'not supported'):
                run_experiment(DB(),'fixture','unused','unused')


if __name__=='__main__':unittest.main()
