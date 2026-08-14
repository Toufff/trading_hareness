#!/usr/bin/env bash
# Mark executions stranded by an n8n runner/broker restart as crashed without
# deleting their audit data.  It never touches recent or completed executions.
set -euo pipefail

DOCKER="${DOCKER:-docker}"
seconds="600"
if [[ "${1:-}" == "--older-than-seconds" ]]; then
  seconds="${2:-}"
fi
if ! [[ "$seconds" =~ ^[0-9]+$ ]] || (( seconds < 60 || seconds > 86400 )); then
  echo 'usage: reconcile-stale-n8n-executions.sh [--older-than-seconds 60..86400]' >&2
  exit 2
fi

stamp="$(date -u +%Y%m%d-%H%M%S)"
backup_dir="backups/workflow-changes/${stamp}-stale-n8n-executions"
mkdir -p "$backup_dir"
chmod 700 "$backup_dir"
query="SELECT id,\"workflowId\",\"startedAt\" FROM execution_entity
       WHERE status='running' AND \"startedAt\" < now() - interval '${seconds} seconds'
       ORDER BY \"startedAt\""
"$DOCKER" compose exec -T postgres psql -v ON_ERROR_STOP=1 -U n8n -d n8n -At -F $'\t' -c "$query" \
  > "$backup_dir/before.tsv"
chmod 600 "$backup_dir/before.tsv"
count="$(wc -l < "$backup_dir/before.tsv" | tr -d ' ')"
if (( count == 0 )); then
  echo "no stale executions; audit: $backup_dir/before.tsv"
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
