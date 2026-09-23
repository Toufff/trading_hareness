import json
import subprocess
import sys
from pathlib import Path

import pytest

from app.intraday_scan.noon_review import render, validate
from app.intraday_scan.presentation import build as build_presentation
from app.intraday_scan.rules import digest


def fixture():
    row = dict(symbol='600001.SH', name='本轮首位', lane='trend', source='new_intraday',
               state='confirmed_observation', matched_today=True, current_reason='趋势增强')
    lane = dict(key='trend', label='趋势', items=[row])
    result = dict(cutoff='2026-09-23T11:30:00+08:00', lanes=[lane],
                  presentation=build_presentation([row], [lane]))
    receipt = dict(run_id='run-123', result_hash=digest(result), database_readback=True)
    review = dict(run_id='run-123', cutoff=result['cutoff'],
                  completed_at='2026-09-23T12:20:00+08:00',
                  market=dict(breadth='下跌多于上涨', sector_rotation='通信较弱',
                              turnover_flow='上午累计额，未与全天直接比较',
                              news='没有已核实的新催化', afternoon_scenarios='站稳再观察'),
                  comparison_summary='相对同板块尚不领先', limitations='午盘数据未结算',
                  companies=[dict(symbol='600001.SH', business='主营电子元件',
                                  fundamentals='最新业绩尚无改善证据', catalyst='未发现公告级催化',
                                  price_volume='上午放量，但需看下午承接',
                                  sector_relative='相对板块偏强', peer_comparison='同组第二名流动性更差',
                                  risk='高位回撤', disposition='watch', why_now='本轮策略首位',
                                  trigger='下午站稳参考线', invalidation='跌破上午低点',
                                  source_refs=['同轮行情 11:30', '公告检索 12:00'],
                                  evidence_available_at='2026-09-23T12:00:00+08:00')])
    return result, receipt, review


def test_complete_review_is_bound_to_exact_run_and_stocks_come_first():
    result, receipt, review = fixture()
    report, coverage = render(result, receipt, review)
    assert coverage['status'] == 'complete' and coverage['required'] == 1
    assert report.index('本轮首位') < report.index('## 市场、板块与下午情景')
    assert report.index('本轮首位') < report.index('## 范围与审计')
    assert '午盘后补充' in report
    assert '不是自动买入指令' in report


def test_missing_company_is_explicit_partial_not_complete():
    result, receipt, review = fixture()
    review['companies'] = []
    report, coverage = render(result, receipt, review)
    assert coverage['status'] == 'partial'
    assert coverage['missing'] == ['600001.SH']
    assert '研究未闭环' in report


def test_wrong_run_future_evidence_and_placeholder_fields_fail_closed():
    result, receipt, review = fixture()
    review['run_id'] = 'other'
    with pytest.raises(ValueError, match='exact scan'):
        validate(result, receipt, review)
    review['run_id'] = receipt['run_id']
    review['companies'][0]['evidence_available_at'] = '2026-09-23T12:21:00+08:00'
    with pytest.raises(ValueError, match='unavailable'):
        validate(result, receipt, review)
    review['companies'][0]['evidence_available_at'] = '2026-09-23T12:00:00+08:00'
    review['companies'][0]['risk'] = ''
    with pytest.raises(ValueError, match='risk is required'):
        validate(result, receipt, review)


def test_noon_review_refuses_unverified_scan_receipt():
    result, receipt, review = fixture()
    receipt['database_readback'] = False
    with pytest.raises(ValueError, match='database readback'):
        validate(result, receipt, review)


def test_cli_writes_readable_review_and_partial_exit_code(tmp_path):
    result, receipt, review = fixture()
    run_dir = tmp_path / 'scan'
    run_dir.mkdir()
    (run_dir / 'result.json').write_text(json.dumps(result, ensure_ascii=False), encoding='utf-8')
    (run_dir / 'receipt.json').write_text(json.dumps(receipt, ensure_ascii=False), encoding='utf-8')
    review_path = tmp_path / 'review.json'
    review_path.write_text(json.dumps(review, ensure_ascii=False), encoding='utf-8')
    script = Path(__file__).resolve().parents[2] / 'scripts' / 'stock-intraday-review.py'
    command = [sys.executable, str(script), '--run-dir', str(run_dir), '--review-file', str(review_path)]
    completed = subprocess.run(command, capture_output=True, text=True, encoding='utf-8')
    assert completed.returncode == 0, completed.stderr
    assert '本轮首位' in (run_dir / 'midday-decision.md').read_text(encoding='utf-8')
    review['companies'] = []
    review_path.write_text(json.dumps(review, ensure_ascii=False), encoding='utf-8')
    partial = subprocess.run(command, capture_output=True, text=True, encoding='utf-8')
    assert partial.returncode == 2
    assert '研究未闭环' in (run_dir / 'midday-decision.md').read_text(encoding='utf-8')
