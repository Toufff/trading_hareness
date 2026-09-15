"""Trusted-host mutations. HTTP readers cannot choose a mutation identity."""
from datetime import timedelta
from uuid import uuid4
from psycopg.types.json import Json
from .rules import new_issue, advance, digest, require, approval_config, actor_role, now_utc, check_config_scope
from .evidence import verify_measurement
from .configuration import code_fingerprint, environment_config


def _event(c, snapshot, action, actor):
    c.execute('''INSERT INTO quant.strategy_governance_events
        (item_id,revision,action,actor_id,evidence_hash,snapshot) VALUES(%s,%s,%s,%s,%s,%s)''',
        (snapshot['id'], snapshot['revision'], action, actor['id'], digest(snapshot), Json(snapshot)))


def create_issue(database, payload, actor):
    item = new_issue(payload, actor)
    item['id'] = str(uuid4())
    with database.transaction() as c:
        inserted = c.execute('''INSERT INTO quant.strategy_governance_items(id,dedupe_key,revision,state,snapshot)
            VALUES(%s,%s,%s,%s,%s) ON CONFLICT(dedupe_key) DO NOTHING RETURNING id''',
            (item['id'], payload['dedupe_key'], 1, item['state'], Json(item))).fetchone()
        if not inserted:
            return c.execute('SELECT snapshot FROM quant.strategy_governance_items WHERE dedupe_key=%s',
                             (payload['dedupe_key'],)).fetchone()['snapshot']
        _event(c, item, 'discover', actor)
    return item


def _get(c, item_id, *, lock=False):
    row = c.execute('SELECT snapshot FROM quant.strategy_governance_items WHERE id=%s' + (' FOR UPDATE' if lock else ''),
                    (item_id,)).fetchone()
    require(row is not None, 'Issue not found')
    return row['snapshot']


def get_issue(database, item_id):
    with database.transaction() as c:
        return _get(c, item_id)


def _save(c, item, action, actor):
    c.execute('UPDATE quant.strategy_governance_items SET revision=%s,state=%s,snapshot=%s,updated_at=now() WHERE id=%s',
              (item['revision'], item['state'], Json(item), item['id']))
    _event(c, item, action, actor)
    c.execute('DELETE FROM quant.strategy_governance_leases WHERE item_id=%s', (item['id'],))


def transition(database, item_id, expected_revision, action, payload, actor):
    with database.transaction() as c:
        item = _get(c, item_id, lock=True)
        if action == 'propose':
            from .proposals import build_proposal
            require(item['revision']==expected_revision,'Proposal revision conflict')
            baseline=_latest(c)
            changed=build_proposal(item,payload,actor,baseline['config'],baseline['generation'])
            _save(c,changed,'propose',actor)
            return changed
        if action == 'design':
            baseline = _latest(c)
            require(payload['spec']['baseline_code_hash'] == code_fingerprint(), 'Frozen baseline code does not match executing strategy modules')
            require(payload['spec']['baseline_config_hash'] == digest(baseline['config']), 'Frozen baseline config differs from active config')
            require(payload['spec']['baseline_generation'] == baseline['generation'], 'Frozen baseline generation is stale')
            check_config_scope(payload['spec'], baseline['config'])
            if item.get('proposal'):
                proposal=item['proposal']
                require(proposal['baseline_generation']==baseline['generation'] and proposal['baseline_config_hash']==digest(baseline['config']),'Proposal baseline changed; independent rework required')
                require(payload['spec']['candidate_config']['ranking_factors']==proposal['candidate_profile'],'Designer cannot silently alter accepted proposal')
                require(payload['spec'].get('requires_effectiveness_validation',False)==proposal['requires_effectiveness_validation'],'Cannot downgrade required effectiveness validation')
        if action == 'observe':
            verify_measurement(payload)
            sessions = sorted(set(payload.get('sessions', [])))
            calendar = c.execute('''SELECT calendar_date FROM quant.market_trade_calendar
                WHERE exchange IN ('SSE','SZSE') AND is_open=true AND calendar_date=ANY(%s::date[])
                GROUP BY calendar_date''', (sessions,)).fetchall()
            require({str(r['calendar_date']) for r in calendar} == set(sessions), 'Observation includes unverified exchange sessions')
        elif action == 'validate':
            require(item['current_experiment'] is not None, 'No experiment to validate')
            verify_measurement(item['experiments'][item['current_experiment']]['evidence'])
        if action in ('experiment', 'ready'):
            for dep in item['dependencies']:
                require(dep != item_id, 'Self dependency prohibited')
                parent = _get(c, dep)
                require(parent['state'] in ('validated', 'ready', 'activated'), 'Dependency not validated: ' + dep)
        changed = advance(item, expected_revision, action, payload, actor)
        _save(c, changed, action, actor)
    return changed


def resolve_active_config(database):
    with database.transaction() as c:
        row = c.execute('''SELECT generation,config,artifact_hash,item_id,recorded_at,code_hash
            FROM quant.strategy_governance_activations ORDER BY generation DESC LIMIT 1''').fetchone()
    return {**dict(row), 'status':'active' if row['code_hash']==code_fingerprint() else 'stale_code'} if row else None


def list_items(database, limit=100):
    with database.transaction() as c:
        rows = c.execute('SELECT snapshot FROM quant.strategy_governance_items ORDER BY updated_at DESC LIMIT %s',
                         (max(1, min(200, limit)),)).fetchall()
    return [row['snapshot'] for row in rows]


def _latest(c):
    row = c.execute('SELECT generation,config,code_hash FROM quant.strategy_governance_activations ORDER BY generation DESC LIMIT 1').fetchone()
    return row or {'generation': 0, 'config': environment_config(), 'code_hash':code_fingerprint()}


def activate(database, item_id, revision, artifact_hash, actor):
    """Call only after interactive human CLI challenge; application cannot isolate a hostile host admin."""
    with database.transaction() as c:
        c.execute("SELECT pg_advisory_xact_lock(hashtext('strategy_governance_active_config'))")
        item = _get(c, item_id, lock=True)
        old = _latest(c)
        spec = item['experiments'][item['current_experiment']]['spec'] if item['current_experiment'] is not None else {}
        if item['current_experiment'] is not None:
            verify_measurement(item['experiments'][item['current_experiment']]['evidence'])
        require(spec.get('baseline_code_hash') == code_fingerprint(), 'Executing strategy code changed; approval is stale')
        require(spec.get('baseline_config_hash') == digest(old['config']), 'Executing baseline config changed; approval is stale')
        check_config_scope(spec, old['config'])
        config = approval_config(item, revision, artifact_hash, old['generation'], actor)
        for dep in item['dependencies']:
            require(_get(c, dep)['state'] == 'activated', 'Activation dependency not active: ' + dep)
        activation = c.execute('''INSERT INTO quant.strategy_governance_activations
            (item_id,revision,artifact_hash,actor_id,action,config,previous_config,code_hash,previous_generation,reason)
            VALUES(%s,%s,%s,%s,'activate',%s,%s,%s,%s,%s) RETURNING generation''',
            (item_id, revision, artifact_hash, actor['id'], Json(config), Json(old['config']), code_fingerprint(), old['generation'], 'Explicit human CLI approval')).fetchone()
        item['state'] = 'activated'; item['revision'] += 1; item['live_effect'] = 'ranking_config'
        item['activation_generation'] = activation['generation']
        _save(c, item, 'human_activate', actor)
    return item


def rollback(database, target_generation, expected_generation, actor, reason):
    actor_role(actor, 'human')
    require(bool(reason.strip()), 'Rollback reason required')
    with database.transaction() as c:
        c.execute("SELECT pg_advisory_xact_lock(hashtext('strategy_governance_active_config'))")
        old = _latest(c)
        require(old['generation'] == expected_generation, 'Active generation changed')
        target = c.execute('SELECT config,artifact_hash FROM quant.strategy_governance_activations WHERE generation=%s',
                           (target_generation,)).fetchone() if target_generation else c.execute('''SELECT previous_config AS config,
                           artifact_hash FROM quant.strategy_governance_activations ORDER BY generation LIMIT 1''').fetchone()
        require(target is not None and target_generation < expected_generation, 'Rollback target must precede current generation')
        return dict(c.execute('''INSERT INTO quant.strategy_governance_activations
            (artifact_hash,actor_id,action,config,previous_config,code_hash,previous_generation,reason)
            VALUES(%s,%s,'rollback',%s,%s,%s,%s,%s) RETURNING generation,config,artifact_hash''',
            (target['artifact_hash'], actor['id'], Json(target['config']), Json(old['config']), code_fingerprint(), old['generation'], reason)).fetchone())


ROLE_STATES = {'reviewer':'discovered', 'proposer':'reviewed', 'designer':'proposed', 'implementer':'designed',
               'evaluator':'experiment', 'validator':'observing', 'release_preparer':'validated'}


def claim_work(database, actor, role, *, minutes=20, include_waiting=False, item_ids=None):
    """One bounded lease. No model starts here; independent worker dispatch is external."""
    actor_role(actor, role)
    require(role in ROLE_STATES and 1 <= minutes <= 60, 'Unsupported queue role/lease')
    require(item_ids is None or (isinstance(item_ids,list) and len(item_ids)<=200 and all(isinstance(i,str) and i for i in item_ids)), 'Invalid bounded item IDs')
    if item_ids == []:
        return {'status':'empty','item':None}
    with database.transaction() as c:
        rows = c.execute('''SELECT i.snapshot FROM quant.strategy_governance_items i
            LEFT JOIN quant.strategy_governance_leases l ON i.id=l.item_id
            WHERE i.state=ANY(%s) AND (%s::text[] IS NULL OR i.id=ANY(%s::text[])) AND (l.item_id IS NULL OR l.expires_at<now())
            ORDER BY i.created_at LIMIT 50 FOR UPDATE OF i SKIP LOCKED''',
            ([ROLE_STATES[role]] + (['observing'] if role=='evaluator' and include_waiting else []) + (['reviewed'] if role=='designer' else []),item_ids,item_ids)).fetchall()
        for row in rows:
            item = row['snapshot']; a = item['actors']
            if role=='designer' and item['state']=='reviewed' and not item['issue'].get('effectiveness_request'):continue
            if role == 'validator' and item['experiments'][item['current_experiment']]['comparison']['status'] != 'passed': continue
            if role == 'reviewer' and actor['id'] == a['observer']: continue
            if role == 'proposer' and actor['id'] in a.values(): continue
            if role == 'designer' and actor['id'] == a.get('proposer'): continue
            if role == 'implementer' and actor['id'] == a.get('reviewer'): continue
            if role == 'evaluator' and actor['id'] == a.get('implementer'): continue
            if role == 'validator' and actor['id'] in a.values(): continue
            expiry = now_utc() + timedelta(minutes=minutes)
            c.execute('''INSERT INTO quant.strategy_governance_leases(item_id,revision,actor_id,expires_at)
                VALUES(%s,%s,%s,%s) ON CONFLICT(item_id) DO UPDATE SET revision=EXCLUDED.revision,
                actor_id=EXCLUDED.actor_id,expires_at=EXCLUDED.expires_at''',
                (item['id'], item['revision'], actor['id'], expiry))
            return {'status':'claimed', 'item':item, 'expires_at':expiry.isoformat()}
    return {'status':'empty', 'item':None}
