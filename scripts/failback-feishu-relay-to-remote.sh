#!/usr/bin/env bash
# Fenced reverse handoff: make the always-on edge the relay writer again.
set -euo pipefail

edge_host="${RELAY_EDGE_HOST:-root@47.114.113.152}"
edge_dir="${RELAY_EDGE_DIR:-/opt/feishu-relay-edge}"
edge_runtime_env="${RELAY_EDGE_RUNTIME_ENV:-/etc/feishu-relay-edge/runtime.env}"
edge_key="${RELAY_EDGE_SSH_KEY:-/Users/papa/.ssh/feishu_relay_edge_ed25519}"
local_adapter_container="${LOCAL_FEISHU_ADAPTER_CONTAINER:-n8n-feishu-adapter}"
local_postgres_container="${LOCAL_N8N_POSTGRES_CONTAINER:-n8n-postgres}"
remote_writer_id="${RELAY_EDGE_WRITER_ID:-relay-edge-47}"

relay_tables=(
  ingestion_topics ingestion_publishers ingestion_source_profiles ingestion_jobs ingestion_delivery_outbox
  ingestion_content_items ingestion_assets ingestion_asset_parts ingestion_errors analysis_jobs
  feishu_group_relay_sources feishu_group_relay_messages feishu_group_relay_actions
  feishu_group_relay_routes feishu_group_relay_route_state feishu_summary_listener_state
  feishu_user_oauth_tokens feishu_relay_writer_ownership
)

for command in ssh scp docker mktemp rg; do
  command -v "$command" >/dev/null || { echo "missing required command: $command" >&2; exit 1; }
done
[[ -r "$edge_key" ]] || { echo "edge SSH key is not readable: $edge_key" >&2; exit 1; }
[[ "$remote_writer_id" =~ ^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$ ]] || { echo "invalid remote relay writer ID" >&2; exit 1; }

edge_ssh=(ssh -i "$edge_key" -o BatchMode=yes -o IdentitiesOnly=yes -o StrictHostKeyChecking=yes)
edge_scp=(scp -i "$edge_key" -o BatchMode=yes -o IdentitiesOnly=yes -o StrictHostKeyChecking=yes)
workspace_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$workspace_root"
temp_dir="$(mktemp -d "${TMPDIR:-/tmp}/feishu-relay-failback.XXXXXX")"
chmod 0700 "$temp_dir"
trap 'rm -rf "$temp_dir"' EXIT
local_dump_file="$temp_dir/relay-ledger.sql"
remote_dump_file="/tmp/feishu-relay-failback-${RANDOM}-${RANDOM}.sql"

echo "[1/6] Fencing local relay pollers..."
FEISHU_GROUP_RELAY_ENABLED=false FEISHU_SUMMARY_LISTENER_ENABLED=false \
  docker compose up -d --force-recreate --no-deps feishu-adapter >/dev/null

echo "[2/6] Taking a local durable ledger snapshot..."
pg_dump_args=(--data-only --no-owner --no-privileges)
for table in "${relay_tables[@]}"; do pg_dump_args+=("--table=public.${table}"); done
docker exec "$local_postgres_container" pg_dump -U n8n -d n8n "${pg_dump_args[@]}" >"$local_dump_file"
rg -q 'COPY public.feishu_group_relay_messages' "$local_dump_file" || { echo "local ledger snapshot is incomplete; remote remains fenced" >&2; exit 1; }

echo "[3/6] Fencing remote relay and uploading the local snapshot..."
"${edge_ssh[@]}" "$edge_host" "EDGE_DIR='$edge_dir' EDGE_RUNTIME_ENV='$edge_runtime_env' bash -s" <<'REMOTE'
set -euo pipefail
cd "$EDGE_DIR"
sed -i -E 's/^FEISHU_GROUP_RELAY_ENABLED=.*/FEISHU_GROUP_RELAY_ENABLED=false/; s/^FEISHU_SUMMARY_LISTENER_ENABLED=.*/FEISHU_SUMMARY_LISTENER_ENABLED=false/' "$EDGE_RUNTIME_ENV"
docker compose --env-file "$EDGE_RUNTIME_ENV" --env-file /etc/feishu-relay-edge/secrets.env up -d --force-recreate --no-deps feishu-adapter >/dev/null
REMOTE
"${edge_scp[@]}" "$local_dump_file" "$edge_host:$remote_dump_file"

echo "[4/6] Restoring ledger and retry-media into the edge..."
docker exec "$local_adapter_container" tar -C /var/lib/adapter-ingestion -cf - . \
  | "${edge_ssh[@]}" "$edge_host" "tar -C '$edge_dir/adapter-ingestion' -xf -"
"${edge_ssh[@]}" "$edge_host" "EDGE_DIR='$edge_dir' EDGE_RUNTIME_ENV='$edge_runtime_env' REMOTE_DUMP='$remote_dump_file' REMOTE_WRITER_ID='$remote_writer_id' bash -s" <<'REMOTE'
set -euo pipefail
cd "$EDGE_DIR"
. "$EDGE_RUNTIME_ENV"
export PGPASSWORD="$RELAY_PGPASSWORD"
psql_args=(-h 127.0.0.1 -U "$RELAY_PGUSER" -d "$RELAY_PGDATABASE" -v ON_ERROR_STOP=1)
{
  printf 'BEGIN;\n'
  printf 'TRUNCATE TABLE public.ingestion_topics,public.ingestion_publishers,public.ingestion_source_profiles,public.ingestion_jobs,public.ingestion_delivery_outbox,public.ingestion_content_items,public.ingestion_assets,public.ingestion_asset_parts,public.ingestion_errors,public.analysis_jobs,public.feishu_group_relay_sources,public.feishu_group_relay_messages,public.feishu_group_relay_actions,public.feishu_group_relay_routes,public.feishu_group_relay_route_state,public.feishu_summary_listener_state,public.feishu_user_oauth_tokens,public.feishu_relay_writer_ownership CASCADE;\n'
  cat "$REMOTE_DUMP"
  printf 'COMMIT;\n'
} | psql "${psql_args[@]}" >/dev/null
psql "${psql_args[@]}" -v writer_id="$REMOTE_WRITER_ID" -c "INSERT INTO public.feishu_relay_writer_ownership(singleton,writer_id,generation) VALUES(true, :'writer_id', 1) ON CONFLICT(singleton) DO UPDATE SET writer_id=EXCLUDED.writer_id,generation=public.feishu_relay_writer_ownership.generation+1,updated_at=now();" >/dev/null
rm -f "$REMOTE_DUMP"
REMOTE

echo "[5/6] Promoting the edge writer and enabling its pollers..."
"${edge_ssh[@]}" "$edge_host" "EDGE_DIR='$edge_dir' EDGE_RUNTIME_ENV='$edge_runtime_env' REMOTE_WRITER_ID='$remote_writer_id' bash -s" <<'REMOTE'
set -euo pipefail
replace_or_append() {
  local key="$1" value="$2" file="$3"
  if rg -q "^${key}=" "$file"; then sed -i -E "s|^${key}=.*|${key}=${value}|" "$file"; else printf '%s=%s\n' "$key" "$value" >>"$file"; fi
}
cd "$EDGE_DIR"
replace_or_append FEISHU_GROUP_RELAY_ENABLED true "$EDGE_RUNTIME_ENV"
replace_or_append FEISHU_SUMMARY_LISTENER_ENABLED true "$EDGE_RUNTIME_ENV"
replace_or_append FEISHU_RELAY_WRITER_ID "$REMOTE_WRITER_ID" "$EDGE_RUNTIME_ENV"
docker compose --env-file "$EDGE_RUNTIME_ENV" --env-file /etc/feishu-relay-edge/secrets.env up -d --force-recreate --no-deps feishu-adapter >/dev/null
REMOTE

echo "[6/6] Verifying remote relay ownership..."
"${edge_ssh[@]}" "$edge_host" "curl -fsS http://127.0.0.1:18300/api/group-relay/status" | node -e '
let body=""; process.stdin.on("data", (chunk) => { body += chunk; }); process.stdin.on("end", () => {
  const status = JSON.parse(body); if (status.writer?.state !== "writer") throw new Error(`remote relay writer fence is ${status.writer?.state ?? "unknown"}`);
  console.log(`remote relay=${status.status}; writer=${status.writer.owner_id}; generation=${status.writer.generation}`);
});'

echo "Failback complete. Local pollers remain fenced; the edge now owns forwarding."
