from app.trade_thesis.report import sections


def test_failed_followup_is_human_readable_and_hides_internal_names():
    lines = sections({
        "status": "failed",
        "source_run_id": "run-1",
        "error_type": "ValueError",
        "reason_code": "thesis_scope_exceeds_500_page_required",
        "user_message": "旧候选跟踪未完成；主扫描结果仍然有效，详细原因已写入运行日志。",
    })
    text = "\n".join(lines)
    assert "旧候选跟踪未完成" in text
    assert "ValueError" not in text
    assert "thesis_scope_exceeds_500_page_required" not in text
    assert "source_run_id" not in text


def test_partial_followup_summarizes_page_and_symbol_failures_without_raw_error_types():
    lines = sections({
        "status": "partial",
        "source_run_id": "run-2",
        "cutoff_at": "2026-09-21T14:15:00+08:00",
        "data_date": "2026-09-21",
        "items": [],
        "scope": {"pages": 2},
        "page_failures": [{"page": 2, "size": 31, "error_type": "RuntimeError"}],
        "failures": [{"symbol": "600000.SH", "error_type": "market_page_unavailable"}],
    })
    text = "\n".join(lines)
    assert "状态：部分完成" in text
    assert "共分2批读取，其中1批未完成" in text
    assert "部分股票跟踪未完成，共1只" in text
    assert "RuntimeError" not in text
    assert "market_page_unavailable" not in text
    assert "source_run_id" not in text
