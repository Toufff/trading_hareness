#!/usr/bin/env bash
# Safely replace the remote-report sync schedule with the generated PIT-safe
# 15-minute intraday workflow. Credentials remain inside n8n; no bearer value
# is written into this repository or the generated artifact.
set -euo pipefail

workflow_id="remoteArchiveSync123"
timestamp="$(date -u +%Y%m%d-%H%M%S)"
backup_dir="backups/workflow-changes/${timestamp}-remote-archive-sync"
container_before="/tmp/${workflow_id}-${timestamp}-before.json"
container_after="/tmp/${workflow_id}-${timestamp}-after.json"
cleanup() {
  docker compose exec -T n8n rm -f "$container_before" "$container_after" >/dev/null 2>&1 || true
}
trap cleanup EXIT

mkdir -p "$backup_dir"
docker compose exec -T n8n n8n export:workflow --id="$workflow_id" --output="$container_before"
docker compose cp "n8n:${container_before}" "$backup_dir/before.json"

# Build from the existing workflow export so its credential reference—not its
# secret—is reused.  The new JSON remains in /tmp and is never committed.
node scripts/build-remote-archive-sync-workflow.mjs "$backup_dir/before.json" "$backup_dir/candidate.json"
jq -e '
  type == "array" and length == 1 and .[0].id == "remoteArchiveSync123" and .[0].active == true and
  ([.[0].nodes[] | select(.name == "交易时段与盘后同步远端报告")] | length == 1) and
  ([.[0].nodes[] | select(.name == "交易时段与盘后同步远端报告") | .parameters.rule.interval[].expression] | sort == ["*/15 9-11,13-14 * * 1-5", "20 18 * * 1-5"])
' "$backup_dir/candidate.json" >/dev/null

docker compose cp "$backup_dir/candidate.json" "n8n:${container_after}"
# n8n's import command is the supported durable path and keeps the existing
# credential reference intact.  The workflow has no active execution during
# this configuration-only update.
docker compose exec -T n8n n8n import:workflow --input="$container_after"
# In this single-main n8n deployment the CLI intentionally cannot activate on
# import.  Stop the process before the narrow DB state change so its in-memory
# copy cannot overwrite the verified schedule on restart.
docker compose stop n8n
docker compose exec -T postgres psql -v ON_ERROR_STOP=1 -U n8n -d n8n -c "UPDATE workflow_entity SET active=true WHERE id='${workflow_id}' AND nodes::jsonb @> '[{\"name\":\"交易时段与盘后同步远端报告\"}]'::jsonb;"
docker compose start n8n
for _ in {1..15}; do
  curl -fsS --max-time 2 http://127.0.0.1:5678/healthz >/dev/null && break
  sleep 2
done
curl -fsS --max-time 2 http://127.0.0.1:5678/healthz >/dev/null
docker compose exec -T -u root n8n rm -f "$container_after"
docker compose exec -T n8n n8n export:workflow --id="$workflow_id" --output="$container_after"
docker compose cp "n8n:${container_after}" "$backup_dir/after.json"
jq -e '.[0].active == true and ([.[0].nodes[] | select(.name == "交易时段与盘后同步远端报告")] | length == 1)' "$backup_dir/after.json" >/dev/null
rm -f "$backup_dir/candidate.json"
echo "remote archive schedule converged; rollback export: ${backup_dir}/before.json"
