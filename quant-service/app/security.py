"""HTTP write-boundary primitives shared by the composition root and tests."""

from __future__ import annotations

import re
import secrets
from typing import Any

from fastapi import Request


def write_access_allowed(method: str, supplied_key: str | None, configured_key: str | None) -> bool:
    if method.upper() not in {"POST", "PUT", "PATCH", "DELETE"}:
        return True
    if not configured_key:
        return True
    return bool(supplied_key) and secrets.compare_digest(supplied_key, configured_key)


def remote_archive_sync_bearer_allowed(request: Request) -> bool:
    """Allow only the bounded bearer-shaped remote text-sync trigger."""
    if request.method.upper() != "POST" or request.url.path != "/api/v1/remote-archive/sync":
        return False
    authorization = request.headers.get("Authorization", "").strip()
    return bool(re.fullmatch(r"Bearer\s+[A-Za-z0-9._~+/-]{24,512}", authorization, flags=re.IGNORECASE))


def security_context() -> dict[str, Any]:
    return {
        "write_methods": ["POST", "PUT", "PATCH", "DELETE"],
        "header": "X-Quant-Write-Key",
        "remote_sync_exception": "/api/v1/remote-archive/sync",
        "secret_values_exposed": False,
    }


__all__ = ["write_access_allowed", "remote_archive_sync_bearer_allowed", "security_context"]
