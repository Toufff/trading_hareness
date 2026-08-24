#!/usr/bin/env bash
# Pull edge-owned evidence into the workstation DB. No remote writes are made.
set -euo pipefail

repo_root="$(cd "$(dirname "$0")/.." && pwd)"
edge_target="${QUANT_EDGE_EXPORT_TARGET:-quant_edge_export@47.114.113.152}"
edge_key="${QUANT_EDGE_EXPORT_KEY:-/Users/papa/.ssh/quant_intraday_edge_export}"
lock_dir="${TMPDIR:-/tmp}/quant-intraday-edge-pull.lock"

if ! mkdir "$lock_dir" 2>/dev/null; then
  printf 'edge evidence pull already running\n'
  exit 0
fi
trap 'rmdir "$lock_dir"' EXIT

if [[ ! -r "$edge_key" ]]; then
  printf 'edge evidence key is not readable: %s\n' "$edge_key" >&2
  exit 1
fi
if ! /opt/homebrew/bin/docker compose -f "$repo_root/compose.yaml" ps --status running quant-research >/dev/null; then
  printf 'local quant-research is not running; launchd will retry later\n' >&2
  exit 1
fi

since="$({ /opt/homebrew/bin/docker compose -f "$repo_root/compose.yaml" exec -T quant-research \
  python -m app.edge_evidence_transfer cursor; } | tail -n 1)"

/opt/homebrew/bin/ssh -T -i "$edge_key" -o BatchMode=yes -o IdentitiesOnly=yes \
  -o StrictHostKeyChecking=yes "$edge_target" "export-since $since" \
  | /usr/bin/gzip -dc \
  | /opt/homebrew/bin/docker compose -f "$repo_root/compose.yaml" exec -T quant-research \
      python -m app.edge_evidence_transfer import
