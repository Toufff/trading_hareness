#!/usr/bin/env bash
# Read-only comparison of tracked canonical workflow JSON against the live edge.
set -euo pipefail

edge_host="${RELAY_EDGE_HOST:-root@47.114.113.152}"
edge_ssh_control_path="${RELAY_EDGE_SSH_CONTROL_PATH:-}"
source_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
tracked_root="$source_root/workflows/edge-relay/workflows"
edge_container="${RELAY_EDGE_N8N_CONTAINER:-feishu-relay-edge-n8n}"
ids=(mediaStateFlow123 mediaPartFlow123 mediaFinalize123 xo3AHKRr4MFXrzFA)
ssh_command=(ssh)
[[ -z "$edge_ssh_control_path" ]] || ssh_command+=(-S "$edge_ssh_control_path" -o ControlMaster=no -o BatchMode=yes)

[[ -d "$tracked_root" ]] || { echo "tracked edge workflow source is missing: $tracked_root" >&2; exit 2; }
for command in ssh tar jq mktemp shasum diff; do command -v "$command" >/dev/null || { echo "missing required command: $command" >&2; exit 127; }; done

stage_root="$(mktemp -d "${TMPDIR:-/tmp}/edge-relay-workflow-verify.XXXXXX")"
cleanup() { rm -rf -- "$stage_root"; }
trap cleanup EXIT
remote_dir="/tmp/edge-relay-workflow-verify-${RANDOM}-$$"
remote_base="${remote_dir##*/}"
remote_archive_base="$("${ssh_command[@]}" "$edge_host" "docker exec '$edge_container' printenv REMOTE_ANALYST_ARCHIVE_BASE_URL")"
[[ -n "$remote_archive_base" ]] || { echo "remote archive base URL is not configured" >&2; exit 1; }

"${ssh_command[@]}" "$edge_host" "docker exec '$edge_container' sh -lc 'mkdir -p \"$remote_dir\" && n8n export:workflow --all --published --separate --output=\"$remote_dir\" >/dev/null' && docker cp '$edge_container:$remote_dir' - && docker exec '$edge_container' rm -rf '$remote_dir'" \
  | tar -xf - -C "$stage_root"
for id in "${ids[@]}"; do
  input="$stage_root/$remote_base/${id}.json"
  [[ -f "$input" ]] || { echo "remote export missing $id" >&2; exit 1; }
  jq -S --arg remote_archive_base "$remote_archive_base" '
    def redact_archive_base: split($remote_archive_base) | join("__REMOTE_ANALYST_ARCHIVE_BASE_URL__");
    walk(if type == "string" then redact_archive_base else . end)
  ' "$input" > "$stage_root/${id}.canonical.json"
  diff -u "$tracked_root/${id}.json" "$stage_root/${id}.canonical.json" >/dev/null || {
    echo "workflow drift: $id" >&2
    exit 1
  }
done
printf 'edge workflow source matches live remote: %s workflows\n' "${#ids[@]}"
