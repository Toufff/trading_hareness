"""Materialize the next-session shadow pool from edge-owned daily evidence."""

from __future__ import annotations

import json
import os
import sys

import httpx


def main() -> int:
    write_key = os.getenv("QUANT_WRITE_API_KEY", "").strip()
    if not write_key:
        raise RuntimeError("QUANT_WRITE_API_KEY is required")
    response = httpx.post(
        "http://127.0.0.1:18110/api/v1/research/ten-day-leader-rotation/run",
        headers={"X-Quant-Write-Key": write_key}, json={}, timeout=600.0,
    )
    response.raise_for_status()
    payload = response.json()
    if payload.get("status") not in {"completed", "partial"}:
        raise RuntimeError(
            f"ten-day shadow materialization {payload.get('status')}: {payload.get('reason')}"
        )
    print(json.dumps({
        "status": payload.get("status"), "as_of_date": payload.get("as_of_date"),
        "scope": payload.get("scope"), "summary": payload.get("summary"),
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:  # systemd retains the bounded failure evidence
        print(f"intraday edge materialization failed: {str(error)[:500]}", file=sys.stderr)
        raise
