"""Write the human-first post-close review page from the persisted run.

Reads the owner's /api/v1/strategy/post-close/latest for the date and writes
<date>_post_close_review.html next to the markdown bundle. No recomputation.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'quant-service'))
from app.short_term_lanes.review_page import write  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument('--date', required=True)
    parser.add_argument('--base-url', default='http://127.0.0.1:5681')
    parser.add_argument('--report-dir', type=Path, required=True)
    parser.add_argument('--expected-run-id', default='')
    args = parser.parse_args()
    session = requests.Session()
    session.trust_env = False
    response = session.get(args.base_url + '/api/v1/strategy/post-close/latest', params={'as_of_date': args.date}, timeout=30)
    response.raise_for_status()
    payload = response.json()
    run = payload.get('run') or {}
    if run.get('as_of_date') != args.date:
        raise SystemExit(json.dumps({'status': 'skipped', 'reason': 'persisted run is not the requested date', 'run_date': run.get('as_of_date')}, ensure_ascii=False))
    if args.expected_run_id and run.get('run_id') != args.expected_run_id:
        raise SystemExit(json.dumps({'status': 'skipped', 'reason': 'persisted run id differs from the published scan', 'run_id': run.get('run_id')}, ensure_ascii=False))
    target = write(args.report_dir, payload)
    print(json.dumps({'status': 'written', 'as_of_date': args.date, 'run_id': run.get('run_id'), 'file': str(target)}, ensure_ascii=False))


if __name__ == '__main__':
    main()
