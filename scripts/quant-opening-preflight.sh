#!/usr/bin/env bash
# Read-only opening preflight for the local quant/Feishu control plane.
set -euo pipefail

repo_root="$(cd "$(dirname "$0")/.." && pwd)"
compose=(docker compose -f "$repo_root/compose.yaml")
quant_health_url="${QUANT_HEALTH_URL:-http://127.0.0.1:5681/health}"
intraday_status_url="${QUANT_INTRADAY_STATUS_URL:-http://127.0.0.1:5681/api/v1/intraday/services/status}"
adapter_health_url="${FEISHU_ADAPTER_HEALTH_URL:-http://127.0.0.1:5680/health}"
backup_root="${QUANT_BACKUP_DIR:-$repo_root/backups}"
require_backup=true

usage() {
  cat <<'EOF'
Usage: scripts/quant-opening-preflight.sh [--skip-backup]

Checks only the local control plane. It does not refresh quotes, fetch market
data, or issue an alert. A recent (< 30 hours) daily PostgreSQL/workflow
backup is required unless --skip-backup is given. The newest archive must
also have a matching ``pg_restore -l`` manifest and parseable workflow JSON.
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
require_command() { command -v "$1" >/dev/null 2>&1 || fail "required command unavailable: $1"; }

require_command docker
require_command curl
require_command jq
require_command cmp
require_command stat

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
      (now - (.updated_at | sub("\\.[0-9]+Z$"; "Z") | fromdateiso8601)) <= ($lease_seconds * 0.75)
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
  background_loop:ths_member_backfill
)
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
    ($book.details.max_symbols >= 20 and $book.details.max_symbols <= 40))
' <<<"$intraday_json" >/dev/null || fail 'Feishu delivery or one-minute board rotation control path is degraded'
pass 'Feishu delivery, one-minute board rotation and bounded five-level observation path are ready'

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
  pass "recent recoverable backup validated: $latest_path ($workflow_count workflows)"
fi

printf 'Opening preflight passed. No market provider was called and no alert was sent.\n'
