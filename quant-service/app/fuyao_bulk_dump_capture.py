"""Safe capture of Fuyao's immutable market-dump artifacts.

The dump endpoint returns a short-lived presigned URL, not the historical rows
themselves.  This module only captures the provider artifact into an atomic
local staging file and writes a secret-free manifest.  Projection into
canonical tables is a separate, schema-aware step so a malformed dump can
never enter a strategy path as if it were validated data.
"""

from __future__ import annotations

import hashlib
import argparse
import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .fuyao_provider import FUYAO_PROVIDER_KEY, fetch_envelope
from .http_clients import provider_http_client


MAX_BULK_DUMP_BYTES = 2 * 1024 * 1024 * 1024
SUPPORTED_CAPABILITIES = frozenset({
    "a_share_daily_k_10y_dump", "a_share_daily_k_10d_dump", "a_share_adjustment_factors_dump",
})


@dataclass(frozen=True)
class DumpDescriptor:
    capability: str
    url: str
    expires_at: str | None
    expires_in_seconds: int | None
    request_id: str | None


def descriptor_from_envelope(capability: str, envelope: dict[str, Any]) -> DumpDescriptor:
    """Validate the short-lived URL shape without returning it in logs."""
    if capability not in SUPPORTED_CAPABILITIES:
        raise ValueError(f"unsupported Fuyao dump capability: {capability}")
    data = envelope.get("data") if isinstance(envelope, dict) else None
    url = str((data or {}).get("presigned_url") or "").strip()
    if not url.startswith(("https://", "http://")):
        raise ValueError("Fuyao dump response has no valid presigned_url")
    raw_expires = (data or {}).get("expires_in_seconds")
    try:
        expires = int(raw_expires) if raw_expires is not None else None
    except (TypeError, ValueError):
        expires = None
    return DumpDescriptor(
        capability=capability, url=url,
        expires_at=str((data or {}).get("presigned_url_expires_at") or "") or None,
        expires_in_seconds=expires,
        request_id=str(envelope.get("request_id") or "") or None,
    )


def safe_dump_filename(capability: str) -> str:
    if capability not in SUPPORTED_CAPABILITIES:
        raise ValueError(f"unsupported Fuyao dump capability: {capability}")
    return f"{capability}.parquet"


def manifest_for_file(
    descriptor: DumpDescriptor,
    path: Path,
    *,
    size: int,
    sha256: str,
    retrieved_at: datetime,
) -> dict[str, Any]:
    """Build a manifest that contains no presigned URL or API credential."""
    return {
        "schema": "fuyao-market-dump-manifest-v1", "capability": descriptor.capability,
        "filename": path.name, "bytes": size, "sha256": sha256,
        "provider_request_id": descriptor.request_id,
        "presigned_url_expires_at": descriptor.expires_at,
        "expires_in_seconds": descriptor.expires_in_seconds,
        "retrieved_at": retrieved_at.astimezone(timezone.utc).isoformat(),
        "projection_status": "captured_unprojected", "research_only": True, "live_effect": "none",
    }


async def capture_dump(
    capability: str,
    *,
    output_dir: str | Path = "/var/lib/quant/fuyao-dumps",
    max_bytes: int = MAX_BULK_DUMP_BYTES,
    client: Any | None = None,
) -> dict[str, Any]:
    """Download one dump atomically and return a secret-free receipt."""
    if max_bytes < 1:
        raise ValueError("max_bytes must be positive")
    descriptor = descriptor_from_envelope(capability, await fetch_envelope(capability))
    root = Path(output_dir)
    root.mkdir(parents=True, exist_ok=True)
    target = root / safe_dump_filename(capability)
    partial = target.with_suffix(target.suffix + ".part")
    own_client = client is None
    digest = hashlib.sha256()
    size = 0
    try:
        if own_client:
            async with provider_http_client(FUYAO_PROVIDER_KEY, "") as http_client:
                size = await _download(http_client, descriptor.url, partial, max_bytes, digest)
        else:
            size = await _download(client, descriptor.url, partial, max_bytes, digest)
        os.replace(partial, target)
    finally:
        if partial.exists():
            partial.unlink()
    retrieved_at = datetime.now(timezone.utc)
    manifest = manifest_for_file(descriptor, target, size=size, sha256=digest.hexdigest(), retrieved_at=retrieved_at)
    manifest_path = target.with_suffix(".manifest.json")
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return {**manifest, "path": str(target), "manifest_path": str(manifest_path)}


async def _download(http_client: Any, url: str, partial: Path, max_bytes: int, digest: Any) -> int:
    """Stream a presigned artifact without retaining it in memory."""
    async with http_client.stream("GET", url) as response:
        response.raise_for_status()
        content_length = response.headers.get("content-length")
        if content_length and int(content_length) > max_bytes:
            raise ValueError("Fuyao dump exceeds configured byte budget")
        size = 0
        with partial.open("wb") as stream:
            async for chunk in response.aiter_bytes(1024 * 1024):
                size += len(chunk)
                if size > max_bytes:
                    raise ValueError("Fuyao dump exceeded configured byte budget")
                digest.update(chunk)
                stream.write(chunk)
    return size


__all__ = [
    "DumpDescriptor", "MAX_BULK_DUMP_BYTES", "SUPPORTED_CAPABILITIES", "capture_dump",
    "descriptor_from_envelope", "manifest_for_file", "safe_dump_filename",
]


def main() -> int:
    parser = argparse.ArgumentParser(description="Capture one Fuyao bulk dump into staging (no projection)")
    parser.add_argument("--capability", choices=sorted(SUPPORTED_CAPABILITIES), required=True)
    parser.add_argument("--output-dir", default=os.getenv("FUYAO_BULK_DUMP_DIR", "/var/lib/quant/fuyao-dumps"))
    parser.add_argument("--max-bytes", type=int, default=MAX_BULK_DUMP_BYTES)
    args = parser.parse_args()
    import asyncio
    print(json.dumps(asyncio.run(capture_dump(args.capability, output_dir=args.output_dir, max_bytes=args.max_bytes)), ensure_ascii=False))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
