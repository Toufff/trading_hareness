#!/usr/bin/env bash
# Replace the combined scheduler with independent bounded report/message
# schedulers. n8n keeps only the encrypted bearer credential; quant-research
# owns durable delta cursors, detail imports, rate limiting, and the
# text-only/no-history contract.
# HTTP nodes are built with the gateway expression: ={{ $env.QUANT_SERVICE_URL || 'http://quant-research-gateway:8000' }}
set -euo pipefail

DOCKER="${DOCKER:-docker}"
legacy_workflow_id="remoteArchiveSync123"
workflow_ids=("remoteArchiveReports123" "remoteArchiveMessages123")
local_credential_id="remoteArchiveLocalBearer123"
local_credential_name="Bearer Auth account (quant-local)"
if ! grep -q '^REMOTE_ANALYST_ARCHIVE_BASE_URL=https://' .env 2>/dev/null; then
  echo 'REMOTE_ANALYST_ARCHIVE_BASE_URL must be configured in the ignored .env file' >&2
  exit 2
fi
timestamp="$(date -u +%Y%m%d-%H%M%S)"
backup_dir="backups/workflow-changes/${timestamp}-remote-archive-sync"
container_before="/tmp/remote-archive-${timestamp}-before.json"
container_after="/tmp/remote-archive-${timestamp}-after.json"
cleanup() {
  "$DOCKER" compose exec -T -u root n8n rm -f "$container_before" "$container_after" >/dev/null 2>&1 || true
}
trap cleanup EXIT

mkdir -p "$backup_dir"
source_workflow_id="$($DOCKER compose exec -T postgres psql -v ON_ERROR_STOP=1 -U n8n -d n8n -Atqc "
  SELECT id
    FROM workflow_entity
   WHERE id IN ('${legacy_workflow_id}','${workflow_ids[0]}','${workflow_ids[1]}')
   ORDER BY CASE WHEN id='${legacy_workflow_id}' THEN 0 ELSE 1 END
   LIMIT 1
")"
if [[ -z "$source_workflow_id" ]]; then
  echo 'no existing remote archive workflow was found to supply the encrypted bearer credential' >&2
  exit 2
fi
"$DOCKER" compose exec -T n8n n8n export:workflow --id="$source_workflow_id" --output="$container_before"
"$DOCKER" compose cp "n8n:${container_before}" "$backup_dir/before.json"

node scripts/build-remote-archive-sync-workflow.mjs "$backup_dir/before.json" "$backup_dir/combined.json" "$local_credential_id" "$local_credential_name"
node scripts/split-remote-archive-sync-workflows.mjs "$backup_dir/combined.json" "$backup_dir/candidate.json"
jq -e '
  type == "array" and length == 2 and
  ([.[].id] | sort == ["remoteArchiveMessages123", "remoteArchiveReports123"]) and
  (all(.[]; (.nodes | length == 3))) and
  (all(.[]; ([.nodes[] | select(.type == "n8n-nodes-base.scheduleTrigger")] | length == 1))) and
  (all(.[]; ([.nodes[] | select(.type == "n8n-nodes-base.manualTrigger")] | length == 1))) and
  ([.[] | .nodes[] | select(.type == "n8n-nodes-base.httpRequest") | .parameters.url] | all(startswith("={{ $env.QUANT_SERVICE_URL || ") and endswith(" }}/api/v1/remote-archive/sync"))) and
  ([.[] | .nodes[] | select(.type == "n8n-nodes-base.httpRequest") | .parameters.headerParameters.parameters[] | select(.name == "X-Quant-Write-Key") | .value] | all(. == "={{ $env.QUANT_WRITE_API_KEY }}")) and
  ([.[] | select(.id == "remoteArchiveReports123") | .nodes[] | select(.type == "n8n-nodes-base.httpRequest") | .parameters.jsonBody] | all(test("=\\{\\{ JSON.stringify")) and all(test("reports"))) and
  ([.[] | select(.id == "remoteArchiveReports123") | .nodes[] | select(.type == "n8n-nodes-base.httpRequest") | .parameters.jsonBody] | all(test("workflow_id")) and all(test("remoteArchiveReports123"))) and
  ([.[] | select(.id == "remoteArchiveReports123") | .nodes[] | select(.type == "n8n-nodes-base.httpRequest") | .parameters.jsonBody] | all(test("max_items: 25"))) and
  ([.[] | select(.id == "remoteArchiveMessages123") | .nodes[] | select(.type == "n8n-nodes-base.httpRequest") | .parameters.jsonBody] | all(test("=\\{\\{ JSON.stringify")) and all(test("messages")) and all(test("max_items: 20")))
  and ([.[] | select(.id == "remoteArchiveMessages123") | .nodes[] | select(.type == "n8n-nodes-base.httpRequest") | .parameters.jsonBody] | all(test("workflow_id")) and all(test("remoteArchiveMessages123")))
' "$backup_dir/candidate.json" >/dev/null

"$DOCKER" compose cp "$backup_dir/candidate.json" "n8n:${container_after}"
"$DOCKER" compose exec -T n8n n8n import:workflow --input="$container_after"

# Publish the imported editable versions and archive the combined legacy
# entity without deleting its execution history.
"$DOCKER" compose stop n8n
"$DOCKER" compose exec -T postgres psql -v ON_ERROR_STOP=1 -U n8n -d n8n <<SQL
BEGIN;
UPDATE workflow_entity
   SET active=false, "activeVersionId"=NULL, "updatedAt"=now()
 WHERE id='${legacy_workflow_id}';
UPDATE workflow_entity SET active=false, "activeVersionId"=NULL, "updatedAt"=now()
 WHERE id IN ('${workflow_ids[0]}','${workflow_ids[1]}');
WITH revision AS (
  INSERT INTO workflow_history("versionId","workflowId",authors,nodes,connections,name,description,"nodeGroups")
  SELECT gen_random_uuid()::text,id,'codex-sync',nodes,connections,name,description,"nodeGroups"
    FROM workflow_entity
   WHERE id IN ('${workflow_ids[0]}','${workflow_ids[1]}')
  RETURNING "versionId","workflowId"
), published AS (
  INSERT INTO workflow_published_version("workflowId","publishedVersionId")
  SELECT "workflowId","versionId" FROM revision
  ON CONFLICT("workflowId") DO UPDATE SET "publishedVersionId"=EXCLUDED."publishedVersionId", "updatedAt"=now()
  RETURNING "workflowId","publishedVersionId"
)
UPDATE workflow_entity w
   SET active=true, "activeVersionId"=p."publishedVersionId", "versionId"=p."publishedVersionId", "updatedAt"=now()
  FROM published p
 WHERE w.id=p."workflowId";
COMMIT;
SQL
"$DOCKER" compose start n8n
health_ok=0
for _ in {1..40}; do
  if "$DOCKER" compose exec -T n8n node -e "fetch('http://127.0.0.1:5678/healthz').then((r)=>process.exit(r.ok?0:1)).catch(()=>process.exit(1))" >/dev/null 2>&1; then
    health_ok=1
    break
  fi
  sleep 2
done
if [[ "$health_ok" != "1" ]]; then
  echo "n8n health check failed after restart" >&2
  exit 1
fi

"$DOCKER" compose exec -T -u root n8n rm -f "$container_after"
"$DOCKER" compose exec -T n8n n8n export:workflow --backup --output="$container_after"
"$DOCKER" compose cp "n8n:${container_after}/remoteArchiveReports123.json" "$backup_dir/reports-after.json"
"$DOCKER" compose cp "n8n:${container_after}/remoteArchiveMessages123.json" "$backup_dir/messages-after.json"
"$DOCKER" compose exec -T postgres psql -v ON_ERROR_STOP=1 -U n8n -d n8n -Atqc "
  SELECT count(*) FROM workflow_entity w
  JOIN workflow_published_version p ON p.\"workflowId\"=w.id
  WHERE w.id IN ('${workflow_ids[0]}','${workflow_ids[1]}') AND w.active AND w.\"activeVersionId\"=p.\"publishedVersionId\"
" | grep -qx '2'
jq -e 'type == "object" and .id == "remoteArchiveReports123" and ([.nodes[] | select(.type == "n8n-nodes-base.scheduleTrigger")] | length == 1) and ([.nodes[] | select(.type == "n8n-nodes-base.manualTrigger")] | length == 1)' "$backup_dir/reports-after.json" >/dev/null
jq -e 'type == "object" and .id == "remoteArchiveMessages123" and ([.nodes[] | select(.type == "n8n-nodes-base.scheduleTrigger")] | length == 1) and ([.nodes[] | select(.type == "n8n-nodes-base.manualTrigger")] | length == 1)' "$backup_dir/messages-after.json" >/dev/null
echo "remote archive split schedules converged; rollback export: ${backup_dir}/before.json"
