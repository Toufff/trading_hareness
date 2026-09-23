from contextlib import contextmanager
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from app.intraday_scan.rules import digest
from app.recommendation_pool import noon


TZ = ZoneInfo('Asia/Shanghai')
DAY = '2026-09-23'


class _Connection:
    def __init__(self, row):
        self.row = row

    def execute(self, _sql, _params):
        return self

    def fetchone(self):
        return self.row


class _Database:
    def __init__(self, row):
        self.row = row

    @contextmanager
    def transaction(self):
        yield _Connection(self.row)


def _row(cutoff='11:30', implementation='same-version'):
    data = {'cutoff': f'{DAY}T{cutoff}:00+08:00', 'rows': []}
    result = {'input_hash': digest(data), 'implementation_hash': implementation,
              'lanes': [{'key': 'trend', 'items': [
                  {'symbol': '001232.SZ', 'minute_end': '1130', 'ohlc': [{}] * 40,
                   'state': 'wait_confirmation'}]}]}
    return {'run_id': 'run-1', 'cutoff': datetime.fromisoformat(data['cutoff']),
            'state': 'completed', 'input': data, 'result': result}


def test_load_requires_exact_completed_1130_and_freezes_stock_evidence(monkeypatch):
    monkeypatch.setattr(noon.engine, 'implementation_hash', lambda: 'same-version')
    monkeypatch.setattr(noon.engine, 'formal', lambda data: {'status': 'completed', 'as_of_date': DAY})
    scan, result, data, eligibility, cutoff = noon.load(_Database(_row()), 'run-1', DAY)
    assert cutoff.strftime('%H%M') == '1130'
    assert eligibility['001232.SZ']['ready'] is True
    assert scan['as_of_date'] == DAY
    assert result['input_hash'] == digest(data)
    with pytest.raises(ValueError, match='not_a_noon_scan'):
        noon.load(_Database(_row('11:25')), 'run-1', DAY)


def test_noon_load_does_not_reinterpret_an_old_run_after_engine_change(monkeypatch):
    monkeypatch.setattr(noon.engine, 'implementation_hash', lambda: 'new-version')
    with pytest.raises(ValueError, match='noon_scan_implementation_changed'):
        noon.load(_Database(_row()), 'run-1', DAY)


def test_noon_decision_cannot_be_backdated_after_the_afternoon_close():
    context = {'source_kind': 'noon', 'valid_until': f'{DAY}T15:00:00+08:00'}
    with pytest.raises(ValueError, match='noon_decision_window_expired'):
        noon.persist(_Database(None), context, {}, now=datetime(2026, 9, 23, 15, 1, tzinfo=TZ))


def test_publish_rechecks_frozen_scan_cutoff_and_engine(monkeypatch):
    monkeypatch.setattr(noon, 'compile_decision', lambda *_: {'reviewed': []})
    monkeypatch.setattr(noon.engine, 'implementation_hash', lambda: 'new-version')
    context = {'source_kind': 'noon', 'source_cutoff': f'{DAY}T11:30:00+08:00',
               'as_of_date': DAY, 'valid_until': f'{DAY}T15:00:00+08:00', 'run_id': 'run-1'}
    review = {'reviewed_at': f'{DAY}T12:01:00+08:00'}
    now = datetime.fromisoformat(f'{DAY}T12:02:00+08:00')
    with pytest.raises(ValueError, match='noon_scan_implementation_changed_during_review'):
        noon.persist(_Database(_row()), context, review, now=now)
    context['source_cutoff'] = f'{DAY}T11:25:00+08:00'
    with pytest.raises(ValueError, match='noon_source_cutoff_changed_during_review'):
        noon.persist(_Database(_row()), context, review, now=now)
