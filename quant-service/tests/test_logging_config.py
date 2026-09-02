from __future__ import annotations

import json
import logging
import unittest

from app import logging_config
from app.logging_config import JsonLogFormatter, configure_logging, get_logger, resolve_log_level


class ResolveLogLevelTests(unittest.TestCase):
    def test_defaults_to_info_when_unset(self) -> None:
        self.assertEqual(resolve_log_level({}), logging.INFO)

    def test_reads_valid_level_case_insensitively(self) -> None:
        self.assertEqual(resolve_log_level({"QUANT_LOG_LEVEL": "debug"}), logging.DEBUG)
        self.assertEqual(resolve_log_level({"QUANT_LOG_LEVEL": "WARNING"}), logging.WARNING)

    def test_falls_back_to_info_for_an_invalid_value(self) -> None:
        self.assertEqual(resolve_log_level({"QUANT_LOG_LEVEL": "not-a-level"}), logging.INFO)


class JsonLogFormatterTests(unittest.TestCase):
    def _record(self, **extra: object) -> logging.LogRecord:
        record = logging.LogRecord(
            name="app.runtime_tasks", level=logging.WARNING, pathname=__file__, lineno=1,
            msg="post_close_strategy lease lost", args=(), exc_info=None,
        )
        for key, value in extra.items():
            setattr(record, key, value)
        return record

    def test_emits_required_keys(self) -> None:
        line = JsonLogFormatter().format(self._record(task="post_close_strategy"))
        payload = json.loads(line)
        self.assertIn("ts", payload)
        self.assertEqual(payload["level"], "WARNING")
        self.assertEqual(payload["logger"], "app.runtime_tasks")
        self.assertEqual(payload["task"], "post_close_strategy")
        self.assertEqual(payload["msg"], "post_close_strategy lease lost")

    def test_omits_task_key_when_not_supplied(self) -> None:
        payload = json.loads(JsonLogFormatter().format(self._record()))
        self.assertNotIn("task", payload)

    def test_non_json_serializable_extra_falls_back_to_str(self) -> None:
        class Unserializable:
            def __str__(self) -> str:
                return "unserializable-value"

        payload = json.loads(JsonLogFormatter().format(self._record(detail=Unserializable())))
        self.assertEqual(payload["detail"], "unserializable-value")


class ConfigureLoggingTests(unittest.TestCase):
    def setUp(self) -> None:
        self._original_configured = logging_config._configured
        self._original_handlers = list(logging.getLogger().handlers)
        self._original_level = logging.getLogger().level

    def tearDown(self) -> None:
        logging_config._configured = self._original_configured
        logging.getLogger().handlers = self._original_handlers
        logging.getLogger().setLevel(self._original_level)

    def test_first_call_installs_exactly_one_json_handler(self) -> None:
        logging_config._configured = False
        logging.getLogger().handlers = []
        configure_logging({"QUANT_LOG_LEVEL": "INFO"})
        handlers = logging.getLogger().handlers
        self.assertEqual(len(handlers), 1)
        self.assertIsInstance(handlers[0].formatter, JsonLogFormatter)

    def test_second_call_does_not_add_a_duplicate_handler(self) -> None:
        logging_config._configured = False
        logging.getLogger().handlers = []
        configure_logging({"QUANT_LOG_LEVEL": "INFO"})
        configure_logging({"QUANT_LOG_LEVEL": "DEBUG"})
        self.assertEqual(len(logging.getLogger().handlers), 1)
        self.assertEqual(logging.getLogger().level, logging.DEBUG)

    def test_get_logger_returns_a_standard_logger(self) -> None:
        self.assertIsInstance(get_logger("app.runtime_tasks"), logging.Logger)


if __name__ == "__main__":
    unittest.main()
