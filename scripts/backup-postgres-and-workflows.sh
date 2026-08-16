#!/usr/bin/env bash
set -euo pipefail

# A self-contained, encrypted-at-rest-by-n8n backup bundle.  The PostgreSQL
# archive is authoritative; workflow JSON makes operational recovery and code
# review possible without restoring a database first.  Credentials are *not*
# exported decrypted.
backup_root="${QUANT_BACKUP_DIR:-$(cd "$(dirname "$0")/.." && pwd)/backups}"
retention_days="${QUANT_BACKUP_RETENTION_DAYS:-14}"
compose_root="$(cd "$(dirname "$0")/.." && pwd)"

prune_only=false
dry_run=false
for argument in "$@"; do
  case "$argument" in
    --prune-only) prune_only=true ;;
    --dry-run) dry_run=true ;;
    *)
      printf 'usage: %s [--prune-only] [--dry-run]\n' "$0" >&2
      exit 64
      ;;
  esac
done

[[ "$retention_days" =~ ^[0-9]+$ ]] && ((retention_days >= 1 && retention_days <= 90)) || {
  printf 'QUANT_BACKUP_RETENTION_DAYS must be an integer from 1 to 90\n' >&2
  exit 64
}

remove_backup_directory() {
  local target="$1"
  if [[ "$dry_run" == true ]]; then
    printf 'would_prune_backup=%s\n' "$target"
  else
    # Targets originate from the strict YYYYMMDD-HHMMSS-daily glob below,
    # rooted at QUANT_BACKUP_DIR.  Do not broaden this to arbitrary *-daily.
    rm -rf -- "$target"
    printf 'pruned_backup=%s\n' "$target"
  fi
}

prune_completed_backups() {
  local candidate newest base day
  local -a daily_dirs=()
  shopt -s nullglob
  daily_dirs=("$backup_root"/????????-??????-daily)
  shopt -u nullglob

  # Retention applies only to directories produced by this script.  A failed
  # staging directory is never eligible; its EXIT trap handles it separately.
  for candidate in "${daily_dirs[@]}"; do
    if [[ $(find "$candidate" -prune -mtime "+$retention_days" -print) ]]; then
      remove_backup_directory "$candidate"
    fi
  done

  # launchd/manual invocations can create several valid archives on one
  # Shanghai date.  Keeping only the lexically latest timestamp preserves the
  # newest recoverable point while preventing a burst of manual test backups
  # from consuming the research SSD allocation.
  for candidate in "${daily_dirs[@]}"; do
    [[ -d "$candidate" ]] || continue
    base="${candidate##*/}"
    day="${base%%-*}"
    newest=""
    for newest_candidate in "${daily_dirs[@]}"; do
      [[ -d "$newest_candidate" && "${newest_candidate##*/}" == "$day"-* ]] || continue
      if [[ -z "$newest" || "$newest_candidate" > "$newest" ]]; then
        newest="$newest_candidate"
      fi
    done
    [[ -z "$newest" || "$candidate" == "$newest" ]] || remove_backup_directory "$candidate"
  done
}

mkdir -p "$backup_root"
if [[ "$prune_only" == true ]]; then
  prune_completed_backups
  exit 0
fi

stamp="$(TZ=Asia/Shanghai date +%Y%m%d-%H%M%S)"
backup_dir="${backup_root}/${stamp}-daily"
staging_dir="${backup_root}/.${stamp}-daily.partial"
container_tmp="/tmp/n8n-workflow-backup-${stamp}"

umask 077
command -v docker >/dev/null 2>&1 || {
  printf 'docker CLI is unavailable; no backup directory was created\n' >&2
  exit 127
}
command -v jq >/dev/null 2>&1 || {
  printf 'jq is unavailable; refusing to create an unvalidated workflow backup\n' >&2
  exit 127
}
if [[ -e "$backup_dir" || -e "$staging_dir" ]]; then
  printf 'backup target already exists; refusing to overwrite: %s\n' "$backup_dir" >&2
  exit 1
fi
mkdir "$staging_dir"
chmod 700 "$staging_dir"
completed=false

cleanup() {
  docker compose -f "$compose_root/compose.yaml" exec -T n8n rm -rf "$container_tmp" >/dev/null 2>&1 || true
  if [[ "$completed" != true && -d "$staging_dir" ]]; then
    rm -rf "$staging_dir"
  fi
}
trap cleanup EXIT

docker compose -f "$compose_root/compose.yaml" exec -T postgres pg_dump -U n8n -Fc -d n8n > "$staging_dir/n8n-postgres.dump"
docker compose -f "$compose_root/compose.yaml" exec -T postgres pg_restore -l < "$staging_dir/n8n-postgres.dump" > "$staging_dir/n8n-postgres.manifest"

docker compose -f "$compose_root/compose.yaml" exec -T n8n mkdir -p "$container_tmp"
docker compose -f "$compose_root/compose.yaml" exec -T n8n n8n export:workflow --backup --output="$container_tmp"
docker cp "n8n:${container_tmp}/." "$staging_dir/workflows"

chmod 600 "$staging_dir/n8n-postgres.dump" "$staging_dir/n8n-postgres.manifest"
chmod 700 "$staging_dir/workflows"
find "$staging_dir/workflows" -type f -exec chmod 600 {} +

# Refuse to publish a bundle whose workflow export is empty or structurally
# invalid.  The EXIT trap removes this staging directory on any failure, so a
# later opening preflight never mistakes it for a recoverable daily backup.
workflow_count=0
while IFS= read -r -d '' workflow_file; do
  jq -e 'type == "object" and (.nodes | type == "array")' "$workflow_file" >/dev/null
  workflow_count="$((workflow_count + 1))"
done < <(find "$staging_dir/workflows" -type f -name '*.json' -print0)
((workflow_count > 0)) || {
  printf 'workflow export is empty; refusing to publish backup\n' >&2
  exit 1
}

mv "$staging_dir" "$backup_dir"
completed=true

# Retention and same-date dedupe only touch complete, strictly named archives.
prune_completed_backups

printf 'backup_dir=%s\n' "$backup_dir"
printf 'postgres_archive_bytes=%s\n' "$(stat -f '%z' "$backup_dir/n8n-postgres.dump")"
printf 'workflow_files=%s\n' "$workflow_count"
