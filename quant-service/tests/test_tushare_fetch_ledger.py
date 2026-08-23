from __future__ import annotations

from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock

from app.tushare_fetch_ledger import TushareFetchLedgerDependencies, persist_cancel, prepare_run


class _Transaction:
    def __init__(self, execute):
        self.execute = execute

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


class _ProviderError(Exception):
    pass


def _deps(database: MagicMock, **overrides) -> TushareFetchLedgerDependencies:
    values = {
        "database": database,
        "json_value": lambda value: value,
        "looks_like_response_header": lambda _rows: False,
        "normalize_cached_rows": MagicMock(return_value=1),
        "persist_rows": MagicMock(return_value=1),
        "record_provider_failure": MagicMock(),
        "record_provider_success": MagicMock(),
        "record_provider_capability": MagicMock(),
        "provider_error_availability": lambda _error: "failed",
        "provider_call_error": _ProviderError,
        "safe_error_detail": lambda value, *_args: value,
    }
    values.update(overrides)
    return TushareFetchLedgerDependencies(**values)


class TushareFetchLedgerTests(unittest.TestCase):
    def test_header_like_cached_evidence_is_not_normalized_or_reused(self) -> None:
        request = SimpleNamespace(
            api_name="daily", provider="super", fields=None, max_rows=100, paginate=False, page_size=100,
            max_pages=1, require_complete=False,
        )
        first = MagicMock(fetchone=MagicMock(return_value={"status": "completed", "row_count": 1}))
        second = MagicMock(fetchall=MagicMock(return_value=[{"provider_key": "tushare_super_get", "row_data": {"code": 403}}]))
        database = MagicMock()
        database.transaction.return_value = _Transaction(MagicMock(side_effect=[first, second]))
        normalizer = MagicMock()

        result = prepare_run(
            request, "request-key", ["tushare_super_get"], {"ts_code": "000001.SZ"},
            _deps(database, looks_like_response_header=lambda _rows: True, normalize_cached_rows=normalizer),
        )

        self.assertEqual(result["status"], "invalid_response")
        normalizer.assert_not_called()

    def test_caller_cancelled_transition_never_records_provider_failure(self) -> None:
        statements = []

        def execute(sql, params=()):
            statements.append((str(sql), params))
            return MagicMock()

        database = MagicMock()
        database.transaction.return_value = _Transaction(execute)
        deps = _deps(database)

        persist_cancel("request-key", "daily", ["tushare_primary"], deps)

        self.assertIn("caller_cancelled", statements[0][0])
        deps.record_provider_failure.assert_not_called()
        deps.record_provider_capability.assert_not_called()
