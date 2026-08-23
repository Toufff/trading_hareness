#!/usr/bin/env bash
# Mark executions stranded by an n8n runner/broker restart as crashed without
# deleting their audit data.  It never touches recent or completed executions.
set -euo pipefail

DOCKER="${DOCKER:-docker}"
seconds="600"
audit_root="${N8N_EXECUTION_AUDIT_DIR:-backups/workflow-changes}"
retention_days="${N8N_EXECUTION_AUDIT_RETENTION_DAYS:-30}"
prune_only=false
dry_run=false
while (($#)); do
  case "$1" in
    --older-than-seconds)
      [[ $# -ge 2 ]] || {
        echo 'usage: reconcile-stale-n8n-executions.sh [--older-than-seconds 60..86400] [--prune-only] [--dry-run]' >&2
        exit 2
      }
      seconds="${2:-}"
      shift 2
      ;;
    --prune-only)
      prune_only=true
      shift
      ;;
    --dry-run)
      dry_run=true
      shift
      ;;
    *)
      echo 'usage: reconcile-stale-n8n-executions.sh [--older-than-seconds 60..86400] [--prune-only] [--dry-run]' >&2
      exit 2
      ;;
  esac
done
if ! [[ "$seconds" =~ ^[0-9]+$ ]] || (( seconds < 60 || seconds > 86400 )); then
  echo 'usage: reconcile-stale-n8n-executions.sh [--older-than-seconds 60..86400] [--prune-only] [--dry-run]' >&2
  exit 2
fi
if ! [[ "$retention_days" =~ ^[0-9]+$ ]] || (( retention_days < 7 || retention_days > 180 )); then
  echo 'N8N_EXECUTION_AUDIT_RETENTION_DAYS must be an integer from 7 to 180' >&2
  exit 2
fi

prune_audits() {
  local candidate
  shopt -s nullglob
  for candidate in "$audit_root"/????????-??????-stale-n8n-executions; do
    [[ -d "$candidate" ]] || continue
    [[ $(find "$candidate" -prune -mtime "+$retention_days" -print) ]] || continue
    if [[ "$dry_run" == true ]]; then
      echo "would_prune_execution_audit=$candidate"
    else
      # Candidates are limited to the script's own strict UTC directory form.
      rm -rf -- "$candidate"
      echo "pruned_execution_audit=$candidate"
    fi
  done
  shopt -u nullglob
}

prune_audits
if [[ "$prune_only" == true || "$dry_run" == true ]]; then
  exit 0
fi

query="SELECT id,\"workflowId\",\"startedAt\" FROM execution_entity
       WHERE status='running' AND \"startedAt\" < now() - interval '${seconds} seconds'
       ORDER BY \"startedAt\""
candidate_count="$($DOCKER compose exec -T postgres psql -v ON_ERROR_STOP=1 -U n8n -d n8n -Atqc "
  SELECT count(*) FROM execution_entity
   WHERE status='running' AND \"startedAt\" < now() - interval '${seconds} seconds'
")"
if [[ "$candidate_count" == "0" ]]; then
  echo "no stale executions"
  exit 0
fi

stamp="$(date -u +%Y%m%d-%H%M%S)"
backup_dir="${audit_root}/${stamp}-stale-n8n-executions"
mkdir -p "$backup_dir"
chmod 700 "$backup_dir"
"$DOCKER" compose exec -T postgres psql -v ON_ERROR_STOP=1 -U n8n -d n8n -At -F $'\t' -c "$query" \
  > "$backup_dir/before.tsv"
chmod 600 "$backup_dir/before.tsv"
count="$(wc -l < "$backup_dir/before.tsv" | tr -d ' ')"
if (( count == 0 )); then
  echo "no stale executions after candidate check; audit: $backup_dir/before.tsv"
  exit 0
fi
"$DOCKER" compose exec -T postgres psql -v ON_ERROR_STOP=1 -U n8n -d n8n -Atqc "
  UPDATE execution_entity
     SET status='crashed', \"stoppedAt\"=now()
   WHERE status='running' AND \"startedAt\" < now() - interval '${seconds} seconds'
  RETURNING id
" > "$backup_dir/updated_ids.tsv"
chmod 600 "$backup_dir/updated_ids.tsv"
updated="$(wc -l < "$backup_dir/updated_ids.tsv" | tr -d ' ')"
[[ "$updated" == "$count" ]] || { echo "reconciliation count mismatch: expected $count, updated $updated" >&2; exit 1; }
echo "marked $updated stale executions crashed; audit: $backup_dir/before.tsv"
