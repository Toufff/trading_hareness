"""Append only distinct job/wait evidence; repeated empty polling consumes no rows."""
from psycopg.types.json import Json
import re
from .rules import digest,require
from .review_evidence import sanitize


def record_diagnostic(database,item_id,payload):
    keys=('status','role','reason','system_owner','next_step','evidence_hash','model_started')
    value={k:payload.get(k) for k in keys}
    require(all(isinstance(value[k],str) and value[k] for k in ('status','role','reason','system_owner','next_step')),'Diagnostic requires actionable bounded text')
    require(type(value['model_started']) is bool,'model_started must be factual boolean')
    value=sanitize(value)
    for key in keys:
        if isinstance(value[key],str):
            value[key]=re.sub(r'(?i)\b[A-Z]:[\\/][^\s"\'<>]+','[LOCAL_PATH]',value[key])[:2000]
    with database.transaction() as c:
        row=c.execute('SELECT revision FROM quant.strategy_governance_items WHERE id=%s',(item_id,)).fetchone()
        require(row is not None,'Diagnostic issue missing')
        value['issue_revision']=row['revision'];fingerprint=digest(value)
        inserted=c.execute('''INSERT INTO quant.strategy_governance_diagnostics(item_id,fingerprint,evidence)
            VALUES(%s,%s,%s) ON CONFLICT DO NOTHING RETURNING recorded_at''',(item_id,fingerprint,Json(value))).fetchone()
    return {'status':'recorded' if inserted else 'unchanged','fingerprint':fingerprint}
