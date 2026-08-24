#!/usr/bin/env bash
# Publish the four committed Feishu relay webhook workflows on the edge.
#
# n8n keeps editable workflow rows in memory.  Updating those rows while it is
# running risks a later stale write restoring the old graph, so this script
# first makes a rollback export, stops n8n, publishes a new workflow-history
# version in one PostgreSQL transaction, then starts and verifies n8n again.
set -euo pipefail

usage() {
  echo "usage: $0 <git-sha-or-tag> [--apply]" >&2
  exit 2
}

[[ $# -ge 1 && $# -le 2 ]] || usage
release_ref="$1"
apply=false
[[ "${2:-}" == "--apply" ]] && apply=true
[[ "${2:-}" == "" || "${2:-}" == "--apply" ]] || usage

source_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
edge_host="${RELAY_EDGE_HOST:-root@47.114.113.152}"
edge_key="${RELAY_EDGE_SSH_KEY:-/Users/papa/.ssh/feishu_relay_edge_ed25519}"
edge_ssh_control_path="${RELAY_EDGE_SSH_CONTROL_PATH:-}"
edge_dir="${RELAY_EDGE_DIR:-/opt/feishu-relay-edge}"
runtime_env="${RELAY_EDGE_RUNTIME_ENV:-/etc/feishu-relay-edge/runtime.env}"
secrets_env="${RELAY_EDGE_SECRETS_ENV:-/etc/feishu-relay-edge/secrets.env}"
edge_container="${RELAY_EDGE_N8N_CONTAINER:-feishu-relay-edge-n8n}"
workflow_ids=(mediaStateFlow123 mediaPartFlow123 mediaFinalize123 xo3AHKRr4MFXrzFA)
release_sha="$(git -C "$source_root" rev-parse --verify "${release_ref}^{commit}")"
[[ -r "$edge_key" ]] || { echo "edge SSH key is not readable: $edge_key" >&2; exit 2; }
ssh_command=(ssh -i "$edge_key" -o BatchMode=yes -o IdentitiesOnly=yes -o StrictHostKeyChecking=yes)
[[ -z "$edge_ssh_control_path" ]] || ssh_command+=(-S "$edge_ssh_control_path" -o ControlMaster=no -o BatchMode=yes)

for command in git jq ssh tar; do
  command -v "$command" >/dev/null || { echo "missing required command: $command" >&2; exit 127; }
done

git -C "$source_root" diff --quiet --ignore-submodules -- || {
  echo "refusing to publish from an uncommitted worktree; commit the workflow revision first" >&2
  exit 1
}
git -C "$source_root" diff --cached --quiet --ignore-submodules -- || {
  echo "refusing to publish staged but uncommitted changes" >&2
  exit 1
}

for workflow_id in "${workflow_ids[@]}"; do
  git -C "$source_root" show "${release_sha}:workflows/edge-relay/workflows/${workflow_id}.json" \
    | jq -e --arg id "$workflow_id" '
        type == "object" and .id == $id and .active == true and
        (.nodes | type == "array" and length > 0) and
        (.connections | type == "object") and
        ([.. | strings | select(contains("feishu-adapter:3000"))] | length == 0)
      ' >/dev/null || {
        echo "invalid or stale relay workflow source: $workflow_id" >&2
        exit 1
      }
done

remote_stage="/tmp/edge-relay-workflow-deploy-${release_sha:0:12}-$(date -u +%Y%m%d%H%M%S)-$$"
printf 'release_sha=%s\nedge_host=%s\nremote_stage=%s\n' "$release_sha" "$edge_host" "$remote_stage"
if [[ "$apply" != true ]]; then
  echo "dry run only; append --apply after the committed revision is reviewed and pushed"
  exit 0
fi

git -C "$source_root" archive --format=tar "$release_sha" workflows/edge-relay/workflows \
  | "${ssh_command[@]}" "$edge_host" "set -euo pipefail; install -d -m 0700 '$remote_stage/source'; tar -xf - -C '$remote_stage/source'"

"${ssh_command[@]}" "$edge_host" bash -s -- \
  "$edge_dir" "$runtime_env" "$secrets_env" "$edge_container" "$remote_stage" "$release_sha" "${workflow_ids[@]}" <<'REMOTE'
set -euo pipefail
edge_dir="$1"
runtime_env="$2"
secrets_env="$3"
edge_container="$4"
remote_stage="$5"
release_sha="$6"
shift 6
workflow_ids=("$@")
backup_dir="$edge_dir/backups/workflow-deploy/$(date -u +%Y%m%d-%H%M%S)-${release_sha:0:12}"
container_export="/tmp/edge-relay-workflow-before-${release_sha:0:12}-$$"
n8n_stopped=false

cleanup() {
  if [[ "$n8n_stopped" == true ]]; then
    cd "$edge_dir" && docker compose --env-file "$runtime_env" --env-file "$secrets_env" start n8n >/dev/null 2>&1 || true
  fi
  docker exec "$edge_container" rm -rf "$container_export" >/dev/null 2>&1 || true
  rm -rf "$remote_stage"
}
trap cleanup EXIT

test -f "$runtime_env"
test -f "$secrets_env"
test "${#workflow_ids[@]}" -eq 4
cd "$edge_dir"
docker exec "$edge_container" printenv REMOTE_ANALYST_ARCHIVE_BASE_URL >"$remote_stage/remote_archive_base"
remote_archive_base="$(<"$remote_stage/remote_archive_base")"
[[ -n "$remote_archive_base" ]] || { echo "REMOTE_ANALYST_ARCHIVE_BASE_URL is not configured" >&2; exit 1; }

install -d -m 0750 "$backup_dir"
docker exec "$edge_container" sh -lc "mkdir -p '$container_export' && n8n export:workflow --all --published --separate --output='$container_export' >/dev/null"
docker cp "$edge_container:$container_export/." "$backup_dir/"

install -d -m 0700 "$remote_stage/rendered"
for workflow_id in "${workflow_ids[@]}"; do
  source_file="$remote_stage/source/workflows/edge-relay/workflows/${workflow_id}.json"
  rendered_file="$remote_stage/rendered/${workflow_id}.json"
  [[ -f "$source_file" ]] || { echo "source workflow is missing: $workflow_id" >&2; exit 1; }
  jq -e --arg id "$workflow_id" --arg remote_archive_base "$remote_archive_base" '
    walk(if type == "string" then split("__REMOTE_ANALYST_ARCHIVE_BASE_URL__") | join($remote_archive_base) else . end)
    | type == "object" and .id == $id and .active == true and
      (.nodes | type == "array" and length > 0) and (.connections | type == "object") and
      ([.. | strings | select(contains("__REMOTE_ANALYST_ARCHIVE_BASE_URL__") or contains("feishu-adapter:3000"))] | length == 0)
  ' "$source_file" >"$rendered_file"
done

docker compose --env-file "$runtime_env" --env-file "$secrets_env" stop n8n
n8n_stopped=true
set -a
. "$secrets_env"
. "$runtime_env"
set +a
export PGPASSWORD="$RELAY_PGPASSWORD"

payload_file="$remote_stage/workflows.tsv"
: >"$payload_file"
for workflow_id in "${workflow_ids[@]}"; do
  printf '%s\t%s\n' "$workflow_id" "$(base64 -w 0 "$remote_stage/rendered/${workflow_id}.json")" >>"$payload_file"
done
psql -v ON_ERROR_STOP=1 -h 127.0.0.1 -U "$RELAY_PGUSER" -d "$RELAY_PGDATABASE" \
  <<SQL
BEGIN;
CREATE TEMP TABLE edge_workflow_candidate (id text PRIMARY KEY, payload_base64 text NOT NULL) ON COMMIT DROP;
\\copy edge_workflow_candidate (id, payload_base64) FROM '${payload_file}' WITH (FORMAT text)
DO \$body\$
DECLARE actual_count integer;
BEGIN
  SELECT count(*) INTO actual_count FROM edge_workflow_candidate;
  IF actual_count <> 4 THEN
    RAISE EXCEPTION 'expected 4 workflow candidates, got %', actual_count;
  END IF;
  IF EXISTS (
    SELECT 1
      FROM edge_workflow_candidate c
      LEFT JOIN workflow_entity w ON w.id = c.id
     WHERE w.id IS NULL
  ) THEN
    RAISE EXCEPTION 'refusing to create an unknown workflow entity';
  END IF;
END
\$body\$;
UPDATE workflow_entity
   SET active = false, "activeVersionId" = NULL, "updatedAt" = now()
 WHERE id IN (SELECT id FROM edge_workflow_candidate);
WITH candidate AS (
  SELECT id, convert_from(decode(payload_base64, 'base64'), 'UTF8')::jsonb AS document
    FROM edge_workflow_candidate
)
UPDATE workflow_entity w
   SET nodes = (c.document->'nodes')::json,
       connections = (c.document->'connections')::json,
       settings = COALESCE(c.document->'settings', '{}'::jsonb)::json,
       name = c.document->>'name',
       description = c.document->>'description',
       "nodeGroups" = COALESCE(c.document->'nodeGroups', '{}'::jsonb)::json,
       "updatedAt" = now()
  FROM candidate c
 WHERE w.id = c.id;
WITH revision AS (
  INSERT INTO workflow_history("versionId", "workflowId", authors, nodes, connections, name, description, "nodeGroups")
  SELECT gen_random_uuid()::text, w.id, 'edge-workflow-deploy:${release_sha:0:12}', w.nodes, w.connections, w.name, w.description, w."nodeGroups"
    FROM workflow_entity w
   WHERE w.id IN (SELECT id FROM edge_workflow_candidate)
  RETURNING "versionId", "workflowId"
), published AS (
  INSERT INTO workflow_published_version("workflowId", "publishedVersionId")
  SELECT "workflowId", "versionId" FROM revision
  ON CONFLICT("workflowId") DO UPDATE
    SET "publishedVersionId" = EXCLUDED."publishedVersionId", "updatedAt" = now()
  RETURNING "workflowId", "publishedVersionId"
)
UPDATE workflow_entity w
   SET active = true,
       "activeVersionId" = p."publishedVersionId",
       "versionId" = p."publishedVersionId",
       "updatedAt" = now()
  FROM published p
 WHERE w.id = p."workflowId";
SELECT count(*) = 4 AS published_all
  FROM workflow_entity w
  JOIN workflow_published_version p ON p."workflowId" = w.id
 WHERE w.id IN (SELECT id FROM edge_workflow_candidate)
   AND w.active
   AND w."activeVersionId" = p."publishedVersionId"
\\gset
\\if :published_all
\\else
\\quit
\\endif
COMMIT;
SQL

docker compose --env-file "$runtime_env" --env-file "$secrets_env" start n8n
n8n_stopped=false
for attempt in {1..30}; do
  curl -fsS http://127.0.0.1:5678/healthz >/dev/null && break
  sleep 2
done
curl -fsS http://127.0.0.1:5678/healthz >/dev/null
printf 'edge_workflow_backup=%s\n' "$backup_dir"
REMOTE

"$source_root/scripts/verify-edge-relay-workflows.sh"
printf 'edge relay workflows published: %s\n' "$release_sha"
