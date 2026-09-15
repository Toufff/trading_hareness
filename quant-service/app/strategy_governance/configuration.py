"""Fingerprint the actual strategy modules and snapshot the environment baseline."""
from dataclasses import asdict
import hashlib
from pathlib import Path
from .rules import digest


def code_fingerprint(app_root=None):
    app = Path(app_root) if app_root else Path(__file__).resolve().parents[1]
    files = [*sorted((app/'short_term_lanes').glob('*.py')), *sorted((app/'ranking_factors').glob('*.py')),
             app/'short_term_liquidity.py']
    return digest({p.relative_to(app).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest() for p in files})


def environment_config():
    from ..short_term_lanes.service import configured_settings
    return {'ranking_factors': {'version':1, 'factors':[
        {'key':s.key, 'weight':s.weight, 'strategies':list(s.strategies)}
        for s in configured_settings().ranking_factors]}}
