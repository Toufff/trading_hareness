#!/usr/bin/env bash
# Safely replace the remote-report sync schedule with the generated PIT-safe
# 15-minute intraday workflow. Credentials remain inside n8n; no bearer value
# is written into this repository or the generated artifact.  The remote base
# URL is injected from the ignored .env file into n8n as
# REMOTE_ANALYST_ARCHIVE_BASE_URL.
set -euo pipefail

workflow_id="remoteArchiveSync123"
reports_workflow_id="remoteArchiveReports123"
messages_workflow_id="remoteArchiveMessages123"
if ! grep -q '^REMOTE_ANALYST_ARCHIVE_BASE_URL=https://' .env 2>/dev/null; then
  echo 'REMOTE_ANALYST_ARCHIVE_BASE_URL must be configured in the ignored .env file' >&2
  exit 2
fi
timestamp="$(date -u +%Y%m%d-%H%M%S)"
backup_dir="backups/workflow-changes/${timestamp}-remote-archive-sync"
container_before="/tmp/${workflow_id}-${timestamp}-before.json"
container_after="/tmp/${workflow_id}-${timestamp}-after"
cleanup() {
  docker compose exec -T n8n rm -f "$container_before" >/dev/null 2>&1 || true
  docker compose exec -T n8n rm -rf "$container_after" >/dev/null 2>&1 || true
}
trap cleanup EXIT

mkdir -p "$backup_dir"
docker compose exec -T n8n n8n export:workflow --id="$workflow_id" --output="$container_before"
docker compose cp "n8n:${container_before}" "$backup_dir/before.json"

# Build from the existing workflow export so its credential reference—not its
# secret—is reused.  The new JSON remains in /tmp and is never committed.
node scripts/build-remote-archive-sync-workflow.mjs "$backup_dir/before.json" "$backup_dir/combined.json"
node scripts/split-remote-archive-sync-workflows.mjs "$backup_dir/combined.json" "$backup_dir/candidate.json"
# The cursor endpoints return ``remote_analyst_id``.  Keep this defensive
# normalization in the tracked convergence path as well as the local
# generator: older ignored generator copies must not recreate
# `/analysts/undefined/...` on a future deployment.
jq 'map(.nodes |= map(if .name == "Read latest report page" or .name == "Read message pages" then .parameters.url |= sub("\\$json\\.analyst_id"; "$json.remote_analyst_id") else . end))' \
  "$backup_dir/candidate.json" > "$backup_dir/candidate.normalized.json"
mv "$backup_dir/candidate.normalized.json" "$backup_dir/candidate.json"
jq -e '
  type == "array" and length == 2 and
  ([.[].id] | sort == ["remoteArchiveMessages123", "remoteArchiveReports123"]) and
  ([.[] | select(.id == "remoteArchiveReports123") | .nodes[] | select(.name == "Read report cursor")] | length == 1) and
  ([.[] | select(.id == "remoteArchiveMessages123") | .nodes[] | select(.name == "Select global message delta")] | length == 1) and
  ([.[] | select(.id == "remoteArchiveReports123") | .nodes[] | select(.name == "Read latest report page") | .parameters.url | contains("remote_analyst_id")] | all) and
  ([.[] | select(.id == "remoteArchiveMessages123") | .nodes[] | select(.name == "Read message updates") | .parameters.url | contains("/messages/updates")] | all) and
  ([.[] | .nodes[] | select(.name == "交易时段与盘后同步远端报告") | .parameters.rule.interval[].expression] | unique | sort == ["*/15 9-11,13-14 * * 1-5", "20 18 * * 1-5"])
' "$backup_dir/candidate.json" >/dev/null

docker compose cp "$backup_dir/candidate.json" "n8n:${container_after}"
# n8n's import command is the supported durable path and keeps the existing
# credential reference intact.  The workflow has no active execution during
# this configuration-only update.
docker compose exec -T n8n n8n import:workflow --input="$container_after"
# n8n 2.x schedules only a *published* version.  `import:workflow` updates the
# editable entity but deliberately clears its published snapshot, so merely
# toggling `active=true` leaves a workflow that looks active in SQL yet never
# appears in n8n's active-trigger list.  Make a history snapshot and publish it
# atomically while n8n is stopped.  Credentials remain references in `nodes`.
docker compose stop n8n
docker compose exec -T postgres psql -v ON_ERROR_STOP=1 -U n8n -d n8n <<SQL
BEGIN;
UPDATE workflow_entity SET active=false, "activeVersionId"=NULL, "updatedAt"=now() WHERE id='${workflow_id}';
WITH revision AS (
  INSERT INTO workflow_history("versionId","workflowId",authors,nodes,connections,name,description,"nodeGroups")
  SELECT gen_random_uuid()::text,id,'codex-sync',nodes,connections,name,description,"nodeGroups"
    FROM workflow_entity
   WHERE id IN ('${reports_workflow_id}','${messages_workflow_id}')
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
docker compose start n8n
for _ in {1..15}; do
  curl -fsS --max-time 2 http://127.0.0.1:5678/healthz >/dev/null && break
  sleep 2
done
curl -fsS --max-time 2 http://127.0.0.1:5678/healthz >/dev/null
docker compose exec -T -u root n8n rm -rf "$container_after"
docker compose exec -T n8n n8n export:workflow --backup --output="$container_after"
docker compose cp "n8n:${container_after}/." "$backup_dir/after-workflows"
docker compose exec -T postgres psql -v ON_ERROR_STOP=1 -U n8n -d n8n -Atqc "SELECT count(*) FROM workflow_entity w JOIN workflow_published_version p ON p.\"workflowId\"=w.id WHERE w.id IN ('${reports_workflow_id}','${messages_workflow_id}') AND w.active AND w.\"activeVersionId\"=p.\"publishedVersionId\"" | grep -qx '2'
jq -e 'type == "object" and (.nodes | type == "array")' "$backup_dir/after-workflows/${reports_workflow_id}.json" >/dev/null
jq -e 'type == "object" and (.nodes | type == "array")' "$backup_dir/after-workflows/${messages_workflow_id}.json" >/dev/null
rm -f "$backup_dir/candidate.json"
echo "remote archive schedule converged; rollback export: ${backup_dir}/before.json"
