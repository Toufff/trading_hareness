import copy
import json
from fastapi import FastAPI
from fastapi.testclient import TestClient
from app.strategy_dashboard_projection import dashboard_post_close
from app.routers.strategy_reads import build_strategy_reads_router


def test_exact_duplicates_only_and_no_original_mutation():
    run = {'run_id': 'A', 'status': 'completed', 'summary': {'lanes': [{'symbol': 'X'}], 'full_evidence': 'x' * 10000}}
    source = dict(run=run, latest_attempt=run, latest_completed=copy.deepcopy(run), candidate_run=run, candidates=[{'symbol': 'X'}])
    original = copy.deepcopy(source)
    compact = dashboard_post_close(source)
    assert source == original
    assert compact['run'] == run
    assert compact['candidates'] == source['candidates']
    for key in ('latest_attempt', 'latest_completed', 'candidate_run'):
        assert compact[key]['summary_ref'] == 'run.summary'
        assert compact[key]['status'] == 'completed'
        assert 'summary' not in compact[key]
    assert len(json.dumps(compact)) < len(json.dumps(source)) * .3


def test_failed_latest_attempt_and_previous_complete_evidence_are_preserved():
    failed = {'run_id': 'B', 'status': 'failed', 'summary': {'reason': 'provider failed', 'errors': ['actual failure']}}
    completed = {'run_id': 'A', 'status': 'completed', 'summary': {'lanes': [{'symbol': 'X', 'reason': 'full evidence'}]}}
    compact = dashboard_post_close(dict(run=failed, latest_attempt=failed, latest_completed=completed, candidate_run=completed, candidates=[{'symbol': 'X'}]))
    assert compact['run'] == failed
    assert compact['latest_attempt']['status'] == 'failed'
    assert compact['latest_attempt']['summary_ref'] == 'run.summary'
    assert compact['latest_completed'] == completed
    assert compact['candidate_run']['summary_ref'] == 'latest_completed.summary'
    assert dashboard_post_close({'run': None, 'candidates': []})['run'] is None


def test_opt_in_route_keeps_default_full_and_rejects_unknown_view(monkeypatch):
    import app.routers.strategy_reads as routes
    run = {'run_id': 'A', 'summary': {'all_candidates': ['X']}, 'status': 'completed'}
    payload = {'run': run, 'latest_attempt': run, 'latest_completed': run, 'candidate_run': run, 'candidates': []}
    monkeypatch.setattr(routes, 'sync_latest_post_close_strategy', lambda *args: payload)
    app = FastAPI(); app.include_router(build_strategy_reads_router(object(), 'test'))
    client = TestClient(app)
    assert client.get('/api/v1/strategy/post-close/latest').json() == payload
    assert client.get('/api/v1/strategy/post-close/latest?view=full').json() == payload
    compact = client.get('/api/v1/strategy/post-close/latest?view=dashboard').json()
    assert compact['run'] == run and compact['latest_attempt']['summary_ref'] == 'run.summary'
    assert client.get('/api/v1/strategy/post-close/latest?view=unknown').status_code == 422
