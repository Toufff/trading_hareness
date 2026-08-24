#!/usr/bin/env bash
# Restricted SSH command: emit only the bounded research-evidence JSONL stream.
set -euo pipefail

original_command="${SSH_ORIGINAL_COMMAND:-}"
if [[ "$original_command" =~ ^export-since[[:space:]]([0-9T:+.-]+)$ ]]; then
  mode="since"
  argument="${BASH_REMATCH[1]}"
elif [[ "$original_command" =~ ^export-changes[[:space:]]([0-9]+)$ ]]; then
  mode="changes"
  argument="${BASH_REMATCH[1]}"
else
  printf 'usage: export-since ISO8601 | export-changes SEQUENCE\n' >&2
  exit 2
fi

export PGHOST=/var/run/postgresql
export PGPORT=5432
export PGDATABASE=quant_intraday_edge
export PGUSER=quant_edge_export
export PYTHONPATH=/opt/quant-intraday-edge/current/quant-service

export_args=(export)
if [[ "$mode" == "changes" ]]; then
  export_args+=(--after-sequence "$argument")
else
  export_args+=(--since "$argument")
fi

/opt/quant-intraday-edge/.venv/bin/python -m app.edge_evidence_transfer "${export_args[@]}" \
  | /usr/bin/gzip -1
