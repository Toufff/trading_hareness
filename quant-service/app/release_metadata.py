"""Non-secret build provenance exposed by the operational health endpoint."""

from __future__ import annotations

import os
import re
from collections.abc import Mapping


_GIT_SHA_RE = re.compile(r"^[0-9a-f]{7,64}$", re.IGNORECASE)
_MAX_TEXT_LENGTH = 160


def _text(value: object | None) -> str | None:
    text = str(value or "").strip()
    if not text or text.lower() in {"unknown", "unset", "none"}:
        return None
    return text[:_MAX_TEXT_LENGTH]


def release_metadata(environment: Mapping[str, str] | None = None) -> dict[str, str | None]:
    """Return only display-safe release fields; never include runtime secrets."""
    env = os.environ if environment is None else environment
    git_sha = _text(env.get("APP_GIT_SHA"))
    return {
        "git_sha": git_sha.lower() if git_sha and _GIT_SHA_RE.fullmatch(git_sha) else None,
        "release": _text(env.get("APP_RELEASE")),
        "build_created_at": _text(env.get("APP_BUILD_CREATED_AT")),
    }


__all__ = ["release_metadata"]
