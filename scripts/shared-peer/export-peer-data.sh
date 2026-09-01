#!/usr/bin/env bash
set -euo pipefail

: "${PGHOST:?PGHOST is required}"
: "${PGPORT:?PGPORT is required}"
: "${PGDATABASE:?PGDATABASE is required}"
: "${PGUSER:?PGUSER is required}"

output_root="${1:-$PWD/peer-export}"
stamp="$(date -u +%Y%m%dT%H%M%SZ)"
destination="${output_root%/}/${stamp}"
mkdir -p "${destination}"

quant_dump="${destination}/application.dump"
pg_dump \
  --host="${PGHOST}" --port="${PGPORT}" --username="${PGUSER}" \
  --dbname="${PGDATABASE}" --format=custom --compress=9 \
  --no-owner --no-privileges --schema=public --schema=quant --file="${quant_dump}"
pg_restore --list "${quant_dump}" >/dev/null

public_dump=""
if [[ "${EXPORT_N8N_PUBLIC_SCHEMA:-false}" == "true" ]]; then
  : "${N8N_PGDATABASE:?N8N_PGDATABASE is required when exporting n8n}"
  public_dump="${destination}/n8n-public.dump"
  pg_dump \
    --host="${PGHOST}" --port="${PGPORT}" --username="${PGUSER}" \
    --dbname="${N8N_PGDATABASE}" --format=custom --compress=9 \
    --no-owner --no-privileges --schema=public --file="${public_dump}"
  pg_restore --list "${public_dump}" >/dev/null
fi

quant_hash="$(sha256sum "${quant_dump}" | awk '{print $1}')"
printf '%s  %s\n' "${quant_hash}" "$(basename "${quant_dump}")" > "${quant_dump}.sha256"
if [[ -n "${public_dump}" ]]; then
  public_hash="$(sha256sum "${public_dump}" | awk '{print $1}')"
  printf '%s  %s\n' "${public_hash}" "$(basename "${public_dump}")" > "${public_dump}.sha256"
fi

python3 - "${destination}" "${PGDATABASE}" "${quant_dump}" "${public_dump}" <<'PY'
import json
import platform
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

destination, database, quant_dump, public_dump = sys.argv[1:]
manifest = {
    "created_at": datetime.now(timezone.utc).isoformat(),
    "source_database": database,
    "source_host_fingerprint": platform.node(),
    "pg_dump_version": subprocess.check_output(["pg_dump", "--version"], text=True).strip(),
    "application_dump": Path(quant_dump).name,
    "n8n_public_dump": Path(public_dump).name if public_dump else None,
    "contains_credentials": bool(public_dump),
}
Path(destination, "manifest.json").write_text(
    json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
)
PY

printf 'peer export ready: %s\n' "${destination}"
printf 'quant sha256: %s\n' "${quant_hash}"
