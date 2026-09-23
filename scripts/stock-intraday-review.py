"""Validate and render a company-reviewed intraday decision without DB writes."""
import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'quant-service'))


def main():
    sys.stdout.reconfigure(encoding='utf-8')
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run-dir', type=Path, required=True)
    parser.add_argument('--review-file', type=Path, required=True)
    args = parser.parse_args()
    from app.intraday_scan.noon_review import render
    run_dir = args.run_dir.resolve()
    result = json.loads((run_dir / 'result.json').read_text(encoding='utf-8'))
    receipt = json.loads((run_dir / 'receipt.json').read_text(encoding='utf-8'))
    review = json.loads(args.review_file.read_text(encoding='utf-8'))
    text, coverage = render(result, receipt, review)
    destination = run_dir / 'midday-decision.md'
    destination.write_text(text, encoding='utf-8')
    if destination.read_text(encoding='utf-8') != text:
        raise ValueError('Decision report readback mismatch')
    print(json.dumps({'status': coverage['status'], 'reviewed': coverage['reviewed'],
                      'required': coverage['required'], 'missing': coverage['missing'],
                      'report': str(destination)}, ensure_ascii=False))
    if coverage['status'] != 'complete':
        raise SystemExit(2)


if __name__ == '__main__':
    main()
