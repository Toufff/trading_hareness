#!/usr/bin/env bash
# Collapse the 18:50 quant workflow to the server-side daily pipeline.
#
# `POST /api/v1/pipeline/daily` already performs market sync, feature
# materialisation, outcome/scorecard settlement, and recommendation generation.
# Keeping the four downstream HTTP nodes connected made the same work run twice.
# This script first exports one small workflow-only rollback artifact, validates
# it, then rewires only the known workflow ID.  It intentionally does not call
# any quant API or market provider.
set -euo pipefail

workflow_id="quantDailyResearch123"
workflow_name="市场研究：收盘后候选池"
timestamp="$(date -u +%Y%m%d-%H%M%S)"
backup_dir="backups/workflow-changes/${timestamp}-daily-pipeline-convergence"
container_copy="/tmp/${workflow_id}-${timestamp}.json"
n8n_stopped=false

require() { command -v "$1" >/dev/null || { echo "missing required command: $1" >&2; exit 1; }; }
require docker
require jq
require curl

cleanup() {
  docker compose exec -T n8n rm -rf "$container_copy" >/dev/null 2>&1 || true
  # Updating workflow_entity while n8n still holds an in-memory copy is not
  # durable: its later persistence can overwrite the new topology.  If an
  # error happens after the deliberate stop, bring the service back before
  # returning control; the exported rollback JSON remains available.
  if [[ "$n8n_stopped" == true ]]; then
    docker compose start n8n >/dev/null 2>&1 || true
  fi
}
trap cleanup EXIT

mkdir -p "$backup_dir"
docker compose exec -T n8n n8n export:workflow --id="$workflow_id" --output="$container_copy"
docker compose cp "n8n:${container_copy}" "$backup_dir/before.json"

jq -e --arg id "$workflow_id" --arg name "$workflow_name" '
  type == "array" and length == 1 and .[0].id == $id and .[0].name == $name and .[0].active == true and
  ([.[0].nodes[] | select(.name == "同步行情与质量门禁") | .parameters.url] == ["={{ $env.QUANT_SERVICE_URL || 'http://quant-research-gateway:8000' }}/api/v1/pipeline/daily"])
' "$backup_dir/before.json" >/dev/null || {
  echo "workflow shape does not match the audited daily pipeline; no update made" >&2
  exit 1
}

# Export is complete while n8n is up.  Stop it before changing the workflow
# row so that its stale in-memory definition cannot overwrite this update.
docker compose stop n8n
n8n_stopped=true

docker compose exec -T postgres psql -v ON_ERROR_STOP=1 -U n8n -d n8n <<'SQL'
UPDATE workflow_entity
   SET connections = '{"收盘后工作日 18:50":{"main":[[{"node":"同步行情与质量门禁","type":"main","index":0}]]}}'::json,
       nodes = (
         SELECT jsonb_agg(
           node - 'disabled'
         )::json
           FROM jsonb_array_elements(nodes::jsonb) AS node
       )
 WHERE id = 'quantDailyResearch123'
   AND active = true
 RETURNING id, name, active, "versionId", "updatedAt";
SQL

actual_connections="$(docker compose exec -T postgres psql -v ON_ERROR_STOP=1 -U n8n -d n8n -Atc "SELECT connections::jsonb FROM workflow_entity WHERE id='${workflow_id}'")"
jq -e '
  keys == ["收盘后工作日 18:50"] and
  .["收盘后工作日 18:50"].main[0][0].node == "同步行情与质量门禁"
' <<<"$actual_connections" >/dev/null || {
  echo "daily workflow convergence verification failed; restore from before.json before retrying" >&2
  exit 1
}

docker compose start n8n
n8n_stopped=false
for _ in 1 2 3 4 5 6 7 8 9 10; do
  if curl --fail --silent --show-error --max-time 2 http://127.0.0.1:5678/healthz >/dev/null; then
    break
  fi
  sleep 2
done
curl --fail --silent --show-error --max-time 2 http://127.0.0.1:5678/healthz >/dev/null || {
  echo "n8n did not become healthy after workflow convergence; rollback export: ${backup_dir}/before.json" >&2
  exit 1
}

persisted_connections="$(docker compose exec -T postgres psql -v ON_ERROR_STOP=1 -U n8n -d n8n -Atc "SELECT connections::jsonb FROM workflow_entity WHERE id='${workflow_id}'")"
jq -e '
  keys == ["收盘后工作日 18:50"] and
  .["收盘后工作日 18:50"].main[0][0].node == "同步行情与质量门禁"
' <<<"$persisted_connections" >/dev/null || {
  echo "n8n restored an unexpected topology after restart; rollback export: ${backup_dir}/before.json" >&2
  exit 1
}

echo "daily workflow converged; rollback export: ${backup_dir}/before.json"
