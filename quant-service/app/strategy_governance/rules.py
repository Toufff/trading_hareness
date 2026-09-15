"""Pure, revisioned governance rules. Different identities are necessary, not proof of independence."""
from copy import deepcopy
from datetime import date, datetime, timezone, timedelta
import hashlib
import json
import math


class GovernanceError(ValueError):
    pass


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
        separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def now_utc():
    return datetime.now(timezone.utc)


def require(condition, message):
    if not condition:
        raise GovernanceError(message)


def text_fields(payload, keys):
    for key in keys:
        require(isinstance(payload.get(key), str) and bool(payload[key].strip()), f'Missing {key}')


def actor_role(actor, role):
    require(bool(actor.get('id')) and role in actor.get('roles', []), f'Actor requires {role} role')


def new_issue(payload, actor):
    actor_role(actor, 'observer')
    text_fields(payload, ['title', 'problem', 'hypothesis', 'scope', 'out_of_scope', 'dedupe_key'])
    require(isinstance(payload.get('evidence'), list) and len(payload['evidence']) > 0, 'Original evidence required')
    dependencies = payload.get('dependencies', [])
    require(isinstance(dependencies, list) and all(isinstance(x, str) and x for x in dependencies), 'Invalid dependencies')
    return dict(title=payload['title'], state='discovered', revision=1,
        issue=deepcopy(payload), actors={'observer': actor['id']}, dependencies=dependencies,
        reviews=[], experiments=[], current_experiment=None, live_effect='none')


def validate_config(config):
    require(isinstance(config, dict) and set(config) == {'ranking_factors'}, 'Only ranking_factors config promotion is supported')
    from ..ranking_factors import load_profile
    from ..short_term_lanes.rules import LANES
    load_profile(config['ranking_factors'], {x[0] for x in LANES})
    return deepcopy(config)


def validate_spec(spec):
    text_fields(spec, ['input_hash', 'baseline_code_hash', 'candidate_code_hash', 'baseline_config_hash', 'holdout_id', 'data_start', 'data_end'])
    require(all(len(spec[k]) == 64 and all(c in '0123456789abcdef' for c in spec[k])
                for k in ['input_hash', 'baseline_code_hash', 'candidate_code_hash', 'baseline_config_hash']), 'SHA256 manifests required')
    try:
        start, end = date.fromisoformat(spec['data_start']), date.fromisoformat(spec['data_end'])
    except (TypeError, ValueError) as exc:
        raise GovernanceError('Strict ISO dates required') from exc
    require(str(start)==spec['data_start'] and str(end)==spec['data_end'] and start<=end, 'Invalid date interval')
    from ..short_term_lanes.rules import LANES
    keys = spec.get('allowed_strategy_keys')
    require(isinstance(keys,list) and keys and len(keys)==len(set(keys)) and set(keys)<={x[0] for x in LANES}, 'Explicit valid allowed_strategy_keys required')
    require(isinstance(spec.get('baseline_generation'), int) and spec['baseline_generation'] >= 0, 'Baseline generation required')
    require(isinstance(spec.get('minimum_sessions'), int) and spec['minimum_sessions'] > 0, 'Minimum independent sessions required')
    require(isinstance(spec.get('minimum_observations'), int) and spec['minimum_observations'] > 0, 'Minimum observations required')
    criteria = spec.get('criteria')
    require(isinstance(criteria, list) and criteria, 'Frozen criteria required')
    for criterion in criteria:
        require(set(criterion) == {'metric', 'operator', 'threshold'}, 'Invalid criterion')
        require(isinstance(criterion['metric'], str) and criterion['metric'], 'Metric required')
        require(criterion['operator'] in ['>=', '<='], 'Unsupported criterion operator')
        require(type(criterion['threshold']) in (int, float) and math.isfinite(criterion['threshold']), 'Finite threshold required')
    kind = spec.get('change_kind', 'ranking_config')
    require(kind in ('ranking_config', 'code'), 'Unsupported change kind')
    if kind == 'ranking_config':
        validate_config(spec['candidate_config'])
    else:
        text_fields(spec, ['candidate_manifest', 'release_plan'])
        require('candidate_config' not in spec, 'Code experiment cannot smuggle a live config')


def compare(spec, evidence):
    """Compare frozen, externally measured evidence, never infer profitability from passed flags."""
    validate_spec(spec)
    for key in ['input_hash', 'baseline_code_hash', 'candidate_code_hash', 'holdout_id']:
        require(evidence.get(key) == spec[key], f'Comparison provenance mismatch: {key}')
    text_fields(evidence, ['measurement_artifact', 'measurement_hash', 'method', 'limitations'])
    require(len(evidence['measurement_hash']) == 64 and all(c in '0123456789abcdef' for c in evidence['measurement_hash']), 'Measurement SHA256 required')
    sessions = evidence.get('sessions', [])
    require(isinstance(sessions, list) and all(isinstance(x, str) for x in sessions), 'Independent trading dates required')
    try:
        require(all(str(date.fromisoformat(x))==x for x in sessions), 'Strict ISO session dates required')
    except ValueError as exc:
        raise GovernanceError('Strict ISO session dates required') from exc
    require(all(spec['data_start'] <= x <= spec['data_end'] for x in sessions), 'Sessions outside frozen interval')
    require(type(evidence.get('observations')) is int and evidence['observations'] >= 0, 'Observation count required')
    metrics = evidence.get('metrics', {})
    checks = []
    for c in spec['criteria']:
        value = metrics.get(c['metric'])
        valid = type(value) in (int, float) and math.isfinite(value)
        passed = valid and (value >= c['threshold'] if c['operator'] == '>=' else value <= c['threshold'])
        checks.append({**c, 'value': value, 'passed': bool(passed)})
    enough = len(set(sessions)) >= spec['minimum_sessions'] and evidence['observations'] >= spec['minimum_observations']
    return dict(status='passed' if enough and all(c['passed'] for c in checks) else 'failed' if enough else 'insufficient',
        checks=checks, sample_sufficient=enough, independent_sessions=len(set(sessions)),
        evidence_hash=digest(evidence), profitability_claim=False)


def check_config_scope(spec, baseline):
    """A factor edit cannot claim one lane while changing every lane through a wildcard."""
    if spec.get('change_kind', 'ranking_config') != 'ranking_config':
        return []  # Code changes are never automatically applied by this subsystem.
    from ..ranking_factors import load_profile
    from ..short_term_lanes.rules import LANES
    keys = {x[0] for x in LANES}
    def expand(config):
        factors = load_profile(config['ranking_factors'], keys)
        return {k:sorted((f.key,f.weight) for f in factors if '*' in f.strategies or k in f.strategies) for k in keys}
    old, new = expand(baseline), expand(spec['candidate_config'])
    changed = {k for k in keys if old[k] != new[k]}
    require(changed <= set(spec['allowed_strategy_keys']), 'Candidate config changes undeclared strategies: '+','.join(sorted(changed-set(spec['allowed_strategy_keys']))))
    return sorted(changed)


TRANSITIONS = {
    'review': ('discovered', 'reviewed', 'reviewer'),
    'design': ('reviewed', 'designed', 'designer'),
    'experiment': ('designed', 'experiment', 'implementer'),
    'observe': ('experiment', 'observing', 'evaluator'),
    'validate': ('observing', 'validated', 'validator'),
    'ready': ('validated', 'ready', 'release_preparer'),
}


def advance(snapshot, expected_revision, action, payload, actor, *, now=None):
    now = now or now_utc()
    require(snapshot['revision'] == expected_revision, 'Revision conflict; reread current item')
    result = deepcopy(snapshot)
    if action in ('rework', 'reject'):
        require(snapshot['state'] not in ('activated', 'rejected'), 'Terminal revision cannot be changed')
        require(set(actor.get('roles', [])) & {'reviewer', 'validator', 'designer', 'proposer'}, 'Review role required')
        require(actor['id'] not in (snapshot['actors'].get('observer'), snapshot['actors'].get('implementer')), 'Self rejection/rework review prohibited')
        text_fields(payload, ['reason'])
        result['reviews'].append(dict(action=action, actor=actor['id'], **deepcopy(payload)))
        result['state'] = 'rejected' if action == 'reject' else 'discovered'
        if action == 'rework':
            result['actors'] = {'observer': snapshot['actors']['observer']}
            result['current_experiment'] = None
            result.pop('ready', None)
    else:
        require(action in TRANSITIONS, 'Unknown action')
        start, end, role = TRANSITIONS[action]
        require(snapshot['state'] == start or (action == 'observe' and snapshot['state'] == 'observing')
                or (action=='design' and snapshot['state']=='proposed'), f'{action} requires state {start}')
        actor_role(actor, role)
        a = snapshot['actors']
        if action == 'review':
            require(actor['id'] != a['observer'], 'Proposer cannot review own issue')
            text_fields(payload, ['reproduction', 'counterexample', 'alternative_explanation', 'verdict'])
            require(payload['verdict'] == 'confirmed', 'Only confirmed review advances; reject or rework otherwise')
        elif action == 'design':
            require(actor['id'] != a.get('proposer'),'Proposer cannot independently approve their design')
            text_fields(payload, ['change', 'tradeoffs', 'failure_condition', 'rollback'])
            validate_spec(payload['spec'])
            result['design'] = deepcopy(payload)
        elif action == 'experiment':
            require(actor['id'] != a['reviewer'], 'Reviewer cannot implement the reviewed change')
            text_fields(payload, ['artifact', 'artifact_hash'])
            require(payload['artifact_hash'] == digest(snapshot['design']['spec']), 'Experiment must bind frozen specification')
            experiment = dict(spec=deepcopy(snapshot['design']['spec']), artifact=payload['artifact'],
                artifact_hash=payload['artifact_hash'], implementer=actor['id'])
            result['experiments'].append(experiment)
            result['current_experiment'] = len(result['experiments']) - 1
        elif action == 'observe':
            require(actor['id'] != a['implementer'], 'Implementer cannot act as independent evaluator')
            exp = result['experiments'][result['current_experiment']]
            exp['comparison'] = compare(exp['spec'], payload)
            exp['evidence'] = deepcopy(payload)
            exp.setdefault('observations', []).append({'evidence':deepcopy(payload), 'comparison':deepcopy(exp['comparison'])})
        elif action == 'validate':
            require(actor['id'] not in set(a.values()), 'Validator must be independent from all prior actors')
            exp = result['experiments'][result['current_experiment']]
            require(exp['comparison']['status'] == 'passed', 'Evidence has not passed frozen criteria')
            require(not exp['spec'].get('requires_effectiveness_validation') or exp['evidence'].get('validation_kind')=='effectiveness',
                    'Engineering invariants cannot satisfy a predictive strategy-effectiveness claim')
            text_fields(payload, ['independent_reproduction', 'leakage_review', 'adverse_cases', 'limitations', 'measurement_hash'])
            require(payload['measurement_hash'] == exp['evidence']['measurement_hash'], 'Validator reviewed a different measurement')
            exp['validation'] = deepcopy(payload)
        elif action == 'ready':
            text_fields(payload, ['summary', 'risk', 'rollback'])
            exp = result['experiments'][result['current_experiment']]
            result['ready'] = dict(**deepcopy(payload), artifact_hash=exp['artifact_hash'],
                expires_at=(now + timedelta(days=7)).isoformat(), approval_revision=expected_revision + 1,
                validation_scope=exp['evidence'].get('validation_kind','unspecified'))
        result['actors'][role] = actor['id']
        result['state'] = end
        result['reviews'].append(dict(action=action, actor=actor['id'], payload=deepcopy(payload)))
    result['revision'] += 1
    result['live_effect'] = 'none'
    return result


def approval_config(snapshot, revision, artifact_hash, generation, actor, *, now=None):
    actor_role(actor, 'human')
    require(snapshot['state'] == 'ready', 'Not ready for human activation')
    require(snapshot['revision'] == revision == snapshot['ready']['approval_revision'], 'Stale approval revision')
    require(snapshot['ready']['artifact_hash'] == artifact_hash, 'Stale artifact approval')
    require(datetime.fromisoformat(snapshot['ready']['expires_at']) > (now or now_utc()), 'Approval expired')
    exp = snapshot['experiments'][snapshot['current_experiment']]
    require(exp['spec'].get('change_kind', 'ranking_config') == 'ranking_config',
            'Code changes require an external human-reviewed release; config activation is prohibited')
    require(exp['spec']['baseline_generation'] == generation, 'Active baseline changed; rework required')
    return validate_config(exp['spec']['candidate_config'])
