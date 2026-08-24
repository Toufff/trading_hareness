#!/usr/bin/env bash
# Pull and activate one immutable Feishu adapter image while retaining the
# remote PostgreSQL ledger, OAuth refresh state, and media retry files.
set -euo pipefail

usage() {
  echo "usage: $0 <git-sha> <release-label> [--image <image-ref>] [--apply]" >&2
  exit 2
}

[[ $# -ge 2 ]] || usage
release_sha="$1"
release_label="$2"
shift 2
[[ "$release_sha" =~ ^[0-9a-fA-F]{7,64}$ ]] || { echo "git SHA must be hexadecimal" >&2; exit 2; }
[[ "$release_label" =~ ^[A-Za-z0-9._-]+$ ]] || { echo "release label contains unsupported characters" >&2; exit 2; }

# GitHub Actions publishes immutable images under the complete commit SHA.
# Accept a convenient abbreviated SHA at the CLI, then resolve it locally
# before deriving the default GHCR tag or checking the health provenance.
repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
release_sha="$(git -C "$repo_root" rev-parse --verify "${release_sha}^{commit}")"

image_ref="${FEISHU_ADAPTER_IMAGE:-ghcr.io/woshipapa/trading-hareness-feishu-adapter:${release_sha}}"
apply=false
while [[ $# -gt 0 ]]; do
  case "$1" in
    --image) image_ref="${2:-}"; shift 2 ;;
    --apply) apply=true; shift ;;
    *) usage ;;
  esac
done
[[ -n "$image_ref" ]] || { echo "image reference must not be empty" >&2; exit 2; }

edge_host="${RELAY_EDGE_HOST:-root@47.114.113.152}"
edge_dir="${RELAY_EDGE_DIR:-/opt/feishu-relay-edge}"
runtime_env="${RELAY_EDGE_RUNTIME_ENV:-/etc/feishu-relay-edge/runtime.env}"
secrets_env="${RELAY_EDGE_SECRETS_ENV:-/etc/feishu-relay-edge/secrets.env}"
edge_key="${RELAY_EDGE_SSH_KEY:-/Users/papa/.ssh/feishu_relay_edge_ed25519}"
ssh_control_path="${RELAY_EDGE_SSH_CONTROL_PATH:-}"
[[ -r "$edge_key" ]] || { echo "edge SSH key is not readable: $edge_key" >&2; exit 2; }
ssh_command=(ssh -i "$edge_key" -o BatchMode=yes -o IdentitiesOnly=yes -o StrictHostKeyChecking=yes)
[[ -z "$ssh_control_path" ]] || ssh_command+=(-S "$ssh_control_path" -o ControlMaster=no -o BatchMode=yes)
built_at="$(date -u +%Y-%m-%dT%H:%M:%SZ)"

printf 'release_sha=%s\nrelease_label=%s\nimage=%s\nedge_host=%s\n' "$release_sha" "$release_label" "$image_ref" "$edge_host"
if [[ "$apply" != true ]]; then
  echo "dry run only; append --apply after the GHCR image is published"
  exit 0
fi

"${ssh_command[@]}" "$edge_host" bash -s -- \
  "$edge_dir" "$runtime_env" "$secrets_env" "$image_ref" "$release_sha" "$release_label" "$built_at" <<'REMOTE'
set -euo pipefail
edge_dir="$1"
runtime_env="$2"
secrets_env="$3"
image_ref="$4"
release_sha="$5"
release_label="$6"
built_at="$7"

test -f "$runtime_env"
test -f "$secrets_env"
docker pull "$image_ref"
update_env() {
  key="$1"
  value="$2"
  temp="$(mktemp "${runtime_env}.XXXXXX")"
  awk -v key="$key" -v value="$value" '
    BEGIN { found=0 }
    index($0, key "=") == 1 { print key "=" value; found=1; next }
    { print }
    END { if (!found) print key "=" value }
  ' "$runtime_env" > "$temp"
  install -m 0640 "$temp" "$runtime_env"
  rm -f "$temp"
}
update_env FEISHU_ADAPTER_IMAGE "$image_ref"
update_env APP_GIT_SHA "$release_sha"
update_env APP_RELEASE "$release_label"
update_env APP_BUILD_CREATED_AT "$built_at"
cd "$edge_dir"
docker compose --env-file "$runtime_env" --env-file "$secrets_env" up -d --no-deps --no-build feishu-adapter
for attempt in {1..30}; do curl -fsS http://127.0.0.1:18300/health >/tmp/feishu-relay-release-health.json && break; sleep 2; done
test -s /tmp/feishu-relay-release-health.json
grep -Fq '"status":"ok"' /tmp/feishu-relay-release-health.json
grep -Fq "\"git_sha\":\"${release_sha}\"" /tmp/feishu-relay-release-health.json
grep -Fq "\"release\":\"${release_label}\"" /tmp/feishu-relay-release-health.json
echo 'relay adapter release health verified'
docker inspect -f '{{.State.Health.Status}} {{.RestartCount}}' feishu-relay-edge-adapter
REMOTE

printf 'relay adapter release applied: %s (%s)\n' "$release_label" "$release_sha"
