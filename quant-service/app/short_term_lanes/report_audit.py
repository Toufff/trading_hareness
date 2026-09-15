"""Readback checks for a persisted report bundle, independent of stock scoring."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path


def expected_summary_symbols(lane):
    original = lane['selected'] + lane.get('caution_list', [])
    shown = {r['symbol'] for r in original}
    return [r['symbol'] for r in lane.get('observation_list', []) if r['symbol'] not in shown] + [r['symbol'] for r in original]


def check_bundle(result: dict, directory: Path) -> dict[str, bool]:
    bundle = result.get('report_bundle') or {}
    reports = bundle.get('reports', [])
    lanes = result['lanes']
    expected = ['overview', *[lane['key'] for lane in lanes]]
    source = {k: v for k, v in result.items() if k != 'report_bundle'}
    source_hash = hashlib.sha256(json.dumps(source, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    names = [r['filename'] for r in reports]
    safe = len(set(names)) == len(names) and all(Path(name).name == name for name in names)
    exists = safe and all((directory / name).is_file() for name in names)
    by_key = {r['key']: r for r in reports}
    overview = by_key.get('overview', {}).get('markdown', '')
    checks = {
        'bundle_all_strategies_and_overview': [r['key'] for r in reports] == expected,
        'bundle_source_hash': bundle.get('source_sha256') == source_hash,
        'bundle_same_date': bool(reports) and all(r['as_of_date'] == result['as_of_date'] for r in reports),
        'bundle_unique_safe_files': safe and exists and bool(names),
        'bundle_content_hashes': bool(reports) and all(
            hashlib.sha256(r['markdown'].encode()).hexdigest() == r['content_sha256'] for r in reports),
        'bundle_results_first': bool(reports) and all(
            next((line for line in r['markdown'].splitlines() if line.startswith('## ')), '') == '## 本次结论'
            for r in reports),
        'bundle_result_summary_scoped': all(lane['key'] in by_key and
            by_key[lane['key']].get('result_summary', {}).get('key') == lane['key'] and
            [row['symbol'] for row in by_key[lane['key']]['result_summary']['rows']] ==
            expected_summary_symbols(lane) for lane in lanes),
        'bundle_disk_equals_api': bool(reports) and exists and all(
            (directory / r['filename']).read_text(encoding='utf-8') == r['markdown'] for r in reports),
        'bundle_overview_links_all_strategies': bool(reports) and all(
            f"]({r['filename']})" in overview for r in reports if r['key'] != 'overview'),
        'bundle_strategy_details_complete': all(lane['key'] in by_key and all(
            all(str(value) in by_key[lane['key']]['markdown'] for value in (
                f"{row['name']}（{row['symbol'].split('.')[0]}）", row['reason'],
                row['confirmation'], row['invalidation'], row['expiry'])) for row in lane['selected'] + lane.get('observation_list', [])) for lane in lanes),
        'bundle_empty_strategies_explained': all(lane['key'] in by_key and
            lane['empty_reason'] in by_key[lane['key']]['markdown'] for lane in lanes if not lane['selected']),
        'bundle_strategy_reviews_scoped': all(lane['key'] in by_key and all(
            review['symbol'] in {r['symbol'] for r in lane['selected'] + lane.get('caution_list', [])}
            for group in by_key[lane['key']]['review']['review_groups'] for review in group['items']) for lane in lanes),
    }
    return checks
