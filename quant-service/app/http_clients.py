"""Lifecycle-owned async HTTP clients for bounded public adapters.

The application process keeps a small keep-alive pool for public sources so a
30-second monitoring pass does not repeatedly create TCP/TLS connections.  A
direct unit invocation outside the FastAPI lifespan still receives a temporary
client, which keeps pure adapter tests isolated and avoids a cross-event-loop
global client.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
import os
from typing import AsyncIterator

import httpx


_public_client: httpx.AsyncClient | None = None
_alert_client: httpx.AsyncClient | None = None
_provider_clients: dict[tuple[str, str], httpx.AsyncClient] = {}
_remote_archive_clients: dict[tuple[str, str], httpx.AsyncClient] = {}


def public_proxy_url() -> str | None:
    """Return only the explicitly delegated public-source proxy.

    ``trust_env`` remains disabled so unrelated process proxy variables cannot
    silently reroute provider traffic.  The Windows launcher may deliberately
    copy its current HTTP proxy into this one scoped variable.
    """
    return (os.getenv("QUANT_PUBLIC_HTTP_PROXY") or "").strip() or None


async def start_http_clients() -> None:
    """Start the process-owned pool once from the FastAPI lifespan."""
    global _alert_client, _public_client
    if _public_client is None or _public_client.is_closed:
        options: dict[str, object] = {
            "timeout": httpx.Timeout(15.0), "trust_env": False, "follow_redirects": True,
            "limits": httpx.Limits(max_connections=12, max_keepalive_connections=8, keepalive_expiry=30.0),
        }
        if proxy := public_proxy_url():
            options["proxy"] = proxy
        _public_client = httpx.AsyncClient(**options)
    if _alert_client is None or _alert_client.is_closed:
        # Keep the local Feishu adapter separate from public-source capacity:
        # a slow public host must never exhaust the human-alert path.
        _alert_client = httpx.AsyncClient(
            timeout=httpx.Timeout(8.0), trust_env=False,
            limits=httpx.Limits(max_connections=4, max_keepalive_connections=2, keepalive_expiry=30.0),
        )


async def close_http_clients() -> None:
    """Release keep-alive sockets deterministically during service shutdown."""
    global _alert_client, _public_client
    if _alert_client is not None:
        await _alert_client.aclose()
        _alert_client = None
    provider_clients = list(_provider_clients.values())
    _provider_clients.clear()
    for client in provider_clients:
        await client.aclose()
    archive_clients = list(_remote_archive_clients.values())
    _remote_archive_clients.clear()
    for client in archive_clients:
        await client.aclose()
    if _public_client is not None:
        await _public_client.aclose()
        _public_client = None


@asynccontextmanager
async def public_http_client() -> AsyncIterator[httpx.AsyncClient]:
    """Yield the shared pool, or a scoped fallback outside application startup."""
    if _public_client is not None and not _public_client.is_closed:
        yield _public_client
        return
    options: dict[str, object] = {
        "timeout": httpx.Timeout(15.0), "trust_env": False, "follow_redirects": True,
        "limits": httpx.Limits(max_connections=4, max_keepalive_connections=2, keepalive_expiry=5.0),
    }
    if proxy := public_proxy_url():
        options["proxy"] = proxy
    async with httpx.AsyncClient(**options) as temporary_client:
        yield temporary_client


@asynccontextmanager
async def alert_http_client() -> AsyncIterator[httpx.AsyncClient]:
    """Yield the isolated Feishu-delivery pool or a scoped test fallback."""
    if _alert_client is not None and not _alert_client.is_closed:
        yield _alert_client
        return
    async with httpx.AsyncClient(
        timeout=httpx.Timeout(8.0), trust_env=False,
        limits=httpx.Limits(max_connections=2, max_keepalive_connections=1, keepalive_expiry=5.0),
    ) as temporary_client:
        yield temporary_client


@asynccontextmanager
async def provider_http_client(provider_key: str, proxy_url: str) -> AsyncIterator[httpx.AsyncClient]:
    """Yield a provider/proxy-isolated client for primary, City and backup paths.

    Credentials continue to be passed per request.  The pool key intentionally
    includes the proxy URL so changing provider routing can never reuse a
    connection through the previous proxy.  Outside the service lifespan the
    adapter gets a scoped temporary client, keeping tests event-loop safe.
    """
    key = (str(provider_key), str(proxy_url))
    lifecycle_active = _public_client is not None and not _public_client.is_closed
    if lifecycle_active:
        client = _provider_clients.get(key)
        if client is None or client.is_closed:
            options: dict[str, object] = {
                "timeout": httpx.Timeout(30.0), "trust_env": False,
                "limits": httpx.Limits(max_connections=4, max_keepalive_connections=2, keepalive_expiry=30.0),
            }
            if proxy_url:
                options["proxy"] = proxy_url
            client = httpx.AsyncClient(**options)
            _provider_clients[key] = client
        yield client
        return
    options = {
        "timeout": httpx.Timeout(30.0), "trust_env": False,
        "limits": httpx.Limits(max_connections=2, max_keepalive_connections=1, keepalive_expiry=5.0),
    }
    if proxy_url:
        options["proxy"] = proxy_url
    async with httpx.AsyncClient(**options) as temporary_client:
        yield temporary_client


@asynccontextmanager
async def remote_archive_http_client(base_url: str, ca_file: str | None) -> AsyncIterator[httpx.AsyncClient]:
    """Yield a small keep-alive pool for the fixed remote analyst archive.

    The archive is deliberately isolated from public-market capacity because
    a report-detail fanout must never delay watchlist quotes or Feishu.  Its
    key includes the endpoint and custom CA path so a configuration change
    cannot reuse a connection with a different TLS trust policy.  Bearer
    credentials are *not* client defaults: callers must attach them per
    request to avoid retaining a rotated credential in process memory.
    """
    key = (str(base_url).rstrip("/"), str(ca_file or ""))
    options: dict[str, object] = {
        "timeout": httpx.Timeout(30.0), "trust_env": False,
        "limits": httpx.Limits(max_connections=2, max_keepalive_connections=1, keepalive_expiry=20.0),
    }
    if ca_file:
        options["verify"] = ca_file
    lifecycle_active = _public_client is not None and not _public_client.is_closed
    if lifecycle_active:
        client = _remote_archive_clients.get(key)
        if client is None or client.is_closed:
            client = httpx.AsyncClient(**options)
            _remote_archive_clients[key] = client
        yield client
        return
    async with httpx.AsyncClient(**options) as temporary_client:
        yield temporary_client


def public_http_client_status() -> dict[str, int | bool]:
    """Expose only pool configuration/ownership, never provider traffic."""
    client = _public_client
    return {
        "lifecycle_owned": client is not None and not client.is_closed,
        "proxy_configured": public_proxy_url() is not None,
        "max_connections": 12,
        "max_keepalive_connections": 8,
    }


def alert_http_client_status() -> dict[str, int | bool]:
    """Expose lifecycle ownership of the adapter client without probing it."""
    client = _alert_client
    return {
        "lifecycle_owned": client is not None and not client.is_closed,
        "max_connections": 4,
        "max_keepalive_connections": 2,
    }


def provider_http_client_status() -> dict[str, int | bool]:
    """Expose provider-pool ownership/count without exposing endpoints or proxies."""
    active = sum(1 for client in _provider_clients.values() if not client.is_closed)
    return {"lifecycle_owned": _public_client is not None and not _public_client.is_closed,
            "active_provider_pools": active, "max_connections_per_pool": 4}


def remote_archive_http_client_status() -> dict[str, int | bool]:
    """Expose pool ownership/count without leaking archive addresses or tokens."""
    active = sum(1 for client in _remote_archive_clients.values() if not client.is_closed)
    return {"lifecycle_owned": _public_client is not None and not _public_client.is_closed,
            "active_archive_pools": active, "max_connections_per_pool": 2}


__all__ = [
    "alert_http_client", "alert_http_client_status", "close_http_clients",
    "provider_http_client", "provider_http_client_status", "public_http_client", "public_http_client_status",
    "public_proxy_url",
    "remote_archive_http_client", "remote_archive_http_client_status", "start_http_clients",
]
