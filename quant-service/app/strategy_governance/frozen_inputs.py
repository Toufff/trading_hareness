"""Freeze the actual scan input once; no recollection, model fabrication or holdings.

Content-addressed files use the configured data volume, never the release tree.
Archive errors are isolated by the scan caller, not a reason to lose its report.
"""
from datetime import date
import hashlib
import json
import os
from pathlib import Path
import tempfile

from .review_evidence import sanitize
from .rules import require

MAX_BYTES=100_000_000


def _strict_day(value):
    value=str(value)
    parsed=date.fromisoformat(value)
    require(value in (parsed.isoformat(),parsed.strftime('%Y%m%d')),'Strict market date required')
    return parsed


def freeze_input(*, day, rows, sessions, events, price_histories, history_health,
                 information_cutoff=None, root=None):
    if root is None:
        configured=os.getenv('QUANT_DATA_DIR')
        if not configured:
            return {'status':'unconfigured','reason':'QUANT_DATA_DIR is required; no experiment input archived'}
        root=Path(configured)/'governance'/'inputs'
    root=Path(root).resolve()
    day=str(day)
    require(str(date.fromisoformat(day))==day,'Strict ISO scan date required')
    from ..short_term_lanes.event_time import resolve_cutoff
    cutoff=resolve_cutoff(day,information_cutoff).isoformat()
    require(rows and sessions,'Cannot freeze an empty market scan as an experiment input')
    require(all(_strict_day(d)<=date.fromisoformat(day) for d in sessions),'Future session in scan input')
    require(all(_strict_day(r.get('trade_date',''))<=date.fromisoformat(day) for r in rows),'Future row in scan input')
    require(all(_strict_day(r.get('date') or r.get('trade_date') or '')<=date.fromisoformat(day)
                for bars in price_histories.values() for r in bars),'Future OHLC in scan input')
    payload=dict(date=day,rows=rows,sessions=sessions,events=events,
                 price_histories=price_histories,history_health=history_health,
                 information_cutoff=cutoff,
                 timing_scope='next-session research; raw event archive is not proof of event eligibility')
    # Never alter market evidence silently just to satisfy a secret filter.
    # Reject suspect fields and let the producer supply a clean market schema.
    require(sanitize(payload)==payload,'Sensitive fields in experiment input; archive refused')
    raw=json.dumps(payload,ensure_ascii=False,sort_keys=True,allow_nan=False,default=str).encode('utf-8')
    require(len(raw)<=MAX_BYTES,'Experiment input exceeds bounded archive size')
    sha=hashlib.sha256(raw).hexdigest()
    directory=root/day;directory.mkdir(parents=True,exist_ok=True)
    path=directory/(sha+'.json')
    if not path.exists():
        temporary=None
        try:
            with tempfile.NamedTemporaryFile(dir=directory,suffix='.tmp',delete=False) as f:
                temporary=Path(f.name);f.write(raw);f.flush();os.fsync(f.fileno())
            try:
                os.link(temporary,path)  # Atomic publication; never overwrite an existing hash.
            except FileExistsError:
                pass
        finally:
            if temporary:temporary.unlink(missing_ok=True)
    require(hashlib.sha256(path.read_bytes()).hexdigest()==sha,'Existing experiment archive failed content hash verification')
    return {'status':'ready','input_path':str(path),'input_hash':sha,'as_of_date':day,
            'information_cutoff':cutoff,
            'bytes':len(raw),'rows':len(rows),'history_symbols':len(price_histories),
            'scope':'actual same-run screen input; historical rows are not independent experimental outcomes'}
