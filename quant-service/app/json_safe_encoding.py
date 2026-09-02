"""Reversible JSON-safety encoding for values PostgreSQL text/JSONB reject.

Extracted from the one-way stock-brain SQLite migration contracts (now under
``scripts/legacy/stock_brain/``) so a production module
(``outcome_recomputation``) does not need to import migration-only code that
otherwise lives outside the ``app`` package.
"""

from __future__ import annotations

import base64
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Mapping, Sequence


def json_safe(value: Any) -> Any:
    if value is None or isinstance(value, (int, float, bool)):
        return value
    if isinstance(value, str):
        # PostgreSQL text/JSONB cannot represent NUL and rejects unpaired UTF-16
        # surrogates. Preserve such legacy bytes reversibly instead of silently
        # deleting or replacing evidence text.
        if "\x00" in value or any(0xD800 <= ord(character) <= 0xDFFF for character in value):
            encoded = value.encode("utf-8", errors="surrogatepass")
            return {
                "encoding": "utf-8-base64",
                "data": base64.b64encode(encoded).decode("ascii"),
                "reason": "postgresql-text-incompatible",
            }
        return value
    if isinstance(value, (datetime, date, Decimal)):
        return str(value)
    if isinstance(value, bytes):
        return {"encoding": "base64", "data": base64.b64encode(value).decode("ascii")}
    if isinstance(value, Mapping):
        return {str(key): json_safe(item) for key, item in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [json_safe(item) for item in value]
    return str(value)


__all__ = ["json_safe"]
