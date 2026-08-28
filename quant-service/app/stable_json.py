"""One JSON encoding convention for research payloads written to jsonb.

Payloads assembled from database rows carry driver-native types - Decimal for
a numeric column, date/datetime for a temporal one.  ``psycopg.types.json.Json``
encodes with the stdlib default, which has no hook for either, so a payload
that reads back perfectly well fails to write.  On 2026-08-27 this stopped the
post-close pipeline twice in a row, once on a Decimal ``strength`` and once on
a datetime inside the analyst execution context.

Feature snapshots already hashed their key with ``default=str``; encoding the
stored copy the same way means whatever the key covers is also storable.

Two adapters, because they are not interchangeable.  ``stable_json`` also
sorts keys, which is right where the stored text is itself hashed and wrong
anywhere an existing hash was taken over the payload in its original order.
``tolerant_json`` adds only the type hook, so it is the one to reach for when
retrofitting a call site whose hashing you have not audited.
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


def tolerant_dumps(payload: Any) -> str:
    """Encode a payload of driver-native types, leaving key order alone."""
    return json.dumps(payload, ensure_ascii=False, default=str, separators=(",", ":"))


def tolerant_json(value: Any) -> Json:
    """``Json`` that accepts Decimal and datetime without reordering keys.

    The reordering in ``stable_json`` is a change of stored bytes.  Where a
    hash was already taken over the payload it is harmless, but auditing every
    call site is slower than not changing the bytes at all, so a retrofit that
    only needs the failure to stop uses this one.
    """
    return Json(value, dumps=tolerant_dumps)


__all__ = ["stable_dumps", "stable_json", "tolerant_dumps", "tolerant_json"]
