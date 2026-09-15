"""Freeze host proposal once per issue revision and code/config, not per retry."""
import json
import os
from pathlib import Path
from .governance_runner import prepare_context,runner_hash,validate_registered
from ..strategy_governance.configuration import code_fingerprint
from ..strategy_governance.experiment_runner import _baseline
from ..strategy_governance.rules import digest,require
from ..strategy_governance.review_evidence import evidence_root


def context(database,item,root=None):
    config,generation=_baseline(database)
    key=digest([item['id'],item['revision'],item['issue'].get('effectiveness_request'),
        code_fingerprint(),runner_hash(),config,generation])
    root=Path(root or evidence_root())/'effectiveness-protocols';root.mkdir(parents=True,exist_ok=True)
    path=root/(key+'.json')
    if not path.exists():
        value=prepare_context(database,item)
        raw=json.dumps(value,ensure_ascii=False,sort_keys=True,allow_nan=False).encode('utf-8')
        # Atomic appearance prevents a concurrent reader seeing partial JSON.
        from uuid import uuid4
        temp=root/(key+'.'+uuid4().hex+'.tmp')
        try:
            temp.write_bytes(raw)
            try:os.link(temp,path)
            except FileExistsError:pass
        finally:temp.unlink(missing_ok=True)
    value=json.loads(path.read_bytes())
    require(value['item_id']==item['id'] and value['spec_hash']==digest(value['spec']),'Cached proposal changed')
    validate_registered(value['spec'])
    return value
