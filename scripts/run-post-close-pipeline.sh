#!/bin/bash
# Post-close daily pipeline runner.
#
# launchd fires this on a plain interval and the script decides whether the
# moment is right, because this host runs on US Pacific time while the market
# it serves runs on Asia/Shanghai: a fixed local-time schedule would drift by
# an hour against the close twice a year. Every decision below is made in
# exchange time.
#
# Running twice is harmless - the pipeline is idempotent - but the success
# marker keeps a 30-minute cadence from re-syncing a date that is already done.
set -euo pipefail

repo_root="${QUANT_REPO_ROOT:-/Users/papa/codebase/n8n}"
compose="/opt/homebrew/bin/docker compose -f ${repo_root}/compose.yaml"
api="${QUANT_API_BASE:-http://127.0.0.1:5681}"
state_dir="${QUANT_POST_CLOSE_STATE_DIR:-${HOME}/Library/Application Support/quant-post-close}"
# Earliest the exchange publishes a usable end-of-day cross-section. Before
# this the daily bars exist but the limit pools do not, and a run would record
# a partial date as done.
open_hhmm="${QUANT_POST_CLOSE_AFTER:-1630}"
close_hhmm="${QUANT_POST_CLOSE_UNTIL:-2330}"

now_date="$(TZ=Asia/Shanghai date +%Y-%m-%d)"
now_hhmm="$(TZ=Asia/Shanghai date +%H%M)"
now_dow="$(TZ=Asia/Shanghai date +%u)"
marker="${state_dir}/${now_date}.done"

emit() { printf '{"at":"%s","trading_date":"%s","event":"%s","detail":%s}\n' \
  "$(TZ=Asia/Shanghai date +%Y-%m-%dT%H:%M:%S%z)" "$now_date" "$1" "${2:-null}"; }

mkdir -p "$state_dir"

[[ "$now_dow" -le 5 ]] || { emit skipped '"weekend in exchange time"'; exit 0; }
[[ "$now_hhmm" > "$open_hhmm" || "$now_hhmm" == "$open_hhmm" ]] || { emit skipped '"before the post-close window"'; exit 0; }
[[ "$now_hhmm" < "$close_hhmm" ]] || { emit skipped '"past the post-close window"'; exit 0; }
[[ -f "$marker" ]] && { emit skipped '"already completed for this date"'; exit 0; }

write_key="$($compose exec -T quant-research sh -c 'printf "%s" "$QUANT_WRITE_API_KEY"' 2>/dev/null || true)"
[[ -n "$write_key" ]] || { emit failed '"quant-research is unreachable or has no write key"'; exit 1; }

emit started null
started=$SECONDS
response="$(/usr/bin/curl -sS -X POST "${api}/api/v1/pipeline/daily" \
  -H 'Content-Type: application/json' -H "X-Quant-Write-Key: ${write_key}" \
  -d "{\"as_of_date\":\"${now_date}\"}" --max-time 1800 2>&1 || true)"
elapsed=$(( SECONDS - started ))

status="$(printf '%s' "$response" | /usr/bin/sed -nE 's/.*"status":"([a-z_]+)".*/\1/p' | head -n 1)"
if [[ "$status" == "completed" ]]; then
  : > "$marker"
  emit completed "$(printf '{"seconds":%d,"body":%s}' "$elapsed" "$(printf '%s' "$response" | head -c 1500 | /usr/bin/python3 -c 'import json,sys;print(json.dumps(sys.stdin.read()))')")"
  exit 0
fi
# A blocked run is left unmarked on purpose so the next tick retries once the
# exchange has published the rest of the date.
emit "${status:-failed}" "$(printf '{"seconds":%d,"body":%s}' "$elapsed" "$(printf '%s' "$response" | head -c 1500 | /usr/bin/python3 -c 'import json,sys;print(json.dumps(sys.stdin.read()))')")"
exit 1
