import json
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

import pytest

from app.short_term_lanes.publication import difference_paths, export_persisted
from app.short_term_lanes.reports import make_bundle
from test_short_term_lane_reports import example


def payload():
    result = example()
    result['information_cutoff'] = '2026-09-04T22:40:40+08:00'
    result['event_research'] = {'age_seconds': 12, 'stale': False}
    result['report_bundle'] = make_bundle(result)
    return {'run': {'run_id': 'saved-run', 'as_of_date': result['as_of_date'],
                    'summary': {'strategy_lanes': result}}}


def test_export_uses_exact_persisted_generation_and_replaces_older_scan(tmp_path):
    data = payload()
    path = tmp_path / '2026-09-04_short_term_lanes.json'
    path.write_text('{"information_cutoff":"earlier-cli-scan"}')
    with patch('app.short_term_lanes.service.build', side_effect=AssertionError('must not re-scan')):
        receipt = export_persisted(data, '2026-09-04', tmp_path, expected_run_id='saved-run')
    assert receipt['status'] == 'exported'
    assert json.loads(path.read_text(encoding='utf-8')) == data['run']['summary']['strategy_lanes']
    assert export_persisted(data, '2026-09-04', tmp_path) == receipt


@pytest.mark.parametrize('change', ['date', 'run', 'source', 'body'])
def test_export_rejects_wrong_or_corrupt_generation_before_writing(tmp_path, change):
    data = deepcopy(payload())
    result = data['run']['summary']['strategy_lanes']
    if change == 'date': data['run']['as_of_date'] = '2026-09-03'
    if change == 'run': data['run']['run_id'] = 'other'
    if change == 'source': result['information_cutoff'] = 'modified'
    if change == 'body': result['report_bundle']['reports'][0]['markdown'] += 'corrupt'
    with pytest.raises(ValueError):
        export_persisted(data, '2026-09-04', tmp_path, expected_run_id='saved-run')
    assert not list(tmp_path.iterdir())


def test_diagnostics_are_bounded_paths_not_sensitive_values():
    assert difference_paths({'a': {'token': 'secret'}, 'b': 1}, {'a': {}, 'b': 2}, limit=1) == ['/a/token']


def test_collect_only_never_builds_or_exports(monkeypatch):
    from app.short_term_lanes.service import main
    monkeypatch.setattr('sys.argv', ['service', '--date', '2026-09-04', '--collect-only'])
    with patch('app.database.Database'), patch('app.short_term_lanes.collect.collect') as collect, \
         patch('app.short_term_lanes.service.build') as build, \
         patch('app.short_term_lanes.service.write_bundle') as write:
        main()
    collect.assert_called_once()
    build.assert_not_called()
    write.assert_not_called()


def test_scheduled_entry_collects_then_builds_once_and_exports_before_verifying():
    root = Path(__file__).resolve().parents[2]
    script = (root / 'scripts/windows/run-post-close-pipeline.ps1').read_text(encoding='utf-8')
    start = script.index("$stage = 'strategy_collection_and_screen'")
    section = script[start:]
    assert '--collect-only' in section
    assert '--collect --output-dir' not in section
    assert section.index('/api/v1/strategy/post-close/run') < section.index('export-strategy-publication.py')
    assert section.index('export-strategy-publication.py') < section.index('verify-short-term-lanes.py')
    assert section.count('/api/v1/strategy/post-close/run') == 1
    assert 'close-strategy-research.py' not in section
    assert 'decision_research' not in section
    assert '--reports-only' in section


def test_same_date_retry_only_verifies_and_never_redispatches_governance():
    root = Path(__file__).resolve().parents[2]
    script = (root / 'scripts/windows/run-post-close-pipeline.ps1').read_text(encoding='utf-8')
    preflight = script[script.index("if (-not $Force -and (Test-EquityDateReady"):script.index("$stage = 'equity_ingestion'")]
    assert "strategy-report-bundle-results-first-2026-09-11" in preflight
    assert 'verify-short-term-lanes.py' in preflight
    assert 'Start-IndependentGovernance' not in preflight


def test_new_strategy_runtime_does_not_import_retired_gates():
    root = Path(__file__).resolve().parents[2]
    active_paths = [
        root / 'quant-service/app/short_term_lanes',
        root / 'quant-service/app/recommendation_pool',
        root / 'quant-service/app/post_close_refresh_service.py',
        root / 'quant-service/app/routers/personal_decisions.py',
        root / 'scripts/windows/run-post-close-pipeline.ps1',
        root / 'frontend/src/composables/usePersonalDecisionWorkspace.ts',
        root / 'frontend/src/views/PersonalDecisionView.vue',
    ]
    offenders = []
    for path in active_paths:
        files = path.rglob('*.py') if path.is_dir() else [path]
        for file in files:
            body = file.read_text(encoding='utf-8')
            if 'decision_research' in body or 'G0' in body or 'G7' in body:
                offenders.append(str(file.relative_to(root)))
    assert offenders == []
