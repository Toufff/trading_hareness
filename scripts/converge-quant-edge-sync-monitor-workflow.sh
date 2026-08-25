#!/usr/bin/env bash
# Publish the lightweight n8n monitor without putting evidence transfer in n8n.
set -euo pipefail

DOCKER="${DOCKER:-docker}"
workflow_id="quantEdgeSyncMonitor123"
workflow_file="workflows/quant-edge-sync-monitor.json"
timestamp="$(date -u +%Y%m%d-%H%M%S)"
backup_dir="backups/workflow-changes/${timestamp}-quant-edge-sync-monitor"
before="/tmp/${workflow_id}-${timestamp}-before.json"
candidate="/tmp/${workflow_id}-${timestamp}-candidate.json"
after="/tmp/${workflow_id}-${timestamp}-after.json"

cleanup() {
  "$DOCKER" compose exec -T -u root n8n rm -f "$before" "$candidate" "$after" >/dev/null 2>&1 || true
}
trap cleanup EXIT

jq -e '
  type == "array" and length == 1 and .[0].id == "quantEdgeSyncMonitor123" and .[0].active == true and
  ([.[0].nodes[].name] | sort | contains(["每五分钟检查同步", "读取远端同步状态", "判定并抑制重复告警", "发送飞书同步告警"])) and
  ([.[0].nodes[] | select(.name == "发送飞书同步告警") | .parameters.headerParameters.parameters[] | select(.name == "X-Quant-Alert-Token") | .value] | all(. == "={{ $env.QUANT_ALERT_WEBHOOK_TOKEN }}"))
' "$workflow_file" >/dev/null

mkdir -p "$backup_dir"
chmod 700 "$backup_dir"
if "$DOCKER" compose exec -T n8n n8n export:workflow --id="$workflow_id" --output="$before" >/dev/null 2>&1; then
  "$DOCKER" compose cp "n8n:${before}" "$backup_dir/before.json"
  chmod 600 "$backup_dir/before.json"
fi

"$DOCKER" compose cp "$workflow_file" "n8n:${candidate}"
"$DOCKER" compose exec -T n8n n8n import:workflow --input="$candidate" >/dev/null
"$DOCKER" compose exec -T n8n n8n update:workflow --id="$workflow_id" --active=true >/dev/null
"$DOCKER" compose exec -T n8n n8n export:workflow --id="$workflow_id" --output="$after"
"$DOCKER" compose cp "n8n:${after}" "$backup_dir/after.json"
jq -e 'type == "array" and .[0].id == "quantEdgeSyncMonitor123" and .[0].active == true' "$backup_dir/after.json" >/dev/null
echo "edge-sync monitor workflow converged (active); backup: $backup_dir"
