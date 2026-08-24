#!/usr/bin/env bash
# Fenced, deterministic failover for the Feishu group-history relay.
#
# The remote edge is the current writer.  This script first fences its two
# pollers, snapshots its durable relay ledger, restores that snapshot into the
# local adapter database in one transaction, then recreates the local adapter
# with polling enabled.  A local process never begins polling when the current
# edge ledger cannot be read, because that would make a message sent by the
# edge but not yet present locally indistinguishable from a new source message.

set -euo pipefail

edge_host="${RELAY_EDGE_HOST:-root@47.114.113.152}"
edge_dir="${RELAY_EDGE_DIR:-/opt/feishu-relay-edge}"
edge_runtime_env="${RELAY_EDGE_RUNTIME_ENV:-/etc/feishu-relay-edge/runtime.env}"
local_adapter_container="${LOCAL_FEISHU_ADAPTER_CONTAINER:-n8n-feishu-adapter}"
local_postgres_container="${LOCAL_N8N_POSTGRES_CONTAINER:-n8n-postgres}"
local_adapter_health_url="${LOCAL_FEISHU_ADAPTER_HEALTH_URL:-http://127.0.0.1:5680/health}"

relay_tables=(
  ingestion_topics ingestion_publishers ingestion_source_profiles ingestion_jobs
  ingestion_content_items ingestion_assets ingestion_asset_parts ingestion_errors analysis_jobs
  feishu_group_relay_sources feishu_group_relay_messages feishu_group_relay_actions
  feishu_group_relay_routes feishu_group_relay_route_state feishu_summary_listener_state
  feishu_user_oauth_tokens
)

for command in ssh docker mktemp curl; do
  command -v "$command" >/dev/null || { echo "missing required command: $command" >&2; exit 1; }
done

workspace_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$workspace_root"

temp_dir="$(mktemp -d "${TMPDIR:-/tmp}/feishu-relay-failover.XXXXXX")"
chmod 0700 "$temp_dir"
trap 'rm -rf "$temp_dir"' EXIT

remote_dump_file="$temp_dir/relay-ledger.sql"

echo "[1/5] Fencing remote relay and taking a durable ledger snapshot..."
ssh "$edge_host" "EDGE_DIR='$edge_dir' EDGE_RUNTIME_ENV='$edge_runtime_env' bash -s" >"$remote_dump_file" <<'REMOTE'
set -euo pipefail
cd "$EDGE_DIR"
sed -i -E 's/^FEISHU_GROUP_RELAY_ENABLED=.*/FEISHU_GROUP_RELAY_ENABLED=false/; s/^FEISHU_SUMMARY_LISTENER_ENABLED=.*/FEISHU_SUMMARY_LISTENER_ENABLED=false/' "$EDGE_RUNTIME_ENV"
docker compose --env-file "$EDGE_RUNTIME_ENV" --env-file /etc/feishu-relay-edge/secrets.env up -d --force-recreate --no-deps feishu-adapter >/dev/null
. "$EDGE_RUNTIME_ENV"
export PGPASSWORD="$RELAY_PGPASSWORD"
pg_dump -h 127.0.0.1 -U "$RELAY_PGUSER" -d "$RELAY_PGDATABASE" --data-only --no-owner --no-privileges \
  --table=public.ingestion_topics --table=public.ingestion_publishers --table=public.ingestion_source_profiles --table=public.ingestion_jobs \
  --table=public.ingestion_content_items --table=public.ingestion_assets --table=public.ingestion_asset_parts --table=public.ingestion_errors --table=public.analysis_jobs \
  --table=public.feishu_group_relay_sources --table=public.feishu_group_relay_messages --table=public.feishu_group_relay_actions \
  --table=public.feishu_group_relay_routes --table=public.feishu_group_relay_route_state --table=public.feishu_summary_listener_state \
  --table=public.feishu_user_oauth_tokens
REMOTE

grep -q 'COPY public.feishu_group_relay_messages' "$remote_dump_file" || {
  echo "remote ledger snapshot is incomplete; local relay remains disabled" >&2
  exit 1
}

echo "[2/5] Restoring remote ledger into local PostgreSQL transactionally..."
{
  printf 'BEGIN;\n'
  printf 'TRUNCATE TABLE '
  for index in "${!relay_tables[@]}"; do
    (( index > 0 )) && printf ','
    printf 'public.%s' "${relay_tables[$index]}"
  done
  printf ' CASCADE;\n'
  cat "$remote_dump_file"
  printf 'COMMIT;\n'
} | docker exec -i "$local_postgres_container" psql -v ON_ERROR_STOP=1 -U n8n -d n8n >/dev/null

echo "[3/5] Copying any remote retry-media files to local durable storage..."
ssh "$edge_host" "tar -C '$edge_dir/adapter-ingestion' -cf - ." \
  | docker exec -i "$local_adapter_container" tar -C /var/lib/adapter-ingestion -xf -

echo "[4/5] Starting local relay and summary listener..."
FEISHU_GROUP_RELAY_ENABLED=true FEISHU_SUMMARY_LISTENER_ENABLED=true \
  docker compose up -d --force-recreate --no-deps feishu-adapter >/dev/null

deadline=$((SECONDS + 45))
until curl -fsS "$local_adapter_health_url" >/dev/null; do
  if (( SECONDS >= deadline )); then
    docker compose logs --tail=80 feishu-adapter >&2
    exit 1
  fi
  sleep 2
done

echo "[5/5] Verifying local ownership and relay health..."
curl -fsS http://127.0.0.1:5680/api/group-relay/status | node -e '
let body = "";
process.stdin.on("data", (chunk) => { body += chunk; });
process.stdin.on("end", () => {
  const status = JSON.parse(body);
  if (status.enabled !== true) throw new Error("local relay did not become enabled");
  console.log(`local relay=${status.status}; summary=${status.summary_listener?.state ?? "unknown"}; sources=${status.sources?.length ?? 0}`);
});'

echo "Failover complete. The remote pollers are fenced; local now owns forwarding."
