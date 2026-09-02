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
    if not supplied_key:
        return False
    # ``compare_digest`` raises ``TypeError`` (surfacing as a 500) when either
    # argument contains non-ASCII text.  Encoding first keeps a wrong or
    # exotic key a normal 401 instead of an unhandled server error.
    return secrets.compare_digest(supplied_key.encode("utf-8"), configured_key.encode("utf-8"))


def remote_archive_sync_bearer_allowed(request: Request) -> bool:
    """Allow only the bounded bearer-shaped remote text-sync trigger.

    This is a shape check only.  The route additionally requires a valid
    ``X-Quant-Write-Key`` (enforced by the app-wide write-key middleware) so
    an unauthenticated caller can no longer trigger a remote sync attempt or
    its automation-run bookkeeping merely by presenting a bearer-shaped
    ``Authorization`` header.
    """
    if request.method.upper() != "POST" or request.url.path != "/api/v1/remote-archive/sync":
        return False
    authorization = request.headers.get("Authorization", "").strip()
    return bool(re.fullmatch(r"Bearer\s+[A-Za-z0-9._~+/-]{24,512}", authorization, flags=re.IGNORECASE))


def licensed_stock_read_allowed(request: Request, configured_key: str | None) -> bool:
    """Treat the authenticated raw-data proxy as a read despite its POST body."""
    if request.method.upper() != "POST" or request.url.path != "/licensed/stock-api/call":
        return False
    expected = str(configured_key or "").strip()
    supplied = request.headers.get("X-Quant-Read-Key", "").strip()
    if not expected or not supplied:
        return False
    # See ``write_access_allowed`` above for why this encodes first.
    return secrets.compare_digest(supplied.encode("utf-8"), expected.encode("utf-8"))


def security_context() -> dict[str, Any]:
    return {
        "write_methods": ["POST", "PUT", "PATCH", "DELETE"],
        "header": "X-Quant-Write-Key",
        "remote_sync_additional_bearer_requirement": "/api/v1/remote-archive/sync",
        "licensed_read_post_exception": "/licensed/stock-api/call",
        "secret_values_exposed": False,
    }


__all__ = [
    "licensed_stock_read_allowed",
    "remote_archive_sync_bearer_allowed",
    "security_context",
    "write_access_allowed",
]
