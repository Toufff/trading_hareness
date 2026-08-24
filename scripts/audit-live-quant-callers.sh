#!/usr/bin/env bash
# Read-only audit for the actually published n8n versions and running caller
# containers. It never writes PostgreSQL, imports a workflow, or restarts a
# service. Use --require-gateway only after a controlled hot/rolling publish.
set -euo pipefail

repo_root="$(cd "$(dirname "$0")/.." && pwd)"
compose=(docker compose -f "$repo_root/compose.yaml")
require_gateway=false

case "${1:-}" in
  "") ;;
  --require-gateway) require_gateway=true ;;
  -h|--help)
    printf 'Usage: scripts/audit-live-quant-callers.sh [--require-gateway]\n'
    exit 0
    ;;
  *) printf 'unknown option: %s\n' "$1" >&2; exit 2 ;;
esac

for service in postgres n8n feishu-adapter; do
  "${compose[@]}" ps --status running --services | grep -Fxq "$service" || {
    printf 'FAIL: required service is not running: %s\n' "$service" >&2
    exit 1
  }
done

workflow_rows="$("${compose[@]}" exec -T postgres psql -U n8n -d n8n -At -F '|' -c "
  SELECT w.id,
         CASE
           WHEN h.nodes::text LIKE '%quant-research-gateway%' THEN 'gateway_literal'
           WHEN h.nodes::text LIKE '%QUANT_SERVICE_URL%' THEN 'gateway_env'
           WHEN h.nodes::text LIKE '%quant-research:8000%' THEN 'direct'
           ELSE 'other'
         END
    FROM public.workflow_entity w
    LEFT JOIN public.workflow_history h
      ON h.\"workflowId\" = w.id AND h.\"versionId\" = w.\"activeVersionId\"
   WHERE w.active
     AND (h.nodes::text LIKE '%quant-research%' OR h.nodes::text LIKE '%QUANT_SERVICE_URL%')
   ORDER BY w.id;")"

violations=0
printf 'published n8n caller routes:\n'
if [[ -z "$workflow_rows" ]]; then
  printf '  (no active quant callers found)\n'
else
  while IFS='|' read -r workflow_id route; do
    [[ -n "$workflow_id" ]] || continue
    printf '  %s -> %s\n' "$workflow_id" "$route"
    if [[ "$require_gateway" == true && "$route" != gateway_literal && "$route" != gateway_env ]]; then
      violations=$((violations + 1))
    fi
  done <<<"$workflow_rows"
fi

n8n_url="$("${compose[@]}" exec -T n8n sh -lc 'printf %s "${QUANT_SERVICE_URL:-}"')"
adapter_url="$("${compose[@]}" exec -T feishu-adapter sh -lc 'printf %s "${QUANT_SERVICE_URL:-}"')"
printf 'running n8n QUANT_SERVICE_URL: %s\n' "${n8n_url:-<unset>}"
printf 'running adapter QUANT_SERVICE_URL: %s\n' "${adapter_url:-<unset>}"

if [[ "$require_gateway" == true ]]; then
  [[ "$n8n_url" == 'http://quant-research-gateway:8000' ]] || violations=$((violations + 1))
  [[ "$adapter_url" == 'http://quant-research-gateway:8000' ]] || violations=$((violations + 1))
fi

if ((violations)); then
  printf 'FAIL: %s live caller(s) are not yet confirmed on the gateway\n' "$violations" >&2
  exit 1
fi

printf 'PASS: live caller audit completed%s\n' "$([[ "$require_gateway" == true ]] && printf ' (gateway required)')"
