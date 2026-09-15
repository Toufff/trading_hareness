"""Project the persisted scan to files; never re-score or refresh evidence here."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from .reports import write_bundle


def difference_paths(left, right, *, limit=30):
    """Bounded field paths only: diagnostics must not dump report/credential values."""
    paths = []

    def visit(a, b, path):
        if len(paths) >= limit or a == b:
            return
        if isinstance(a, dict) and isinstance(b, dict):
            for key in sorted(a.keys() | b.keys()):
                child = f'{path}/{key}'
                if key not in a or key not in b:
                    if len(paths) < limit:
                        paths.append(child)
                else:
                    visit(a[key], b[key], child)
        elif isinstance(a, list) and isinstance(b, list) and len(a) == len(b):
            for index, (x, y) in enumerate(zip(a, b)):
                visit(x, y, f'{path}/{index}')
        else:
            paths.append(path or '/')

    visit(left, right, '')
    return paths


def export_persisted(payload, day, directory: Path, *, expected_run_id=None):
    run = payload.get('run') or {}
    result = (run.get('summary') or {}).get('strategy_lanes') or {}
    if run.get('as_of_date') != day or result.get('as_of_date') != day:
        raise ValueError('publication_date_mismatch')
    if expected_run_id and str(run.get('run_id')) != str(expected_run_id):
        raise ValueError('publication_run_mismatch')
    if result.get('status') != 'completed':
        raise ValueError('publication_scan_not_completed')
    bundle = result.get('report_bundle') or {}
    source = {key: value for key, value in result.items() if key != 'report_bundle'}
    digest = hashlib.sha256(json.dumps(source, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    if bundle.get('source_sha256') != digest:
        raise ValueError('publication_source_hash_mismatch')
    reports = bundle.get('reports') or []
    if [r['key'] for r in reports] != ['overview', *[lane['key'] for lane in result['lanes']]]:
        raise ValueError('publication_report_catalog_mismatch')
    if any(r.get('as_of_date') != day or
           hashlib.sha256(r['markdown'].encode()).hexdigest() != r.get('content_sha256')
           for r in reports):
        raise ValueError('publication_report_hash_mismatch')
    write_bundle(directory, result, bundle)
    saved = json.loads((directory / f'{day}_short_term_lanes.json').read_text(encoding='utf-8'))
    if saved != result:
        raise ValueError('publication_disk_readback_mismatch')
    return dict(status='exported', run_id=run['run_id'], as_of_date=day,
                source_sha256=digest, report_count=len(reports))
