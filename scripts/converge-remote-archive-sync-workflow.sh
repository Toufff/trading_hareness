#!/usr/bin/env bash
# Replace the old Code-node fanout with one bounded scheduler. n8n keeps only
# the encrypted bearer credential; quant-research owns delta cursors, detail
# imports, rate limiting, and the text-only/no-history contract.
set -euo pipefail

DOCKER="${DOCKER:-docker}"
workflow_id="remoteArchiveSync123"
old_workflow_ids=("remoteArchiveReports123" "remoteArchiveMessages123")
if ! grep -q '^REMOTE_ANALYST_ARCHIVE_BASE_URL=https://' .env 2>/dev/null; then
  echo 'REMOTE_ANALYST_ARCHIVE_BASE_URL must be configured in the ignored .env file' >&2
  exit 2
fi
timestamp="$(date -u +%Y%m%d-%H%M%S)"
backup_dir="backups/workflow-changes/${timestamp}-remote-archive-sync"
container_before="/tmp/${workflow_id}-${timestamp}-before.json"
container_after="/tmp/${workflow_id}-${timestamp}-after.json"
cleanup() {
  "$DOCKER" compose exec -T -u root n8n rm -f "$container_before" "$container_after" >/dev/null 2>&1 || true
}
trap cleanup EXIT

mkdir -p "$backup_dir"
"$DOCKER" compose exec -T n8n n8n export:workflow --id="$workflow_id" --output="$container_before"
"$DOCKER" compose cp "n8n:${container_before}" "$backup_dir/before.json"

node scripts/build-remote-archive-sync-workflow.mjs "$backup_dir/before.json" "$backup_dir/combined.json"
node scripts/split-remote-archive-sync-workflows.mjs "$backup_dir/combined.json" "$backup_dir/candidate.json"
jq -e '
  type == "array" and length == 1 and .[0].id == "remoteArchiveSync123" and
  (.[0].nodes | length == 2) and
  ([.[0].nodes[].name] | sort == ["交易时段与盘后同步远端报告", "同步远端分析师文字"]) and
  ([.[0].nodes[] | select(.name == "同步远端分析师文字") | .parameters.url] | all(. == "http://quant-research:8000/api/v1/remote-archive/sync")) and
  ([.[0].nodes[] | select(.name == "同步远端分析师文字") | .credentials.httpBearerAuth] | all(.id != null and .name != null)) and
  ([.[0].nodes[] | select(.name == "交易时段与盘后同步远端报告") | .parameters.rule.interval[].expression] | unique | sort == ["*/15 9-11,13-14 * * 1-5", "20 18 * * 1-5"])
' "$backup_dir/candidate.json" >/dev/null

"$DOCKER" compose cp "$backup_dir/candidate.json" "n8n:${container_after}"
"$DOCKER" compose exec -T n8n n8n import:workflow --input="$container_after"

# Publish the imported editable version and archive the two obsolete active
# entities without deleting their execution history.
"$DOCKER" compose stop n8n
"$DOCKER" compose exec -T postgres psql -v ON_ERROR_STOP=1 -U n8n -d n8n <<SQL
BEGIN;
UPDATE workflow_entity
   SET active=false, "activeVersionId"=NULL, "updatedAt"=now()
 WHERE id IN ('${old_workflow_ids[0]}','${old_workflow_ids[1]}');
UPDATE workflow_entity
   SET active=false, "activeVersionId"=NULL, "updatedAt"=now()
 WHERE id='${workflow_id}';
WITH revision AS (
  INSERT INTO workflow_history("versionId","workflowId",authors,nodes,connections,name,description,"nodeGroups")
  SELECT gen_random_uuid()::text,id,'codex-sync',nodes,connections,name,description,"nodeGroups"
    FROM workflow_entity
   WHERE id='${workflow_id}'
  RETURNING "versionId","workflowId"
), published AS (
  INSERT INTO workflow_published_version("workflowId","publishedVersionId")
  SELECT "workflowId","versionId" FROM revision
  ON CONFLICT("workflowId") DO UPDATE SET "publishedVersionId"=EXCLUDED."publishedVersionId", "updatedAt"=now()
  RETURNING "workflowId","publishedVersionId"
)
UPDATE workflow_entity w
   SET active=true, "activeVersionId"=p."publishedVersionId", "updatedAt"=now()
  FROM published p
 WHERE w.id=p."workflowId";
COMMIT;
SQL
"$DOCKER" compose start n8n
for _ in {1..20}; do
  curl -fsS --max-time 2 http://127.0.0.1:5678/healthz >/dev/null && break
  sleep 2
done
curl -fsS --max-time 2 http://127.0.0.1:5678/healthz >/dev/null

"$DOCKER" compose exec -T -u root n8n rm -f "$container_after"
"$DOCKER" compose exec -T n8n n8n export:workflow --backup --output="$container_after"
"$DOCKER" compose cp "n8n:${container_after}/remoteArchiveSync123.json" "$backup_dir/after.json"
"$DOCKER" compose exec -T postgres psql -v ON_ERROR_STOP=1 -U n8n -d n8n -Atqc "
  SELECT count(*) FROM workflow_entity w
  JOIN workflow_published_version p ON p.\"workflowId\"=w.id
  WHERE w.id='${workflow_id}' AND w.active AND w.\"activeVersionId\"=p.\"publishedVersionId\"
" | grep -qx '1'
jq -e 'type == "object" and (.nodes | length == 2) and ([.nodes[].name] | sort == ["交易时段与盘后同步远端报告", "同步远端分析师文字"])' "$backup_dir/after.json" >/dev/null
echo "remote archive schedule converged; rollback export: ${backup_dir}/before.json"
