#!/usr/bin/env bash
# Read-only market-session acceptance check for the edge collector.  It is
# intentionally stricter than /health: when the SSE continuous session is
# active every currently-required data loop must be fresh and error-free.
set -euo pipefail

edge_host="${INTRADAY_EDGE_HOST:-root@47.114.113.152}"
edge_key="${INTRADAY_EDGE_SSH_KEY:-/Users/papa/.ssh/feishu_relay_edge_ed25519}"
edge_status_url="${INTRADAY_EDGE_STATUS_URL:-http://127.0.0.1:18110/api/v1/intraday/services/status}"
edge_health_url="${INTRADAY_EDGE_HEALTH_URL:-http://127.0.0.1:18110/health}"
allow_standby=false

if [[ "${1:-}" == "--allow-standby" ]]; then
  allow_standby=true
elif [[ $# -ne 0 ]]; then
  echo "usage: $0 [--allow-standby]" >&2
  exit 2
fi

for command in ssh jq; do
  command -v "$command" >/dev/null || { echo "missing required command: $command" >&2; exit 127; }
done
[[ -r "$edge_key" ]] || { echo "edge SSH key is not readable: $edge_key" >&2; exit 2; }

ssh_command=(ssh -i "$edge_key" -o BatchMode=yes -o IdentitiesOnly=yes -o StrictHostKeyChecking=yes)
payload="$("${ssh_command[@]}" "$edge_host" bash -s -- "$edge_health_url" "$edge_status_url" <<'REMOTE'
set -euo pipefail
health_url="$1"
status_url="$2"
health="$(curl -fsS "$health_url")"
status="$(curl -fsS "$status_url")"
jq -cn --argjson health "$health" --argjson status "$status" '{health:$health,status:$status}'
REMOTE
)"

printf '%s' "$payload" | jq -e '.health.status == "ok" and .health.optional_background_tasks.runtime_profile == "intraday_edge"' >/dev/null || {
  printf '%s\n' "$payload" | jq -c '{acceptance:"failed", reason:"edge health or runtime profile is invalid", health:.health}' >&2
  exit 1
}

session_active="$(printf '%s' "$payload" | jq -r '.status.session_active == true')"
if [[ "$session_active" != true ]]; then
  printf '%s\n' "$payload" | jq -c '{acceptance:"standby", build:.health.build, session_active:.status.session_active, session_reason:.status.session_reason, items:[.status.items[] | {key,state,expected_active,last_observed_at,age_seconds,max_age_seconds,last_error}]}'
  if [[ "$allow_standby" == true ]]; then
    exit 0
  fi
  echo "edge is healthy but the SSE continuous session is inactive; rerun during 09:30-11:30 or 13:00-15:00 Asia/Shanghai" >&2
  exit 3
fi

# `feishu_alert` is a delivery capability, not a market-data poller; the
# remaining expected-active services are the actual session acceptance set.
if ! printf '%s' "$payload" | jq -e '
  .status.items as $items
  | [$items[] | select(.expected_active == true and .key != "feishu_alert")] as $required
  | ($required | length > 0)
  and all($required[];
      (.state == "healthy")
      and (.last_observed_at != null)
      and (.last_error == null)
      and (.age_seconds != null)
      and (.max_age_seconds != null)
      and (.age_seconds <= .max_age_seconds)
  )
' >/dev/null; then
  printf '%s\n' "$payload" | jq -c '{acceptance:"failed", build:.health.build, session_active:.status.session_active, session_reason:.status.session_reason, items:[.status.items[] | select(.expected_active == true and .key != "feishu_alert") | {key,state,last_observed_at,age_seconds,max_age_seconds,last_error}]}' >&2
  exit 1
fi

printf '%s\n' "$payload" | jq -c '{acceptance:"passed", build:.health.build, session_active:.status.session_active, items:[.status.items[] | select(.expected_active == true and .key != "feishu_alert") | {key,state,last_observed_at,age_seconds,max_age_seconds}]}'
