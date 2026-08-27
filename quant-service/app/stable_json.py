"""One JSON encoding convention for research payloads written to jsonb.

Payloads assembled from database rows carry driver-native types - Decimal for
a numeric column, date/datetime for a temporal one.  ``psycopg.types.json.Json``
encodes with the stdlib default, which has no hook for either, so a payload
that reads back perfectly well fails to write.  On 2026-08-27 this stopped the
post-close pipeline twice in a row, once on a Decimal ``strength`` and once on
a datetime inside the analyst execution context.

Feature snapshots already hashed their key with ``default=str``; encoding the
stored copy the same way means whatever the key covers is also storable.
"""

from __future__ import annotations

import json
from typing import Any

from psycopg.types.json import Json


def stable_dumps(payload: Any) -> str:
    """Deterministic text for a research payload."""
    return json.dumps(payload, ensure_ascii=False, sort_keys=True,
                      default=str, separators=(",", ":"))


def stable_json(value: Any) -> Json:
    """``Json`` adapter that tolerates the types a database row hands back."""
    return Json(value, dumps=stable_dumps)


__all__ = ["stable_dumps", "stable_json"]
