"""Verify immutable local measurement artifacts before accepting an observation."""
import hashlib
import json
from pathlib import Path
from .rules import require


def verify_measurement(payload):
    path = Path(payload['measurement_artifact'])
    require(path.is_file(), 'Measurement artifact is missing')
    require(hashlib.sha256(path.read_bytes()).hexdigest() == payload['measurement_hash'], 'Measurement artifact hash mismatch')
    measured = json.loads(path.read_text(encoding='utf-8-sig'))
    for key in ['input_hash', 'baseline_code_hash', 'candidate_code_hash', 'holdout_id', 'sessions', 'observations', 'metrics']:
        require(measured.get(key) == payload.get(key), 'Evidence differs from measurement artifact: ' + key)
