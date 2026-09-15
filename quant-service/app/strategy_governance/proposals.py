"""Accept one bounded configuration proposal; independent designer freezes it later."""
from copy import deepcopy
from .rules import require,actor_role,text_fields,digest,check_config_scope
from .experiment_runner import read_frozen
from ..ranking_factors import load_profile
from ..short_term_lanes.rules import LANES


def build_proposal(item,payload,actor,baseline,generation):
    actor_role(actor,'proposer')
    require(item['state']=='reviewed','Proposal requires independently reviewed issue')
    require(actor['id'] not in item['actors'].values(),'Proposer must differ from observer and reviewer')
    require(item['issue'].get('change_kind')=='ranking_config','Only explicitly typed ranking_config issue may generate a configuration proposal')
    required={'change','tradeoffs','failure_condition','rollback','purpose','factor_key','enabled','weight','strategy_key'}
    require(set(payload)==required,'Exactly one structured proposal required; no extra paths/hashes/metrics')
    text_fields(payload,['change','tradeoffs','failure_condition','rollback'])
    require(payload['purpose'] in ('preference','predictive'),'Proposal purpose required')
    host_purpose=item['issue'].get('proposal_purpose','predictive')
    require(payload['purpose']==host_purpose,'Proposer cannot relabel a predictive improvement as a preference')
    if host_purpose=='preference':
        authorization=item['issue'].get('preference_authorization') or {}
        require(authorization.get('source')=='user' and isinstance(authorization.get('reference'),str) and authorization['reference'],
                'Preference-only validation requires an explicit recorded user preference authorization')
    require(payload['factor_key']=='small_cap','Only registered small_cap factor is supported')
    require(type(payload['enabled']) is bool and type(payload['weight']) in (int,float) and 0<=payload['weight']<=.25,'Invalid enable/weight')
    require(not payload['enabled'] or payload['weight']>0,'Zero weight is not an effective enabled proposal; use explicit disabled')
    keys={l[0] for l in LANES};key=payload['strategy_key']
    require(key in keys and item['issue']['scope']==key,'Proposal must affect exactly the reviewed single strategy scope')
    frozen=item['issue'].get('host_frozen_input') or item['issue'].get('experiment_request') or {}
    require(isinstance(frozen.get('input_path'),str) and isinstance(frozen.get('input_hash'),str),'Host frozen market input required')
    _,actual=read_frozen(frozen['input_path']);require(actual==frozen['input_hash'],'Host input hash changed')
    specs=load_profile(baseline['ranking_factors'],keys)
    active=next((s for s in specs if s.key=='small_cap'),None)
    enabled_scopes=(set(keys) if active and '*' in active.strategies else set(active.strategies) if active else set())
    others=enabled_scopes-{key}
    if payload['enabled']:
        require(not others or active.weight==payload['weight'],'Current factor schema cannot give one lane a different weight while preserving other lanes; split unsupported capability, do not broaden scope')
        enabled_scopes.add(key);weight=payload['weight']
    else:
        enabled_scopes.discard(key);weight=active.weight if active else payload['weight']
    factors=[{'key':'small_cap','weight':weight,'strategies':sorted(enabled_scopes)}] if enabled_scopes else []
    candidate={'version':1,'factors':factors}
    changed=check_config_scope({'candidate_config':{'ranking_factors':candidate},'allowed_strategy_keys':[key]},baseline)
    require(changed==[key],'Proposal is a no-op; no experiment or model token should be spent')
    proposal={'actor':actor['id'],'payload':deepcopy(payload),'candidate_profile':candidate,'allowed_strategy_keys':[key],
        'baseline_generation':generation,'baseline_config_hash':digest(baseline),'input_path':frozen['input_path'],
        'input_hash':actual,'requires_effectiveness_validation':payload['purpose']=='predictive',
        'validation_scope':'engineering_preference' if payload['purpose']=='preference' else 'effectiveness_required'}
    proposal['proposal_hash']=digest(proposal)
    result=deepcopy(item);result['proposal']=proposal;result.setdefault('proposals',[]).append(deepcopy(proposal))
    result['actors']['proposer']=actor['id'];result['state']='proposed';result['revision']+=1
    result['issue']['experiment_request']={k:proposal[k] for k in ('candidate_profile','allowed_strategy_keys','input_path','input_hash','requires_effectiveness_validation')}
    result['issue']['experiment_request']['change_kind']='ranking_config'
    return result
