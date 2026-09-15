import json
from pathlib import Path
from unittest.mock import Mock
import pytest
from app.strategy_governance import dispatcher as d
from app.strategy_governance.rules import GovernanceError


def test_worker_command_is_fresh_read_only_and_noninteractive(tmp_path):
    argv=d.command('codex.exe',tmp_path)
    assert '--ephemeral' in argv and '--ignore-user-config' in argv
    assert argv[argv.index('--sandbox')+1]=='read-only'
    assert '--dangerously-bypass-approvals-and-sandbox' not in argv
    assert '--model' not in argv
    assert '-C' in argv and str(tmp_path) in argv
    assert 'approval_policy="never"' in argv


def test_child_does_not_inherit_database_provider_or_host_actor_secrets():
    env=d.child_environment({'PATH':'p','USERPROFILE':'u','PGPASSWORD':'secret',
        'DATABASE_URL':'secret','LONGHU_TOKEN':'secret','OPENAI_API_KEY':'secret',
        'STRATEGY_GOVERNANCE_ACTORS_FILE':'private','CODEX_THREAD_ID':'thread'})
    assert env=={'PATH':'p','USERPROFILE':'u','PYTHONIOENCODING':'utf-8'}


def test_response_rejects_extra_keys_and_host_only_measurements(tmp_path):
    path=tmp_path/'response.json'
    for response in ({'decision':'advance','payload':'{}','actor':'human'},
                     {'decision':'advance','payload':'{"metrics":{"return":99}}'},
                     {'decision':'other','payload':'{}'}):
        path.write_text(json.dumps(response),encoding='utf-8')
        with pytest.raises(GovernanceError):d.parse_response(path,d.Limits())
    path.write_text(json.dumps({'decision':'defer','payload':'{"reason":"missing evidence"}'}),encoding='utf-8')
    assert d.parse_response(path,d.Limits())[0]=='defer'


def test_input_is_bounded_and_secrets_redacted():
    prompt=d.prompt_for('reviewer',{'issue':{'api_key':'secret','evidence':['fact']}},{},d.Limits())
    assert 'secret' not in prompt and '[REDACTED]' in prompt
    with pytest.raises(GovernanceError):
        d.prompt_for('reviewer',{'x':'x'*2000},{},d.Limits(input_bytes=1024))


def test_roles_are_provisioned_and_separate(tmp_path):
    path=tmp_path/'actors.json'
    path.write_text(json.dumps({'actors':{'review':{'enabled':True,'kind':'agent','roles':['reviewer']},
        'design':{'enabled':True,'kind':'agent','roles':['designer']}}}))
    assert len(d.role_actors(path,['reviewer','designer']))==2
    with pytest.raises(GovernanceError):d.role_actors(path,['validator'])


def test_failed_or_deferred_job_keeps_database_lease_for_cooldown(tmp_path,monkeypatch):
    path=tmp_path/'actors.json'
    path.write_text(json.dumps({'actors':{'review':{'enabled':True,'kind':'agent','roles':['reviewer']}}}))
    exe=tmp_path/'codex.exe';exe.touch()
    from app.strategy_governance.review_evidence import write_packet,code_excerpt
    archive=tmp_path/'evidence';monkeypatch.setenv('QUANT_GOVERNANCE_EVIDENCE_ROOT',str(archive))
    reference=write_packet(issue_key='test-one',question='Does this observed fixture reproduce?',
        cases=[{'before':{'value':1},'after':{'value':2}}],
        code=[code_excerpt(Path(__file__),['def test_failed_or_deferred_job_keeps_database_lease_for_cooldown'])],
        facts={'test_fixture':True},limitations=['Fixture only; no production finding.'],root=archive)
    item={'id':'one','revision':1,'issue':{'dedupe_key':'test-one'},'state':'discovered'}
    claim=Mock(return_value={'status':'claimed','item':item,'expires_at':'later'})
    monkeypatch.setattr(d.repo,'claim_work',claim)
    monkeypatch.setattr(d.repo,'list_items',lambda db:[item])
    monkeypatch.setattr(d,'execute_claim',lambda *a,**k:{'status':'deferred','reason':'need data'})
    result=d.dispatch(object(),registry=path,job_root=tmp_path/'jobs',roles=('reviewer',),execute=True,executable=str(exe),
        review_context={'one':reference})
    assert result['status']=='waiting'
    assert claim.call_args.kwargs['minutes']==60
    assert Path(result['jobs'][0]['job_dir'],'outcome.json').is_file()
    assert not (tmp_path/'jobs'/'dispatcher.lock').exists()
    repeated=d.dispatch(object(),registry=path,job_root=tmp_path/'jobs',roles=('reviewer',),execute=True,executable=str(exe),
        review_context={'one':reference})
    assert len(repeated['jobs'])==0 and claim.call_count==1
    assert any(x['reason']=='unchanged_failed_or_deferred_input' for x in repeated['waiting_roles'])


def test_dry_run_does_not_claim_or_invoke_model(tmp_path,monkeypatch):
    path=tmp_path/'actors.json'
    path.write_text(json.dumps({'actors':{'review':{'enabled':True,'kind':'agent','roles':['reviewer']}}}))
    monkeypatch.setattr(d.repo,'list_items',lambda db:[])
    claim=Mock();monkeypatch.setattr(d.repo,'claim_work',claim)
    result=d.dispatch(object(),registry=path,job_root=tmp_path/'jobs',roles=('reviewer',))
    assert result['status']=='dry_run' and not claim.called and not (tmp_path/'jobs').exists()


@pytest.mark.parametrize('invalid',['raw_dict','wrong_issue','wrong_hash'])
def test_explicit_review_reference_cannot_bypass_archive_contract(tmp_path,monkeypatch,invalid):
    from app.strategy_governance.review_evidence import write_packet,code_excerpt
    registry=tmp_path/'registry.json'
    registry.write_text(json.dumps({'actors':{'review':{'enabled':True,'kind':'agent','roles':['reviewer']}}}))
    exe=tmp_path/'codex.exe';exe.touch()
    archive=tmp_path/'evidence';monkeypatch.setenv('QUANT_GOVERNANCE_EVIDENCE_ROOT',str(archive))
    reference=write_packet(issue_key='test-bound',question='Original fixture?',cases=[{'before':1,'after':2}],
        code=[code_excerpt(Path(__file__),['def test_explicit_review_reference_cannot_bypass_archive_contract'])],
        facts={},limitations=['Test fixture'],root=archive)
    if invalid=='raw_dict':reference={'original_source':'arbitrary unverified data'}
    if invalid=='wrong_issue':reference={**reference,'issue_key':'another-issue'}
    if invalid=='wrong_hash':reference={**reference,'sha256':'0'*64}
    item={'id':'one','revision':1,'state':'discovered','issue':{'dedupe_key':'test-bound'}}
    monkeypatch.setattr(d.repo,'list_items',lambda db:[item])
    claim=Mock();monkeypatch.setattr(d.repo,'claim_work',claim)
    runner=Mock();monkeypatch.setattr(d,'execute_claim',runner)
    result=d.dispatch(object(),registry=registry,job_root=tmp_path/'jobs',roles=('reviewer',),execute=True,
        executable=exe,review_context={'one':reference})
    assert not claim.called and not runner.called and not result['jobs']
    assert result['waiting_roles']


def test_no_model_spent_on_missing_measurement_or_context(tmp_path):
    actor={'id':'one','roles':['evaluator']}
    runner=Mock(side_effect=AssertionError('must not start model'))
    item={'id':'i','revision':1}
    result=d.execute_claim(object(),item,'evaluator',actor,tmp_path,'codex.exe',d.Limits(),model_runner=runner)
    assert result['status']=='deferred'
    result=d.execute_claim(object(),item,'designer',actor,tmp_path,'codex.exe',d.Limits(),model_runner=runner)
    assert result['status']=='deferred' and not runner.called


def test_dispatcher_never_imports_or_calls_activation():
    source=Path(d.__file__).read_text(encoding='utf-8')
    assert 'repo.activate(' not in source and 'repo.rollback(' not in source


def test_busy_lock_prevents_duplicate_processes(tmp_path,monkeypatch):
    path=tmp_path/'actors.json'
    path.write_text(json.dumps({'actors':{'review':{'enabled':True,'kind':'agent','roles':['reviewer']}}}))
    jobs=tmp_path/'jobs';jobs.mkdir();(jobs/'dispatcher.lock').write_text('owner')
    claim=Mock();monkeypatch.setattr(d.repo,'claim_work',claim)
    result=d.dispatch(object(),registry=path,job_root=jobs,roles=('reviewer',),execute=True)
    assert result['status']=='busy' and not claim.called


def test_measurement_targets_only_its_own_issue_before_claim():
    def item(name):
        return {'id':name,'state':'observing','current_experiment':0,'experiments':[
            {'spec':{'change_kind':'ranking_config'},'comparison':{'status':'insufficient'},
             'evidence':{'measurement_hash':'old'}}]}
    a,b=item('A'),item('B')
    evidence={'issue_id':'B','measurement_hash':'new'}
    assert d.eligible_work([a,b],'evaluator',measurement=evidence)==['B']
    assert d.eligible_work([a,b],'evaluator',measurement={'measurement_hash':'new'})==[]
    assert d.eligible_work([a,b],'evaluator',measurement={'issue_id':'B','measurement_hash':'old'})==[]


def test_original_review_context_selects_requested_issue():
    a,b={'id':'A','state':'discovered'},{'id':'B','state':'discovered'}
    context={'B':{'code_excerpt':'actual original source'}}
    assert d.eligible_work([a,b],'reviewer',review_context=context)==['B']


def test_postclose_governance_is_hidden_bounded_and_does_not_reclassify_scan():
    source=(Path(__file__).resolve().parents[2]/'scripts/windows/run-post-close-pipeline.ps1').read_text(encoding='utf-8')
    block=source.split('function Start-IndependentGovernance {',1)[1].split('\ntrap {',1)[0]
    assert '-WindowStyle Hidden' in block and 'pythonw.exe' in block
    assert "'--max-jobs', '1'" in block and "'--daily-jobs', '6'" in block
    assert 'governance_dispatched' in block and 'not a completed-governance assertion' in block
    assert "$record['status']" not in block


def test_structured_payload_object_avoids_actual_double_encoding_failure(tmp_path):
    path=tmp_path/'response.json'
    fields={'reason':None,'reproduction':'independent raw facts','counterexample':'adverse case',
            'alternative_explanation':'other cause','verdict':'confirmed'}
    path.write_text(json.dumps({'decision':'advance','payload':fields}),encoding='utf-8')
    decision,payload=d.parse_response(path,d.Limits(),'reviewer')
    assert decision=='advance' and 'reason' not in payload and payload['verdict']=='confirmed'
    # Valid legacy artifacts remain readable, but the real broken inner JSON is
    # rejected. Never append braces or manufacture a successful review.
    path.write_text(json.dumps({'decision':'advance','payload':json.dumps(fields)}),encoding='utf-8')
    assert d.parse_response(path,d.Limits(),'reviewer')==(decision,payload)
    path.write_text(json.dumps({'decision':'advance','payload':json.dumps(fields)[:-1]}),encoding='utf-8')
    with pytest.raises(json.JSONDecodeError):d.parse_response(path,d.Limits(),'reviewer')


def test_role_schema_uses_required_nullable_fields_not_nested_json_strings():
    for role in d.MODEL_ROLES:
        schema=d.response_schema(role)['properties']['payload']
        assert schema['type']=='object' and schema['additionalProperties'] is False
        assert set(schema['required'])==set(schema['properties'])
        assert all(isinstance(p['type'],list) and 'null' in p['type'] and 'object' not in p['type'] for p in schema['properties'].values())
        assert 'spec' not in schema['properties']


def test_designer_spec_is_injected_by_host_not_reencoded_by_model(tmp_path,monkeypatch):
    spec={'host_frozen_identity':'abc','candidate_config':{'ranking_factors':{'factors':[]}}}
    item={'id':'B','revision':2,'issue':{}}
    actor={'id':'designer','roles':['designer']}
    response={'change':'one atomic change','tradeoffs':'cost','failure_condition':'fails','rollback':'restore'}
    runner=Mock(return_value=('advance',response))
    monkeypatch.setattr(d,'validate_spec',lambda value:None)
    transition=Mock(return_value={'id':'B','state':'designed','revision':3})
    monkeypatch.setattr(d.repo,'transition',transition)
    d.execute_claim(object(),item,'designer',actor,tmp_path,'codex.exe',d.Limits(),
                    design_context={'spec':spec},model_runner=runner)
    assert transition.call_args.args[4]['spec']==spec
    assert runner.call_args.kwargs['role']=='designer'


def test_scheduled_environment_without_path_finds_local_native_installation(tmp_path):
    binary=tmp_path/'OpenAI/Codex/bin/build1/codex.exe'
    binary.parent.mkdir(parents=True);binary.touch()
    assert d.find_codex_executable(environ={'PATH':'','LOCALAPPDATA':str(tmp_path)})==str(binary.resolve())


def test_registry_executable_precedes_path_and_local_installation(tmp_path):
    binary=tmp_path/'registered.exe';binary.touch()
    explicit=tmp_path/'explicit.exe';explicit.touch()
    registry=tmp_path/'registry.json'
    registry.write_text(json.dumps({'runtime':{'codex_executable':str(binary)}}))
    assert d.find_codex_executable(registry=registry,environ={'PATH':''})==str(binary.resolve())
    assert d.find_codex_executable(explicit,registry,environ={'PATH':''})==str(explicit.resolve())


def test_missing_native_executable_persists_preflight_failure(tmp_path,monkeypatch):
    registry=tmp_path/'registry.json'
    registry.write_text(json.dumps({'actors':{'review':{'enabled':True,'kind':'agent','roles':['reviewer']}}}))
    def missing(*a,**k):raise FileNotFoundError('Native executable missing')
    monkeypatch.setattr(d,'find_codex_executable',missing)
    claim=Mock();monkeypatch.setattr(d.repo,'claim_work',claim)
    result=d.dispatch(object(),registry=registry,job_root=tmp_path/'jobs',roles=('reviewer',),execute=True)
    assert result['status']=='host_preflight_failed' and not claim.called
    assert json.loads((tmp_path/'jobs/latest.json').read_text())['status']=='host_preflight_failed'
    assert not (tmp_path/'jobs/dispatcher.lock').exists()


def test_preflight_missing_originals_spends_no_model_or_claim(tmp_path,monkeypatch):
    registry=tmp_path/'registry.json'
    registry.write_text(json.dumps({'actors':{'review':{'enabled':True,'kind':'agent','roles':['reviewer']}}}))
    exe=tmp_path/'codex.exe';exe.touch()
    monkeypatch.setattr(d.repo,'list_items',lambda db:[{'id':'PV03','revision':1,'state':'discovered','issue':{'evidence':['path-only']}}])
    claim=Mock();monkeypatch.setattr(d.repo,'claim_work',claim)
    runner=Mock();monkeypatch.setattr(d,'run_model',runner)
    result=d.dispatch(object(),registry=registry,job_root=tmp_path/'jobs',roles=('reviewer',),execute=True,executable=exe)
    assert not claim.called and not runner.called and result['jobs']==[]
    assert any(x.get('system_owner') in ('evidence_collector','governance_preflight') for x in result['waiting_roles'])


def test_unsupported_code_is_waiting_before_lease(monkeypatch):
    from app.strategy_governance import dispatch_preflight as pre
    monkeypatch.setattr(pre,'validate_spec',lambda spec:None)
    item={'id':'C','state':'designed','issue':{},'design':{'spec':{'change_kind':'code'}}}
    result=pre.prepare(object(),item,'implementer')
    assert result['status']=='waiting' and result['system_owner']=='isolated_code_executor'


def test_proposer_cannot_turn_code_issue_into_small_cap_tuning(monkeypatch):
    from app.strategy_governance import dispatch_preflight as pre
    monkeypatch.setitem(d.repo.ROLE_STATES,'proposer','reviewed')
    item={'id':'PV03','state':'reviewed','issue':{'change_kind':'code'}}
    result=pre.prepare(object(),item,'proposer')
    assert result['reason']=='unsupported_issue_for_config_proposer'


def test_proposer_payload_has_one_typed_whitelisted_factor(tmp_path):
    payload={'reason':None,'change':'single preference','tradeoffs':'not alpha','failure_condition':'outside scope',
             'rollback':'restore','purpose':'preference','factor_key':'small_cap','enabled':True,'weight':.12,'strategy_key':'accumulation'}
    path=tmp_path/'response.json';path.write_text(json.dumps({'decision':'advance','payload':payload}),encoding='utf-8')
    decision,actual=d.parse_response(path,d.Limits(),'proposer')
    assert decision=='advance' and actual['enabled'] is True and actual['weight']==.12
    assert d.response_schema('proposer')['properties']['payload']['properties']['factor_key']['enum']==['small_cap',None]
