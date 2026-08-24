#!/usr/bin/env bash
# Read-only guard before the writer-handoff scripts. It proves that the edge
# writer is reachable and the workstation standby is fenced, without changing
# polling, the ledger, or OAuth state.
set -euo pipefail

edge_host="${RELAY_EDGE_HOST:-root@47.114.113.152}"
edge_key="${RELAY_EDGE_SSH_KEY:-/Users/papa/.ssh/feishu_relay_edge_ed25519}"
edge_ssh_control_path="${RELAY_EDGE_SSH_CONTROL_PATH:-}"
local_adapter_health_url="${LOCAL_FEISHU_ADAPTER_HEALTH_URL:-http://127.0.0.1:5680/health}"
local_adapter_status_url="${LOCAL_FEISHU_ADAPTER_STATUS_URL:-http://127.0.0.1:5680/api/group-relay/status}"
minimum_edge_available_gib="${RELAY_EDGE_MIN_AVAILABLE_GIB:-5}"

for command in ssh curl jq awk; do command -v "$command" >/dev/null || { echo "missing required command: $command" >&2; exit 127; }; done
[[ -r "$edge_key" ]] || { echo "edge SSH key is not readable: $edge_key" >&2; exit 2; }
ssh_command=(ssh -i "$edge_key" -o BatchMode=yes -o IdentitiesOnly=yes -o StrictHostKeyChecking=yes)
[[ -z "$edge_ssh_control_path" ]] || ssh_command+=(-S "$edge_ssh_control_path" -o ControlMaster=no -o BatchMode=yes)

curl -fsS "$local_adapter_health_url" | jq -e '.status == "ok"' >/dev/null
local_status="$(curl -fsS "$local_adapter_status_url")"
printf '%s' "$local_status" | jq -e '.enabled == false and (.summary_listener.state == "disabled")' >/dev/null || {
	echo "local standby is not fenced; refuse a handoff preflight" >&2
	exit 1
}

remote_snapshot="$("${ssh_command[@]}" "$edge_host" bash -s -- "$minimum_edge_available_gib" <<'REMOTE'
set -euo pipefail
minimum_gib="$1"
curl -fsS http://127.0.0.1:5678/healthz | jq -e '.status == "ok"' >/dev/null
curl -fsS http://127.0.0.1:18300/health | jq -e '.status == "ok"' >/dev/null
status="$(curl -fsS http://127.0.0.1:18300/api/group-relay/status)"
printf '%s' "$status" | jq -e '.writer.state == "writer" and .enabled == true and .summary_listener.state == "healthy"' >/dev/null
available_gib="$(df -BG / | awk 'NR == 2 { gsub(/G/, "", $4); print $4 }')"
[[ "$available_gib" =~ ^[0-9]+$ ]] && (( available_gib >= minimum_gib )) || { echo "edge disk availability below threshold: ${available_gib:-unknown} GiB" >&2; exit 1; }
printf '%s' "$status" | jq -c --argjson available_gib "$available_gib" '{writer:.writer, relay_status:.status, summary_listener:.summary_listener.state, delivery_outbox:.delivery_outbox, edge_available_gib:$available_gib}'
REMOTE
)"

printf 'handoff_preflight=ok\nlocal=standby_fenced\nedge=%s\n' "$remote_snapshot"
