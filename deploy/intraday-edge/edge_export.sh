#!/usr/bin/env bash
# Restricted SSH command: emit only the bounded research-evidence JSONL stream.
set -euo pipefail

original_command="${SSH_ORIGINAL_COMMAND:-}"
if [[ ! "$original_command" =~ ^export-since[[:space:]]([0-9T:+.-]+)$ ]]; then
  printf 'usage: export-since ISO8601\n' >&2
  exit 2
fi
since="${BASH_REMATCH[1]}"

export PGHOST=/var/run/postgresql
export PGPORT=5432
export PGDATABASE=quant_intraday_edge
export PGUSER=quant_edge_export
export PYTHONPATH=/opt/quant-intraday-edge/current/quant-service

/opt/quant-intraday-edge/.venv/bin/python -m app.edge_evidence_transfer export --since "$since" \
  | /usr/bin/gzip -1
