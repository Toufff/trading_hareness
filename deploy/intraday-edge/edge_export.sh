#!/usr/bin/env bash
# Restricted SSH command: emit only the bounded research-evidence JSONL stream.
set -euo pipefail

export PGHOST=/var/run/postgresql
export PGPORT=5432
export PGDATABASE=quant_intraday_edge
export PGUSER=quant_edge_export
export PYTHONPATH=/opt/quant-intraday-edge/current/quant-service

# The Python parser is the sole protocol authority.  It accepts RFC 3339 Z
# timestamps as well as +00:00 and rejects every command other than the two
# bounded, read-only export variants before opening the database stream.
/opt/quant-intraday-edge/.venv/bin/python -m app.edge_evidence_transfer restricted-export "${SSH_ORIGINAL_COMMAND:-}" \
  | /usr/bin/gzip -1
