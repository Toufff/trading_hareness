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

record_pull_status() {
  local state="$1"
  local error_message="${2:-}"
  /opt/homebrew/bin/docker compose -f "$repo_root/compose.yaml" exec -T quant-research \
    python -m app.edge_evidence_transfer pull-status --state "$state" --error "$error_message" >/dev/null 2>&1 || true
}

cursor_payload="$({ /opt/homebrew/bin/docker compose -f "$repo_root/compose.yaml" exec -T quant-research \
  python -m app.edge_evidence_transfer cursor --json 2>/dev/null \
  || /opt/homebrew/bin/docker compose -f "$repo_root/compose.yaml" exec -T quant-research \
    python -m app.edge_evidence_transfer cursor; } | tail -n 1)"
sequence="$(printf '%s' "$cursor_payload" | /usr/bin/sed -nE 's/.*"sequence":([0-9]+).*/\1/p')"
# During a staged rollout, the running workstation image or edge forced
# command may still be v1. Preserve the old bounded snapshot handoff until
# both ends report the journal cursor instead of stopping collection.
[[ -n "$sequence" ]] && [[ "$sequence" =~ ^[0-9]+$ ]] || sequence=0

if [[ "$sequence" -gt 0 ]]; then
  export_command="export-changes $sequence"
else
  # One bounded bootstrap covers the edge retention window before the local
  # cursor moves to the new monotonic journal. Later runs use only deltas.
  # Use an explicit RFC 3339 UTC offset.  The forced-command parser also
  # accepts Z, but this remains compatible with a staged edge running the
  # earlier strict command grammar.
  bootstrap_since="$(date -u -v-30d +%Y-%m-%dT%H:%M:%S+00:00)"
  export_command="export-since $bootstrap_since"
fi

record_pull_status running
set +e
transfer_output="$(/opt/homebrew/bin/ssh -T -i "$edge_key" -o BatchMode=yes -o IdentitiesOnly=yes \
  -o StrictHostKeyChecking=yes "$edge_target" "$export_command" \
  | /usr/bin/gzip -dc \
  | /opt/homebrew/bin/docker compose -f "$repo_root/compose.yaml" exec -T quant-research \
      python -m app.edge_evidence_transfer import 2>&1)"
transfer_status=$?
set -e
printf '%s\n' "$transfer_output"
if [[ "$transfer_status" -ne 0 ]]; then
  record_pull_status failed "$(printf '%s' "$transfer_output" | /usr/bin/tail -n 1 | /usr/bin/cut -c1-300)"
  exit "$transfer_status"
fi
record_pull_status completed
