"""Export the owner's persisted run; no scanner, provider or model invocation."""
import argparse
import json
from pathlib import Path
import sys

import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'quant-service'))
from app.short_term_lanes.publication import export_persisted


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--date', required=True)
    parser.add_argument('--expected-run-id', required=True)
    parser.add_argument('--base-url', default='http://127.0.0.1:5681')
    parser.add_argument('--report-dir', type=Path, required=True)
    args = parser.parse_args()
    session = requests.Session()
    session.trust_env = False
    response = session.get(args.base_url + '/api/v1/strategy/post-close/latest',
                           params={'as_of_date': args.date}, timeout=30)
    response.raise_for_status()
    print(json.dumps(export_persisted(response.json(), args.date, args.report_dir,
          expected_run_id=args.expected_run_id), ensure_ascii=False))


if __name__ == '__main__':
    main()
