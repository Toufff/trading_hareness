#!/usr/bin/env python3
"""Dry-run or publish the latest verified stock-brain CITIC snapshot."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "quant-service"))

from app.legacy_stock_brain_import import load_stock_brain_portfolio  # noqa: E402


def _request_json(url: str, *, method: str = "GET", payload: dict | None = None, token: str = "") -> dict:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
    headers = {"accept": "application/json"}
    if body is not None:
        headers["content-type"] = "application/json"
    if token:
        headers["X-Quant-Write-Key"] = token
    request = Request(url, data=body, headers=headers, method=method)
    try:
        with urlopen(request, timeout=30) as response:
            return json.loads(response.read().decode("utf-8"))
    except HTTPError as error:
        detail = error.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {error.code}: {detail}") from error
    except URLError as error:
        raise RuntimeError(f"cannot reach decision API: {error.reason}") from error


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path(r"F:\AIWorkflow\stock-brain\daily\config.json"))
    parser.add_argument("--account-key", default="citics-primary")
    parser.add_argument("--apply", action="store_true", help="publish to the API; otherwise validate and print only")
    parser.add_argument("--api-base", default=os.getenv("QUANT_API_BASE", "http://127.0.0.1:8000"))
    parser.add_argument("--token-env", default="QUANT_WRITE_API_KEY")
    args = parser.parse_args()

    snapshot = load_stock_brain_portfolio(args.config, account_key=args.account_key)
    payload = snapshot.model_dump(mode="json")
    if not args.apply:
        print(json.dumps({"mode": "dry-run", "snapshot": payload}, ensure_ascii=False, indent=2))
        return 0

    token = os.getenv(args.token_env, "")
    if not token:
        raise SystemExit(f"{args.token_env} is required with --apply")
    base = args.api_base.rstrip("/")
    write_result = _request_json(
        f"{base}/api/v1/personal/portfolio-snapshots", method="POST", payload=payload, token=token,
    )
    readback = _request_json(
        f"{base}/api/v1/personal/portfolio-snapshots/latest?account_key={args.account_key}", token=token,
    )
    if readback.get("source_snapshot_key") != payload["source_snapshot_key"]:
        raise SystemExit("write succeeded but latest readback does not match the published snapshot")
    print(json.dumps({"mode": "applied", "write": write_result, "readback": readback}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
