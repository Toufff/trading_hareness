"""Process-wide structured logging for the quant-service runtime.

Every background loop, lease and scheduler failure previously went to stdout
through a bare ``print`` call: no level, timestamp or task label, and no way
to raise or lower verbosity without a code change.  This module wires the
standard library ``logging`` package to emit one JSON object per line
instead, so log aggregation can filter/alert on ``level``/``task`` without
parsing prose.  It intentionally adds no third-party dependency.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from datetime import datetime, timezone
from typing import Any, Mapping


# The attribute names a bare ``logging.LogRecord`` already carries.  Anything
# else on a record came from an ``extra={...}`` kwarg and should be folded
# into the JSON payload as its own key.
_BASE_RECORD_ATTRS = frozenset(logging.LogRecord("", 0, "", 0, "", None, None).__dict__) | {"message", "asctime"}

_configured = False


class JsonLogFormatter(logging.Formatter):
    """Render one JSON object per record with ``ts``/``level``/``logger``/``msg``.

    Any ``extra={...}`` field passed by the caller (for example ``task``) is
    merged in verbatim so a background-loop label survives into the log line
    without every call site having to serialize its own JSON.
    """

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key in _BASE_RECORD_ATTRS or key in payload:
                continue
            try:
                json.dumps(value)
            except TypeError:
                value = str(value)
            payload[key] = value
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, default=str)


def resolve_log_level(environ: Mapping[str, str] | None = None) -> int:
    """Read ``QUANT_LOG_LEVEL``, defaulting to INFO for an unset/invalid value."""
    values = environ if environ is not None else os.environ
    name = str(values.get("QUANT_LOG_LEVEL", "INFO")).strip().upper()
    level = logging.getLevelName(name)
    return level if isinstance(level, int) else logging.INFO


def configure_logging(environ: Mapping[str, str] | None = None) -> None:
    """Idempotently wire the root logger to emit one JSON line per record.

    Safe to call more than once: application startup and the test suite may
    both invoke it within one process. Only the first call installs a
    handler; every call re-applies ``QUANT_LOG_LEVEL`` so a later change to
    the environment (or an explicit test override) still takes effect.
    """
    global _configured
    root = logging.getLogger()
    if not _configured:
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(JsonLogFormatter())
        root.addHandler(handler)
        _configured = True
    root.setLevel(resolve_log_level(environ))


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)


__all__ = ["JsonLogFormatter", "configure_logging", "get_logger", "resolve_log_level"]
