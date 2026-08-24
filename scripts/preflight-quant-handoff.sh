#!/usr/bin/env bash
# Start and validate the release-candidate service without changing live
# routing or creating background leases.  The candidate is stopped on exit
# unless --keep is supplied for a human inspection window.
set -euo pipefail

repo_root="$(cd "$(dirname "$0")/.." && pwd)"
compose=(docker compose --profile handoff -f "$repo_root/compose.yaml" -f "$repo_root/deploy/compose.quant-handoff.yaml")
candidate_service="quant-research-handoff"
keep=false

case "${1:-}" in
  "") ;;
  --keep) keep=true ;;
  -h|--help)
    printf 'Usage: scripts/preflight-quant-handoff.sh [--keep]\n'
    exit 0
    ;;
  *)
    printf 'unknown option: %s\n' "$1" >&2
    exit 2
    ;;
esac

cleanup() {
  if [[ "$keep" == false ]]; then
    "${compose[@]}" rm -f -s "$candidate_service" >/dev/null 2>&1 || true
  fi
}
trap cleanup EXIT

# Do not print the expanded Compose document: it contains environment-derived
# credentials.  Only assert the release-candidate isolation properties.
"${compose[@]}" config --format json | jq -e '
  .services["quant-research-handoff"] as $service
  | ($service != null)
    and (($service.ports | length) == 0)
    and ($service.environment.QUANT_BACKGROUND_TASKS_ENABLED == "false")
    and ($service.environment | has("PGPASSWORD"))
    and ($service.environment | has("QUANT_WRITE_API_KEY"))
' >/dev/null

"${compose[@]}" up -d --no-deps "$candidate_service" >/dev/null
for _ in $(seq 1 30); do
  status="$(docker inspect n8n-quant-research-handoff --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}')"
  [[ "$status" == healthy ]] && break
  sleep 1
done
if [[ "${status:-}" != healthy ]]; then
  docker logs n8n-quant-research-handoff --tail 80 >&2
  exit 1
fi

"${compose[@]}" exec -T "$candidate_service" python -c "from urllib.request import urlopen; import json; health=json.load(urlopen('http://127.0.0.1:8000/health', timeout=3)); payload=json.load(urlopen('http://127.0.0.1:8000/api/v1/research/ten-day-leader-rotation/latest?limit=90', timeout=3)); assert health['status'] == 'ok'; assert health['optional_background_tasks']['background_tasks_enabled'] is False; assert health['runtime_loops'] == {}; assert payload['scope'] == 'research_only_no_orders'; batch=payload['intraday']['latest_batch']; assert batch and batch['decision_eligible_count'] == 0 and batch['quote_sources']; print('handoff candidate verified: observed=%s sources=%s' % (batch['observed_count'], ','.join(batch['quote_sources'])))"

if [[ "$keep" == true ]]; then
  printf 'PASS: candidate remains running for inspection; stop it with: docker compose --profile handoff -f compose.yaml -f deploy/compose.quant-handoff.yaml stop -t 10 %s\n' "$candidate_service"
else
  printf 'PASS: candidate preflight complete; candidate will be stopped and removed now\n'
fi
