#!/usr/bin/env bash
# Publish the versioned intraday workflow definition while keeping the legacy
# n8n scheduler disabled. The quant-service leased loop is the single live
# scanner; this workflow remains an authenticated manual/recovery fallback.
set -euo pipefail

DOCKER="${DOCKER:-docker}"
workflow_id="quantIntradayAlerts123"
timestamp="$(date -u +%Y%m%d-%H%M%S)"
backup_dir="backups/workflow-changes/${timestamp}-quant-intraday-alerts"
before="/tmp/${workflow_id}-${timestamp}-before.json"
candidate="/tmp/${workflow_id}-${timestamp}-candidate.json"
after="/tmp/${workflow_id}-${timestamp}-after.json"
cleanup() { "$DOCKER" compose exec -T -u root n8n rm -f "$before" "$candidate" "$after" >/dev/null 2>&1 || true; }
trap cleanup EXIT

mkdir -p "$backup_dir"
chmod 700 "$backup_dir"
"$DOCKER" compose exec -T n8n n8n export:workflow --id="$workflow_id" --output="$before"
"$DOCKER" compose cp "n8n:${before}" "$backup_dir/before.json"
chmod 600 "$backup_dir/before.json"
jq -e '
  type == "array" and length == 1 and .[0].id == "quantIntradayAlerts123" and
  ([.[0].nodes[].name] | sort | contains(["上午连续竞价每分钟","下午连续竞价每分钟","扫描显式观察池","上午板块快报每五分钟","下午板块快报每五分钟","生成并推送板块五分钟快报"])) and
  (.[0].connections["上午连续竞价每分钟"].main[0][0].node == "扫描显式观察池") and
  (.[0].connections["下午连续竞价每分钟"].main[0][0].node == "扫描显式观察池") and
  ([.[0].nodes[] | select(.type == "n8n-nodes-base.httpRequest") | .parameters.headerParameters.parameters[] | select(.name == "X-Quant-Write-Key") | .value] | all(. == "={{ $env.QUANT_WRITE_API_KEY }}"))
' workflows/quant-intraday-alerts.json >/dev/null
"$DOCKER" compose cp workflows/quant-intraday-alerts.json "n8n:${candidate}"
"$DOCKER" compose exec -T n8n n8n import:workflow --input="$candidate"

# Keep this legacy graph explicitly unpublished.  `update:workflow` updates
# the scheduler registry as well as workflow_entity; a direct SQL toggle alone
# leaves an already-running n8n scheduler with stale cron registrations.
"$DOCKER" compose exec -T n8n n8n update:workflow --id="$workflow_id" --active=false >/dev/null
"$DOCKER" compose restart n8n >/dev/null
for _ in {1..20}; do curl -fsS --max-time 2 http://127.0.0.1:5678/healthz >/dev/null && break; sleep 2; done
curl -fsS --max-time 2 http://127.0.0.1:5678/healthz >/dev/null
"$DOCKER" compose exec -T n8n n8n export:workflow --id="$workflow_id" --output="$after"
"$DOCKER" compose cp "n8n:${after}" "$backup_dir/after.json"
jq -e 'type == "array" and .[0].active == false and .[0].connections["上午连续竞价每分钟"].main[0][0].node == "扫描显式观察池" and .[0].connections["下午连续竞价每分钟"].main[0][0].node == "扫描显式观察池"' "$backup_dir/after.json" >/dev/null
echo "intraday workflow definition converged (inactive fallback); rollback export: $backup_dir/before.json"
