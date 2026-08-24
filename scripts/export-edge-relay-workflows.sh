#!/usr/bin/env bash
# Export the remote relay's four active n8n workflows as canonical, reviewable
# source files. Credential values are not exported; only n8n credential refs are.
set -euo pipefail

edge_host="${RELAY_EDGE_HOST:-root@47.114.113.152}"
edge_key="${RELAY_EDGE_SSH_KEY:-/Users/papa/.ssh/feishu_relay_edge_ed25519}"
edge_ssh_control_path="${RELAY_EDGE_SSH_CONTROL_PATH:-}"
source_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
target_root="$source_root/workflows/edge-relay"
edge_container="${RELAY_EDGE_N8N_CONTAINER:-feishu-relay-edge-n8n}"
ids=(mediaStateFlow123 mediaPartFlow123 mediaFinalize123 xo3AHKRr4MFXrzFA)
[[ -r "$edge_key" ]] || { echo "edge SSH key is not readable: $edge_key" >&2; exit 2; }
ssh_command=(ssh -i "$edge_key" -o BatchMode=yes -o IdentitiesOnly=yes -o StrictHostKeyChecking=yes)
[[ -z "$edge_ssh_control_path" ]] || ssh_command+=(-S "$edge_ssh_control_path" -o ControlMaster=no -o BatchMode=yes)

for command in ssh tar jq mktemp; do command -v "$command" >/dev/null || { echo "missing required command: $command" >&2; exit 127; }; done

stage_root="$(mktemp -d "${TMPDIR:-/tmp}/edge-relay-workflows.XXXXXX")"
cleanup() { rm -rf -- "$stage_root"; }
trap cleanup EXIT
remote_dir="/tmp/edge-relay-workflows-${RANDOM}-$$"
remote_base="${remote_dir##*/}"
remote_archive_base="$("${ssh_command[@]}" "$edge_host" "docker exec '$edge_container' printenv REMOTE_ANALYST_ARCHIVE_BASE_URL")"
[[ -n "$remote_archive_base" ]] || { echo "remote archive base URL is not configured" >&2; exit 1; }

"${ssh_command[@]}" "$edge_host" "docker exec '$edge_container' sh -lc 'mkdir -p \"$remote_dir\" && n8n export:workflow --all --published --separate --output=\"$remote_dir\" >/dev/null' && docker cp '$edge_container:$remote_dir' - && docker exec '$edge_container' rm -rf '$remote_dir'" \
  | tar -xf - -C "$stage_root"

mkdir -p "$stage_root/workflows"
for id in "${ids[@]}"; do
  input="$stage_root/$remote_base/${id}.json"
  [[ -f "$input" ]] || { echo "remote export missing $id" >&2; exit 1; }
  jq -e 'type == "object" and (.id | type == "string") and (.nodes | type == "array") and (.connections | type == "object")' "$input" >/dev/null
  jq -S --arg remote_archive_base "$remote_archive_base" '
    def redact_archive_base: split($remote_archive_base) | join("__REMOTE_ANALYST_ARCHIVE_BASE_URL__");
    walk(if type == "string" then redact_archive_base else . end)
  ' "$input" > "$stage_root/workflows/${id}.json"
done

jq -n --arg generated_at "$(date -u +%Y-%m-%dT%H:%M:%SZ)" --arg source "remote-edge" \
  --slurpfile workflows <(jq -s '[.[] | {id, name, active, versionId}] | sort_by(.id)' "$stage_root"/workflows/*.json) \
  '{schema_version: 1, generated_at: $generated_at, source: $source, workflows: $workflows[0]}' > "$stage_root/manifest.json"
rm -rf -- "$stage_root/$remote_base"

if [[ -e "$target_root" ]]; then
  backup_root="${target_root}.previous.$(date -u +%Y%m%d-%H%M%S)"
  mv "$target_root" "$backup_root"
  mv "$stage_root" "$target_root"
  rm -rf -- "$backup_root"
else
  mv "$stage_root" "$target_root"
fi
trap - EXIT
printf 'workflow_source=%s\n' "$target_root"
