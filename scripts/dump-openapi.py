#!/usr/bin/env python3
"""Write the quant-service OpenAPI document without a database or a server.

``app.main`` builds its FastAPI application at import time but only opens
connection pools lazily, so the schema can be rendered offline::

    python scripts/dump-openapi.py            # JSON to stdout
    python scripts/dump-openapi.py out.json   # JSON to a file

The output feeds ``scripts/generate-api-types.mjs`` (``QUANT_OPENAPI_FILE`` or
``--offline``) and ``scripts/verify-api-contract.mjs`` so ``npm run api:check``
no longer needs a running quant-research container.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
SERVICE = ROOT / "quant-service"


def openapi_document() -> dict:
    # Keep every background loop off; this process only renders the schema.
    os.environ.setdefault("QUANT_BACKGROUND_TASKS_ENABLED", "false")
    if str(SERVICE) not in sys.path:
        sys.path.insert(0, str(SERVICE))
    from app.main import app

    return app.openapi()


def main(argv: list[str]) -> int:
    document = openapi_document()
    rendered = json.dumps(document, ensure_ascii=False, indent=2) + "\n"
    if argv and argv[0] != "-":
        output = Path(argv[0])
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(rendered, encoding="utf-8")
        print(f"{output} ({len(document.get('paths', {}))} paths)", file=sys.stderr)
    else:
        sys.stdout.write(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
