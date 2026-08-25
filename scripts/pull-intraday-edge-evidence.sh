#!/usr/bin/env bash
# Pull edge-owned evidence into the workstation DB. No remote writes are made.
set -euo pipefail

repo_root="$(cd "$(dirname "$0")/.." && pwd)"
edge_target="${QUANT_EDGE_EXPORT_TARGET:-quant_edge_export@47.114.113.152}"
edge_key="${QUANT_EDGE_EXPORT_KEY:-/Users/papa/.ssh/quant_intraday_edge_export}"
lock_dir="${TMPDIR:-/tmp}/quant-intraday-edge-pull.lock"
max_pages="${QUANT_EDGE_PULL_MAX_PAGES:-40}"
max_seconds="${QUANT_EDGE_PULL_MAX_SECONDS:-240}"

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
  local pages_imported="${3:-}"
  local rows_imported="${4:-}"
  local duration_ms="${5:-}"
  local args=(python -m app.edge_evidence_transfer pull-status --state "$state" --error "$error_message")
  [[ -n "$pages_imported" ]] && args+=(--pages-imported "$pages_imported")
  [[ -n "$rows_imported" ]] && args+=(--rows-imported "$rows_imported")
  [[ -n "$duration_ms" ]] && args+=(--duration-ms "$duration_ms")
  /opt/homebrew/bin/docker compose -f "$repo_root/compose.yaml" exec -T quant-research \
    "${args[@]}" >/dev/null 2>&1 || true
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

record_pull_status running
started_seconds=$SECONDS
pages_imported=0
rows_imported=0
has_more=false

while :; do
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
    duration_ms=$(( (SECONDS - started_seconds) * 1000 ))
    record_pull_status failed "$(printf '%s' "$transfer_output" | /usr/bin/tail -n 1 | /usr/bin/cut -c1-300)" \
      "$pages_imported" "$rows_imported" "$duration_ms"
    exit "$transfer_status"
  fi

  page_result="$(printf '%s\n' "$transfer_output" | /usr/bin/tail -n 1)"
  sequence="$(printf '%s' "$page_result" | /opt/homebrew/bin/jq -r '.sequence // 0')"
  page_rows="$(printf '%s' "$page_result" | /opt/homebrew/bin/jq -r '([.counts[] | numbers] | add) // 0')"
  has_more="$(printf '%s' "$page_result" | /opt/homebrew/bin/jq -r '.has_more // false')"
  [[ "$sequence" =~ ^[0-9]+$ ]] || { record_pull_status failed "invalid imported sequence"; exit 1; }
  [[ "$page_rows" =~ ^[0-9]+$ ]] || page_rows=0
  pages_imported=$((pages_imported + 1))
  rows_imported=$((rows_imported + page_rows))
  duration_ms=$(( (SECONDS - started_seconds) * 1000 ))

  if [[ "$has_more" != "true" ]]; then
    record_pull_status completed "" "$pages_imported" "$rows_imported" "$duration_ms"
    exit 0
  fi
  if [[ "$pages_imported" -ge "$max_pages" || "$SECONDS" -ge "$max_seconds" ]]; then
    record_pull_status catching_up "bounded catch-up will resume on the next local trigger" \
      "$pages_imported" "$rows_imported" "$duration_ms"
    exit 0
  fi
done
