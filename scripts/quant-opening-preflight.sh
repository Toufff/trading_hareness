#!/usr/bin/env bash
# Read-only opening preflight for the local quant/Feishu control plane.
set -euo pipefail

repo_root="$(cd "$(dirname "$0")/.." && pwd)"
compose=(docker compose -f "$repo_root/compose.yaml")
quant_health_url="${QUANT_HEALTH_URL:-http://127.0.0.1:5681/health}"
intraday_status_url="${QUANT_INTRADAY_STATUS_URL:-http://127.0.0.1:5681/api/v1/intraday/services/status}"
analyst_sync_health_url="${QUANT_ANALYST_SYNC_HEALTH_URL:-http://127.0.0.1:5681/api/v1/analyst-research/sync-health}"
adapter_health_url="${FEISHU_ADAPTER_HEALTH_URL:-http://127.0.0.1:5680/health}"
backup_root="${QUANT_BACKUP_DIR:-$repo_root/backups}"
backup_max_bytes="${QUANT_BACKUP_MAX_BYTES:-8589934592}"
require_backup=true
warnings=0

usage() {
  cat <<'EOF'
Usage: scripts/quant-opening-preflight.sh [--skip-backup]

Checks only the local control plane. It does not refresh quotes, fetch market
data, or issue an alert. A recent (< 30 hours) daily PostgreSQL/workflow
backup is required unless --skip-backup is given. The newest archive must
also have a matching ``pg_restore -l`` manifest and parseable workflow JSON.
Completed daily archives must remain within ``QUANT_BACKUP_MAX_BYTES``.
The normal check also performs a non-mutating capacity preview for the next
daily archive.
EOF
}

while (($#)); do
  case "$1" in
    --skip-backup) require_backup=false ;;
    -h|--help) usage; exit 0 ;;
    *) printf 'unknown option: %s\n' "$1" >&2; usage >&2; exit 2 ;;
  esac
  shift
done

fail() { printf 'FAIL: %s\n' "$*" >&2; exit 1; }
pass() { printf 'PASS: %s\n' "$*"; }
warn() { printf 'WARN: %s\n' "$*" >&2; warnings="$((warnings + 1))"; }
require_command() { command -v "$1" >/dev/null 2>&1 || fail "required command unavailable: $1"; }

require_command docker
require_command curl
require_command jq
require_command cmp
require_command stat
require_command du

[[ "$backup_max_bytes" =~ ^[0-9]+$ ]] && ((backup_max_bytes >= 1073741824 && backup_max_bytes <= 8589934592)) || {
  fail 'QUANT_BACKUP_MAX_BYTES must be an integer from 1073741824 to 8589934592'
}

daily_backup_bytes() {
  local candidate kib total_kib=0
  shopt -s nullglob
  for candidate in "$backup_root"/????????-??????-daily; do
    [[ -d "$candidate" ]] || continue
    kib="$(du -sk "$candidate" | awk 'NR == 1 { print $1 }')"
    [[ "$kib" =~ ^[0-9]+$ ]] || fail "could not measure daily backup: $candidate"
    total_kib="$((total_kib + kib))"
  done
  shopt -u nullglob
  printf '%s\n' "$((total_kib * 1024))"
}

# Keep this check self-contained and regression-testable without importing the
# production service image.  The lease condition below intentionally allows a
# small clock/renewal grace at the normal loop boundary.

for service in postgres n8n feishu-adapter quant-research; do
  "${compose[@]}" ps --status running --services | grep -Fxq "$service" || fail "compose service is not running: $service"
done
pass 'required compose services are running'

health_json="$(curl --fail --silent --show-error --max-time 5 "$quant_health_url")" || fail 'quant health endpoint is unavailable'
jq -e '
  .status == "ok" and
  .database_pool.open == true and
  .database_pool.available >= 1 and
  (.runtime_leases.background_loop_lease_seconds | type == "number") and
  .runtime_leases.background_loop_lease_seconds >= 60 and
  .runtime_leases.background_loop_lease_seconds <= 600 and
  (.runtime_leases.background_loop_lease_seconds as $lease_seconds |
    ([.runtime_leases.background_loops[] |
      # A board-flow loop intentionally runs every 60 seconds while its lease
      # is 120 seconds.  The opening gate must reject an expired lease, but a
      # healthy loop can legitimately be near the lease boundary between
      # renewals; a 75% cutoff therefore creates false negatives.
      (now - (.updated_at | sub("\\.[0-9]+Z$"; "Z") | fromdateiso8601)) <= ($lease_seconds + 5)
    ] | all)) and
  .provider_rate_limits.process_local_limiter == true and
  .provider_rate_limits.shared_database_reservation == true and
  (.provider_rate_limits.shared_max_wait_seconds | type == "number") and
  .provider_rate_limits.shared_max_wait_seconds >= 0 and
  .provider_rate_limits.shared_max_wait_seconds <= 30 and
  .intraday_automation.enabled == true and
  .intraday_automation.normal_scan_interval_seconds == 30 and
  .intraday_automation.special_window_scan_interval_seconds == 10 and
  .intraday_automation.super_get_fast_interval_seconds == 1 and
  .intraday_automation.board_curve_enabled == true and
  .intraday_automation.board_curve_interval_seconds == 60 and
  ([.blocking_executors[] | .available_slots >= 0] | all)
' <<<"$health_json" >/dev/null || fail 'quant local health lacks an opening-ready control-plane condition'
required_leases=(
  background_loop:intraday_monitor
  background_loop:super_get_fast_quote
  background_loop:tencent_order_book
  background_loop:board_flow_curve
  background_loop:minute_profile_capture
  background_loop:strategy_review
  background_loop:post_close_strategy
  background_loop:daily_strategy_summary
)
# Membership backfills are intentionally optional: they can be disabled while
# historical/member refresh work is paused.  The health payload is the only
# source of truth for whether the current process was configured to own them.
while IFS= read -r optional_lease; do
  [[ -n "$optional_lease" ]] && required_leases+=("$optional_lease")
done < <(jq -r '.optional_background_tasks | to_entries[] | select(.value == true) | .key' <<<"$health_json")
for lease_key in "${required_leases[@]}"; do
  jq -e --arg lease_key "$lease_key" \
    '[.runtime_leases.background_loops[].lease_key] | index($lease_key) != null' \
    <<<"$health_json" >/dev/null || fail "required background lease is absent: $lease_key"
done
pass 'quant database, shared provider pacing, required leases, executor, 30s/10s/1s and 60s board settings are opening-ready'

intraday_json="$(curl --fail --silent --show-error --max-time 5 "$intraday_status_url")" || fail 'intraday status endpoint is unavailable'
jq -e '
  .summary.decision_path_degraded == false and
  (first(.items[] | select(.key == "feishu_alert")) as $feishu |
    $feishu.configured == true and $feishu.state == "ready") and
  (first(.items[] | select(.key == "eastmoney_board_flow")) as $board |
    $board.configured == true and $board.cadence == "上交所观察时段 09:20 起每 60 秒追加曲线") and
  (first(.items[] | select(.key == "tencent_order_book")) as $book |
    $book.configured == true and $book.cadence == "显式观察池批量每 3 秒" and
    $book.details.max_symbols == 40 and $book.details.uncovered_watch_count == 0)
' <<<"$intraday_json" >/dev/null || fail 'Feishu delivery or one-minute board rotation control path is degraded'
pass 'Feishu delivery, one-minute board rotation and bounded five-level observation path are ready'

# Analyst evidence is deliberately not part of the live strategy/alert gate
# until an approved promotion exists.  Still surface a current-workflow failure
# at opening so a stale text context is never mistaken for a verified feed.
if analyst_sync_json="$(curl --fail --silent --show-error --max-time 5 "$analyst_sync_health_url")"; then
  if jq -e '
    .runtime_verification == "verified_recent_execution" and
    ([.stream_health[] | .status == "ready"] | all) and
    ([.workflow_health[] | .status == "ready"] | all)
  ' <<<"$analyst_sync_json" >/dev/null; then
    pass 'analyst report/message sync has current published-workflow success evidence'
  else
    verification="$(jq -r '.runtime_verification // "unknown"' <<<"$analyst_sync_json")"
    warn "analyst report/message sync lacks current-workflow success evidence (${verification}); analyst live weight remains zero"
  fi
else
  warn 'analyst sync-health endpoint is unavailable; analyst live weight must remain zero'
fi

# The analyst workflows deliberately use small independent pages.  A large
# page turns unchanged report-detail checks into a multi-minute n8n request
# and recreates the timeout/429 failure mode before the service-side durable
# cursor can do its work.  It reads the published history revision rather
# than the editable workflow draft, and neither calls the archive nor exposes
# the encrypted Bearer credential.
analyst_sync_page_count="$("${compose[@]}" exec -T postgres psql -U n8n -d n8n -Atqc "
  WITH http_nodes AS (
    SELECT w.id,w.active,
           (w.\"activeVersionId\" IS NOT NULL AND w.\"activeVersionId\"=p.\"publishedVersionId\") AS published,
           n.node->'parameters'->>'jsonBody' AS json_body
      FROM public.workflow_entity w
      JOIN public.workflow_published_version p ON p.\"workflowId\"=w.id
      JOIN public.workflow_history h ON h.\"workflowId\"=w.id AND h.\"versionId\"=w.\"activeVersionId\"
      CROSS JOIN LATERAL jsonb_array_elements(h.nodes::jsonb) AS n(node)
     WHERE w.id IN ('remoteArchiveReports123','remoteArchiveMessages123')
       AND n.node->>'type'='n8n-nodes-base.httpRequest'
  )
  SELECT count(*) FROM http_nodes
   WHERE active AND published AND (
     (id='remoteArchiveReports123' AND json_body LIKE '%streams: [\"reports\"]%' AND json_body LIKE '%max_items: 25%') OR
     (id='remoteArchiveMessages123' AND json_body LIKE '%streams: [\"messages\"]%' AND json_body LIKE '%max_items: 20%')
   )
")"
[[ "$analyst_sync_page_count" == "2" ]] || fail 'published analyst sync workflows are missing their bounded 25/20 text-only pages'
pass 'published analyst report/message workflows use bounded 25/20 text-only pages'

adapter_json="$(curl --fail --silent --show-error --max-time 5 "$adapter_health_url")" || fail 'Feishu adapter health endpoint is unavailable'
jq -e '.status == "ok" and .quant_alert_configured == true' <<<"$adapter_json" >/dev/null || fail 'Feishu adapter is not configured for quant alerts'
pass 'Feishu adapter health is ready'

migration_version="$("${compose[@]}" exec -T postgres psql -U n8n -d n8n -Atc 'SELECT version_num FROM quant.alembic_version' | tr -d '[:space:]')"
[[ -n "$migration_version" ]] || fail 'quant Alembic revision is missing'
pass "quant schema revision is applied: $migration_version"

if [[ "$require_backup" == true ]]; then
  [[ -d "$backup_root" ]] || fail "backup directory is missing: $backup_root"
  latest_backup="$(find "$backup_root" -mindepth 1 -maxdepth 1 -type d -name '*-daily' -exec stat -f '%m %N' {} \; | sort -nr | head -n 1)"
  [[ -n "$latest_backup" ]] || fail 'no daily PostgreSQL/workflow backup exists'
  latest_epoch="${latest_backup%% *}"
  latest_path="${latest_backup#* }"
  backup_age_seconds="$(( $(date +%s) - latest_epoch ))"
  ((backup_age_seconds >= 0 && backup_age_seconds <= 108000)) || fail "latest daily backup is older than 30 hours: $latest_path"
  [[ -s "$latest_path/n8n-postgres.dump" && -s "$latest_path/n8n-postgres.manifest" && -d "$latest_path/workflows" ]] || fail "latest backup is incomplete: $latest_path"
  [[ "$(stat -f '%Lp' "$latest_path/n8n-postgres.dump")" == "600" ]] || fail "backup dump permissions are not 600: $latest_path"
  [[ "$(stat -f '%Lp' "$latest_path/n8n-postgres.manifest")" == "600" ]] || fail "backup manifest permissions are not 600: $latest_path"
  [[ "$(stat -f '%Lp' "$latest_path/workflows")" == "700" ]] || fail "backup workflow directory permissions are not 700: $latest_path"
  cmp -s "$latest_path/n8n-postgres.manifest" <("${compose[@]}" exec -T postgres pg_restore -l < "$latest_path/n8n-postgres.dump") \
    || fail "latest backup archive listing differs from its manifest: $latest_path"
  workflow_count=0
  while IFS= read -r -d '' workflow_file; do
    [[ "$(stat -f '%Lp' "$workflow_file")" == "600" ]] || fail "workflow backup permissions are not 600: $workflow_file"
    jq -e 'type == "object" and (.nodes | type == "array")' "$workflow_file" >/dev/null \
      || fail "workflow backup is not a parseable n8n export: $workflow_file"
    workflow_count="$((workflow_count + 1))"
  done < <(find "$latest_path/workflows" -type f -name '*.json' -print0)
  ((workflow_count > 0)) || fail "latest backup has no workflow exports: $latest_path"
  backup_bytes="$(daily_backup_bytes)"
  ((backup_bytes <= backup_max_bytes)) || fail "completed daily backups exceed QUANT_BACKUP_MAX_BYTES: $backup_bytes > $backup_max_bytes"
  if ! backup_capacity_preview="$(QUANT_BACKUP_DIR="$backup_root" QUANT_BACKUP_MAX_BYTES="$backup_max_bytes" \
      "$repo_root/scripts/backup-postgres-and-workflows.sh" --dry-run)"; then
    fail 'next daily backup cannot reserve managed capacity'
  fi
  grep -Fqx 'would_create_daily_backup=true' <<<"$backup_capacity_preview" >/dev/null \
    || fail 'next daily backup capacity preview did not complete'
  pass "recent recoverable backup validated: $latest_path ($workflow_count workflows)"
  pass "completed daily backups are within capacity: $backup_bytes / $backup_max_bytes bytes"
  pass 'next daily backup capacity reservation is feasible (non-mutating preview)'
fi

if ((warnings > 0)); then
  printf 'Opening preflight passed with %s non-blocking warning(s). No market provider was called and no alert was sent.\n' "$warnings"
else
  printf 'Opening preflight passed. No market provider was called and no alert was sent.\n'
fi
