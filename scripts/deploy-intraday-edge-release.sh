#!/usr/bin/env bash
# Deploy one committed source revision into a retained edge release directory.
# Default mode is read-only planning. --apply performs the fenced service restart.
set -euo pipefail

usage() {
  echo "usage: $0 <git-sha-or-tag> <release-label> [--apply]" >&2
  exit 2
}

[[ $# -ge 2 && $# -le 3 ]] || usage
release_ref="$1"
release_label="$2"
apply=false
[[ "${3:-}" == "--apply" ]] && apply=true
[[ "${3:-}" == "" || "${3:-}" == "--apply" ]] || usage
[[ "$release_label" =~ ^[A-Za-z0-9._-]+$ ]] || { echo "release label contains unsupported characters" >&2; exit 2; }

source_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
edge_host="${QUANT_EDGE_HOST:-root@47.114.113.152}"
edge_ssh_control_path="${QUANT_EDGE_SSH_CONTROL_PATH:-}"
edge_root="${QUANT_EDGE_ROOT:-/opt/quant-intraday-edge}"
release_sha="$(git -C "$source_root" rev-parse --verify "${release_ref}^{commit}")"
release_dir="$edge_root/releases/$release_sha"
built_at="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
ssh_command=(ssh)
[[ -z "$edge_ssh_control_path" ]] || ssh_command+=(-S "$edge_ssh_control_path" -o ControlMaster=no -o BatchMode=yes)

git -C "$source_root" diff --quiet --ignore-submodules -- || {
  echo "refusing to deploy an uncommitted worktree; commit the release first" >&2
  exit 1
}
git -C "$source_root" diff --cached --quiet --ignore-submodules -- || {
  echo "refusing to deploy staged but uncommitted changes" >&2
  exit 1
}

for command in git ssh tar; do command -v "$command" >/dev/null || { echo "missing required command: $command" >&2; exit 127; }; done

printf 'release_sha=%s\nrelease_label=%s\nedge_host=%s\nrelease_dir=%s\n' "$release_sha" "$release_label" "$edge_host" "$release_dir"
if [[ "$apply" != true ]]; then
  echo "dry run only; append --apply after the committed revision is reviewed and pushed"
  exit 0
fi

# Archive only the executable edge source and deployment templates. Existing
# releases are retained for rollback; the script never prunes server history.
git -C "$source_root" archive --format=tar "$release_sha" quant-service deploy/intraday-edge \
  | "${ssh_command[@]}" "$edge_host" "set -euo pipefail; install -d -m 0750 -o root -g quant_edge '$release_dir'; tar -xf - -C '$release_dir'; chown -R root:quant_edge '$release_dir'; chmod -R g+rX '$release_dir'; test -f '$release_dir/quant-service/entrypoint.py'; test -f '$release_dir/deploy/intraday-edge/quant-intraday-edge.service'"

"${ssh_command[@]}" "$edge_host" "set -euo pipefail
  edge_root='$edge_root'
  release_dir='$release_dir'
  release_sha='$release_sha'
  release_label='$release_label'
  built_at='$built_at'
  cd \"\$release_dir/quant-service\"
  \"\$edge_root/.venv/bin/python\" -m pip install --no-index --find-links \"\$edge_root/wheels\" -r requirements.txt >/dev/null
  PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=\"\$release_dir/quant-service\" \"\$edge_root/.venv/bin/python\" -c 'from app.release_metadata import release_metadata; assert release_metadata()[\"git_sha\"] is None'
  install -m 0640 /dev/stdin /etc/quant-intraday-edge.release.env <<EOF
APP_GIT_SHA=\$release_sha
APP_RELEASE=\$release_label
APP_BUILD_CREATED_AT=\$built_at
PYTHONPATH=\$edge_root/current/quant-service
EOF
  install -m 0644 \"\$release_dir/deploy/intraday-edge/quant-intraday-edge.service\" /etc/systemd/system/quant-intraday-edge.service
  install -m 0644 \"\$release_dir/deploy/intraday-edge/quant-intraday-edge-daily.service\" /etc/systemd/system/quant-intraday-edge-daily.service
  install -m 0644 \"\$release_dir/deploy/intraday-edge/quant-intraday-edge-materialize.service\" /etc/systemd/system/quant-intraday-edge-materialize.service
  install -m 0755 \"\$release_dir/deploy/intraday-edge/edge_export.sh\" /usr/local/sbin/quant-edge-export
  ln -sfn \"\$release_dir\" \"\$edge_root/current\"
  systemctl daemon-reload
  systemctl restart quant-intraday-edge.service
  for attempt in {1..30}; do curl -fsS http://127.0.0.1:18110/health >/tmp/quant-edge-release-health.json && break; sleep 2; done
  test -s /tmp/quant-edge-release-health.json
  \"\$edge_root/.venv/bin/python\" - <<'PY'
import json
payload = json.load(open('/tmp/quant-edge-release-health.json'))
assert payload['status'] == 'ok'
assert payload['build']['git_sha'] == '$release_sha'
assert payload['build']['release'] == '$release_label'
print('edge release health verified')
PY"

printf 'edge release applied: %s (%s)\n' "$release_label" "$release_sha"
