"""Platform-compatible local ASGI launcher."""

from __future__ import annotations

import argparse
import asyncio
import sys

import uvicorn


def configure_event_loop() -> str:
    """Psycopg async requires the selector loop on native Windows."""
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
        return "windows-selector"
    return "platform-default"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=5681)
    args = parser.parse_args()
    configure_event_loop()
    uvicorn.run("app.main:app", host=args.host, port=args.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
