"""Local provisioned identities. This is a trusted-host boundary, not OS sandboxing."""
import json
import os
from pathlib import Path
from .rules import require


def resolve_actor(actor_id, registry_path=None):
    path = registry_path or os.getenv('STRATEGY_GOVERNANCE_ACTORS_FILE')
    require(bool(path), 'Set STRATEGY_GOVERNANCE_ACTORS_FILE to a locally provisioned registry')
    registry = json.loads(Path(path).read_text(encoding='utf-8-sig'))
    actor = registry.get('actors', {}).get(actor_id)
    require(isinstance(actor, dict) and actor.get('enabled') is True, 'Actor not provisioned/enabled')
    require(isinstance(actor.get('roles'), list), 'Actor roles must be provisioned')
    if 'human' in actor['roles']:
        require(actor.get('kind') == 'human' and actor['roles'] == ['human'], 'Human identity cannot carry agent roles')
    else:
        require(actor.get('kind') in ('agent', 'system'), 'Actor kind required')
    return {**actor, 'id': actor_id}
