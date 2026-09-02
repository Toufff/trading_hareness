from __future__ import annotations

import unittest

from app.provider_health import record_provider_api_capability, record_provider_empty_result, record_provider_failure
from app.tushare_providers import ProviderRateLimitedError, ProviderUnauthorizedError, classify_provider_error_text


class _RecordingConnection:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple]] = []

    def execute(self, statement, params=()):
        self.calls.append((statement, params))


class RecordProviderEmptyResultTests(unittest.TestCase):
    def test_does_not_touch_consecutive_failures_column(self) -> None:
        connection = _RecordingConnection()
        record_provider_empty_result(connection, "tushare_super_get", "daily_all_a", 120)
        statement, params = connection.calls[0]
        self.assertNotIn("consecutive_failures=0", statement.split("DO UPDATE SET", 1)[1])
        self.assertIn("last_row_count=0", statement)
        self.assertEqual(params, ("tushare_super_get", "daily_all_a", 120))


class RecordProviderFailureClassificationTests(unittest.TestCase):
    def test_default_path_increments_consecutive_failures_unchanged(self) -> None:
        connection = _RecordingConnection()
        record_provider_failure(connection, "tushare_primary", "daily", "boom", 50)
        statement, _params = connection.calls[0]
        self.assertIn("consecutive_failures=quant.provider_health.consecutive_failures+1", statement)

    def test_rate_limited_does_not_increment_consecutive_failures(self) -> None:
        connection = _RecordingConnection()
        record_provider_failure(
            connection, "tushare_super_get", "daily", "HTTP 429: too many requests", 50,
            error_class="rate_limited", retry_after_seconds=45,
        )
        statement, params = connection.calls[0]
        self.assertNotIn("consecutive_failures=quant.provider_health.consecutive_failures+1", statement)
        self.assertIn("circuit_open_until=now() + (%s * interval '1 second')", statement)
        self.assertEqual(params, ("tushare_super_get", "daily", "rate_limited: HTTP 429: too many requests", 50, 45.0, 45.0))

    def test_rate_limited_retry_after_is_bounded(self) -> None:
        connection = _RecordingConnection()
        record_provider_failure(
            connection, "tushare_super_get", "daily", "HTTP 429", retry_after_seconds=99999,
            error_class="rate_limited",
        )
        _statement, params = connection.calls[0]
        self.assertEqual(params[-1], 300.0)

    def test_unauthorized_does_not_increment_consecutive_failures(self) -> None:
        connection = _RecordingConnection()
        record_provider_failure(
            connection, "tushare_backup", "daily", "HTTP 401: invalid token", 10, error_class="unauthorized",
        )
        statement, params = connection.calls[0]
        self.assertNotIn("consecutive_failures=quant.provider_health.consecutive_failures+1", statement)
        self.assertIn("circuit_open_until=now() + interval '30 minutes'", statement)
        self.assertEqual(params, ("tushare_backup", "daily", "unauthorized: HTTP 401: invalid token", 10))


class ClassifyProviderErrorTextTests(unittest.TestCase):
    def test_http_429_is_rate_limited(self) -> None:
        error_cls, retry_after = classify_provider_error_text("provider: HTTP 429: too many requests")
        self.assertIs(error_cls, ProviderRateLimitedError)
        self.assertIsNone(retry_after)

    def test_retry_after_hint_is_parsed(self) -> None:
        _error_cls, retry_after = classify_provider_error_text("HTTP 429: retry-after 12")
        self.assertEqual(retry_after, 12.0)

    def test_http_401_is_unauthorized(self) -> None:
        error_cls, _retry_after = classify_provider_error_text("HTTP 401: invalid credentials")
        self.assertIs(error_cls, ProviderUnauthorizedError)

    def test_unrelated_failure_is_unclassified(self) -> None:
        from app.tushare_providers import ProviderCallError

        error_cls, retry_after = classify_provider_error_text("HTTP 500: internal error")
        self.assertIs(error_cls, ProviderCallError)
        self.assertIsNone(retry_after)

    def test_not_purchased_capability_rejection_is_not_misclassified_as_unauthorized(self) -> None:
        from app.tushare_providers import ProviderCallError

        error_cls, _retry_after = classify_provider_error_text("api not purchased for this account")
        self.assertIs(error_cls, ProviderCallError)


class ProviderCapabilityEvidenceTests(unittest.TestCase):
    def test_later_failure_preserves_verified_note_and_records_latest_observation(self):
        class Connection:
            def __init__(self):
                self.statement = ""
                self.params = ()

            def execute(self, statement, params):
                self.statement, self.params = statement, params

        connection = Connection()
        record_provider_api_capability(
            connection, "tushare_super_get", "daily", "failed", note="ConnectionError: upstream closed",
        )
        self.assertIn("quant.provider_api_capabilities.availability='verified'", connection.statement)
        self.assertIn("EXCLUDED.availability IN ('failed','empty')", connection.statement)
        metadata = connection.params[-1].obj
        self.assertEqual(metadata["last_observation"], "failed")
        self.assertEqual(metadata["last_observation_note"], "ConnectionError: upstream closed")

    def test_verified_observation_keeps_its_evidence_note(self):
        class Connection:
            def __init__(self): self.params = ()
            def execute(self, _statement, params): self.params = params

        connection = Connection()
        record_provider_api_capability(
            connection, "tushare_super_get", "daily", "verified", 12, "verified rows",
        )
        metadata = connection.params[-1].obj
        self.assertEqual(metadata["verified_note"], "verified rows")
        self.assertEqual(metadata["last_row_count"], 12)


if __name__ == "__main__":
    unittest.main()
