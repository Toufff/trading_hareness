import asyncio
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from app.strategy_governance.evidence import verify_measurement
from app.strategy_governance.identity import resolve_actor
from app.strategy_governance.rules import GovernanceError, validate_spec, approval_config, check_config_scope
from app.routers.strategy_governance import build_strategy_governance_router
from tests.test_strategy_governance import spec, evidence, ready, actor


class GovernanceBoundaryTests(unittest.TestCase):
    def test_http_has_no_mutations(self):
        router=build_strategy_governance_router(object())
        self.assertEqual(len(router.routes),1)
        self.assertEqual(router.routes[0].methods,{'GET'})

    def test_artifact_tampering_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/'measurement.json'; path.write_text('{}',encoding='utf-8')
            e=evidence();e['measurement_artifact']=str(path)
            with self.assertRaises(GovernanceError):verify_measurement(e)

    def test_local_registry_not_user_roles(self):
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/'actors.json'
            path.write_text(json.dumps({'actors':{'x':{'enabled':True,'kind':'agent','roles':['reviewer']},
                'bad':{'enabled':True,'kind':'agent','roles':['human']}}}),encoding='utf-8')
            self.assertEqual(resolve_actor('x',path)['roles'],['reviewer'])
            with self.assertRaises(GovernanceError):resolve_actor('unknown',path)
            with self.assertRaises(GovernanceError):resolve_actor('bad',path)

    def test_code_experiment_supported_but_not_config_activation(self):
        s=spec();s.pop('candidate_config');s.update(change_kind='code',candidate_manifest='manifest.json',release_plan='manual release')
        validate_spec(s)
        item=ready();item['experiments'][0]['spec']=s
        with self.assertRaisesRegex(GovernanceError,'external human-reviewed release'):
            approval_config(item,item['revision'],item['ready']['artifact_hash'],0,actor('human'))

    def test_human_cli_rejects_noninteractive(self):
        path=Path(__file__).resolve().parents[2]/'scripts'/'strategy-governance.py'
        module_spec=importlib.util.spec_from_file_location('governance_cli',path)
        cli=importlib.util.module_from_spec(module_spec);module_spec.loader.exec_module(cli)
        with patch('sys.stdin.isatty',return_value=False):
            with self.assertRaises(GovernanceError):cli.human_challenge({'kind':'human','roles':['human']},'ACTIVATE')

    def test_wildcard_factor_cannot_escape_declared_scope(self):
        s=spec();s['candidate_config']['ranking_factors']['factors']=[{'key':'small_cap','weight':.12,'strategies':['*']}]
        with self.assertRaises(GovernanceError):check_config_scope(s,{'ranking_factors':{'version':1,'factors':[]}})
        s['candidate_config']['ranking_factors']['factors'][0]['strategies']=['accumulation']
        self.assertEqual(check_config_scope(s,{'ranking_factors':{'version':1,'factors':[]}}),['accumulation'])

    def test_invalid_date_and_scope_rejected(self):
        s=spec();s['data_start']='2026-02-31'
        with self.assertRaises(GovernanceError):validate_spec(s)

    def test_real_liquidity_file_bytes_change_code_fingerprint(self):
        from app.strategy_governance.configuration import code_fingerprint
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);(root/'short_term_lanes').mkdir();(root/'ranking_factors').mkdir()
            liquidity=root/'short_term_liquidity.py';liquidity.write_text('WEIGHT=.4',encoding='utf-8')
            original=code_fingerprint(root)
            liquidity.write_text('WEIGHT=.5',encoding='utf-8')
            self.assertNotEqual(original,code_fingerprint(root))
        s=spec();s['allowed_strategy_keys']=['bogus']
        with self.assertRaises(GovernanceError):validate_spec(s)


if __name__=='__main__':unittest.main()
