"""Read-only readiness checks before taking a lease or starting model workers."""
import json
import hashlib
from pathlib import Path
from . import repository as repo
from .rules import validate_spec, digest
from .evidence import verify_measurement


def _waiting(reason, owner, next_step):
    return {'status':'waiting','reason':reason,'system_owner':owner,'next_step':next_step}


def prepare(database, item, role, *, design_context=None, measurement=None, review_context=None, input_bytes=120_000):
    """No LLM and no mutation. Supplied host packets are not model conclusions."""
    expected=repo.ROLE_STATES[role]
    registered_design=role=='designer' and item['state']=='reviewed' and bool(item.get('issue',{}).get('effectiveness_request'))
    if item['state']!=expected and not (role=='evaluator' and item['state']=='observing') and not registered_design:
        return {'status':'not_applicable'}
    request={**(item.get('issue',{}).get('host_frozen_input') or {}),
             **(item.get('issue',{}).get('experiment_request') or {})}
    result={'status':'ready','review_context':review_context,'design_context':design_context,'measurement':measurement}
    try:
        if role=='proposer':
            kind=item.get('issue',{}).get('change_kind')
            if kind!='ranking_config':
                return _waiting('unsupported_issue_for_config_proposer','isolated_code_executor',
                    '本事项未明确标记为配置类改进，不能用小盘因子替代代码或数据问题；等待相应隔离施工能力')
            from ..short_term_lanes.rules import LANES
            if item.get('issue',{}).get('scope') not in {key for key,_,_ in LANES}:
                return _waiting('missing_atomic_strategy_scope','proposal_builder','配置提案必须限定为一个明确策略，不能扩大到其他策略')
            path=request.get('input_path'); expected_hash=request.get('input_hash')
            if not path or not expected_hash or not Path(path).is_file():
                return _waiting('missing_bound_frozen_input','evidence_collector','系统需绑定真实冻结输入路径与哈希，模型不能猜测实验数据')
            if hashlib.sha256(Path(path).read_bytes()).hexdigest()!=expected_hash:
                return _waiting('frozen_input_hash_mismatch','evidence_collector','原始冻结输入内容已变化；系统重新归档并审查，不复用旧哈希')
        if role in ('reviewer','proposer'):
            from .review_evidence import load_review_packet
            if review_context is not None:
                if item['id'] not in review_context:return {'status':'not_targeted'}
                # Explicit overrides are archived references too, never an
                # arbitrary dict bypassing source hashes/issue binding.
                loaded=load_review_packet({**item,'review_evidence':review_context[item['id']]})
            else:
                loaded=load_review_packet(item)
            if loaded['status']!='ready':
                return _waiting(loaded.get('reason','missing_original_packet'),'evidence_collector',
                                '系统证据采集器补齐并校验同事项的原始代码、输入、可复现案例与文件哈希，再交独立复核；本次不调用模型')
            packet=loaded['packet']
            result['review_context']={item['id']:packet}
        elif role=='designer':
            if design_context and design_context.get('item_id',item['id'])!=item['id']:
                return {'status':'not_targeted'}
            if not design_context:
                if item.get('issue',{}).get('effectiveness_request'):
                    from ..effectiveness.protocol_cache import context as effectiveness_context
                    design_context=effectiveness_context(database,item)
            if not design_context:
                proposal=item.get('proposal') or {}
                request={**request,**{k:proposal[k] for k in ('candidate_profile','allowed_strategy_keys') if k in proposal}}
                if not all(k in request for k in ('input_path','candidate_profile','allowed_strategy_keys')):
                    return _waiting('missing_frozen_proposal','proposal_builder',
                        '系统设计阶段需准备原子配置提案和真实冻结输入；任意代码方案尚不支持自动实施，不能跳过此阶段')
                from .experiment_runner import prepare_context
                design_context=prepare_context(database,request['input_path'],request['candidate_profile'],request['allowed_strategy_keys'])
            validate_spec(design_context['spec'])
            if item.get('proposal'):
                design_context=dict(design_context)
                design_context['spec']={**design_context['spec'],'requires_effectiveness_validation':item['proposal']['requires_effectiveness_validation']}
                design_context['spec_hash']=digest(design_context['spec'])
            result['design_context']=design_context
        elif role=='implementer':
            spec=item['design']['spec'];validate_spec(spec)
            if spec.get('change_kind','ranking_config')!='ranking_config':
                from ..effectiveness.governance_runner import supported,validate_registered
                if supported(spec):
                    validate_registered(spec)
                    from datetime import datetime
                    from zoneinfo import ZoneInfo
                    if datetime.now(ZoneInfo('Asia/Shanghai')).date().isoformat()>=spec['data_start']:
                        return _waiting('registration_window_expired','effectiveness_design',
                            '实验未在保留窗开始前登记；须重新独立设计未来区间，不能把已知行情算成样本外。')
                else:
                    return _waiting('unsupported_code_executor','isolated_code_executor',
                        '任意代码隔离施工执行器尚未实现；仅允许已注册的只读影子排序执行器')
        elif role=='evaluator':
            exp=item['experiments'][item['current_experiment']]
            if measurement:
                if measurement.get('issue_id')!=item['id']:return {'status':'not_targeted'}
                verify_measurement(measurement)
                if exp.get('evidence',{}).get('measurement_hash')==measurement.get('measurement_hash'):
                    return _waiting('unchanged_measurement','experiment_observer','等待新的实际观测证据；同一测量不重复累计样本')
            else:
                spec=exp['spec']
                from ..effectiveness.governance_runner import supported,run as run_effectiveness
                if supported(spec):
                    from datetime import datetime
                    from zoneinfo import ZoneInfo
                    measurement=run_effectiveness(database,item,datetime.now(ZoneInfo('Asia/Shanghai')).date())
                    if exp.get('evidence',{}).get('measurement_hash')==measurement['measurement_hash']:
                        return _waiting('unchanged_effectiveness_measurement','effectiveness_observer','同一日同一数据不重复记样本；等待新结果')
                    result['measurement']=measurement
                elif spec.get('change_kind','ranking_config')!='ranking_config' or spec.get('validation_kind')!='engineering':
                    return _waiting('unsupported_measurement_runner','experiment_runner','系统尚未实现此类代码或收益样本外实验；不伪造观测指标')
                if supported(spec):
                    return result
                if not request.get('input_path') or not Path(request['input_path']).is_file():
                    return _waiting('missing_experiment_input','experiment_runner','系统需提供冻结实验输入文件，再由确定性引擎实际计算')
                if item['state']=='observing':
                    return _waiting('same_snapshot_already_observed','experiment_observer','等待新的授权实验或新测量，不重复运行同一快照凑样本')
                from .experiment_runner import prepare_context
                current=prepare_context(database,request['input_path'],spec['candidate_config']['ranking_factors'],spec['allowed_strategy_keys'])['spec']
                for key in ('input_hash','baseline_code_hash','candidate_code_hash','baseline_config_hash','baseline_generation'):
                    if current[key]!=spec[key]:
                        return _waiting('frozen_provenance_changed:'+key,'experiment_runner',
                                        '冻结输入、代码或正式配置已变化；系统须重新准备实验修订，不能沿用旧验收')
        elif role in ('validator','release_preparer'):
            exp=item['experiments'][item['current_experiment']]
            if exp.get('spec',{}).get('requires_effectiveness_validation') and exp.get('evidence',{}).get('validation_kind')!='effectiveness':
                return _waiting('waiting_effectiveness_measurement','effectiveness_runner',
                    '该提案声称预测或收益改善；一天工程不变量不能替代样本外收益验证，此类验证引擎尚未实现，不启动验收模型')
            if exp.get('comparison',{}).get('status')!='passed':
                return _waiting('measurement_not_passed','experiment_observer','实际测量未通过冻结标准或样本不足，不启动验收模型')
            verify_measurement(exp['evidence'])
        packet={'item':item,'context':{k:v for k,v in result.items() if k.endswith('context')}}
        if len(json.dumps(packet,ensure_ascii=False,allow_nan=False).encode('utf-8'))>input_bytes:
            return _waiting('evidence_packet_too_large','evidence_collector','系统需产生有明确范围的原始证据包；不静默截断后让模型审核')
        return result
    except (OSError,ValueError,KeyError,TypeError,ImportError) as error:
        return _waiting('preflight_failed:'+type(error).__name__,'governance_preflight',str(error))
