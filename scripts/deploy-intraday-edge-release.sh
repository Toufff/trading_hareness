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
edge_key="${QUANT_EDGE_SSH_KEY:-/Users/papa/.ssh/feishu_relay_edge_ed25519}"
edge_ssh_control_path="${QUANT_EDGE_SSH_CONTROL_PATH:-}"
edge_root="${QUANT_EDGE_ROOT:-/opt/quant-intraday-edge}"
github_repository="${QUANT_EDGE_GITHUB_REPOSITORY:-woshipapa/trading_hareness}"
github_branch="${QUANT_EDGE_GITHUB_BRANCH:-main}"
release_sha="$(git -C "$source_root" rev-parse --verify "${release_ref}^{commit}")"
release_dir="$edge_root/releases/$release_sha"
built_at="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
release_paths=(quant-service deploy/intraday-edge scripts/audit-fuyao-capabilities.py scripts/deploy-intraday-edge-release.sh)
[[ -r "$edge_key" ]] || { echo "edge SSH key is not readable: $edge_key" >&2; exit 2; }
[[ "$github_repository" =~ ^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$ ]] || { echo "invalid GitHub repository" >&2; exit 2; }
[[ "$github_branch" =~ ^[A-Za-z0-9._/-]+$ ]] || { echo "invalid GitHub branch" >&2; exit 2; }
ssh_command=(ssh -i "$edge_key" -o BatchMode=yes -o IdentitiesOnly=yes -o StrictHostKeyChecking=yes)
[[ -z "$edge_ssh_control_path" ]] || ssh_command+=(-S "$edge_ssh_control_path" -o ControlMaster=no -o BatchMode=yes)

git -C "$source_root" diff --quiet --ignore-submodules -- "${release_paths[@]}" || {
	echo "refusing to deploy edge source with uncommitted changes; commit the edge release first" >&2
	exit 1
}
git -C "$source_root" diff --cached --quiet --ignore-submodules -- "${release_paths[@]}" || {
	echo "refusing to deploy staged but uncommitted edge source" >&2
	exit 1
}

# GitHub is the release source of truth.  Refuse a local-only commit even if it
# exists in the workstation object database.
github_sha="$(git -C "$source_root" ls-remote --exit-code origin "refs/heads/$github_branch" | awk 'NR == 1 {print $1}')"
[[ "$github_sha" == "$release_sha" ]] || {
  echo "refusing edge deploy: origin/$github_branch is $github_sha, requested release is $release_sha" >&2
  exit 1
}
github_archive_url="https://codeload.github.com/$github_repository/tar.gz/$release_sha"

for command in git ssh tar; do command -v "$command" >/dev/null || { echo "missing required command: $command" >&2; exit 127; }; done

printf 'release_sha=%s\nrelease_label=%s\ngithub_branch=%s\ngithub_archive=%s\nedge_host=%s\nrelease_dir=%s\n' \
  "$release_sha" "$release_label" "$github_branch" "$github_archive_url" "$edge_host" "$release_dir"
if [[ "$apply" != true ]]; then
  echo "dry run only; append --apply after the committed revision is reviewed and pushed"
  exit 0
fi

# Download the exact GitHub commit on the server, then retain only the edge
# executable subset. Existing releases stay available for rollback.
"${ssh_command[@]}" "$edge_host" "set -euo pipefail
  release_dir='$release_dir'
  archive_url='$github_archive_url'
  temp_dir=\"\$(mktemp -d /tmp/quant-edge-github.XXXXXX)\"
  case \"\$temp_dir\" in /tmp/quant-edge-github.*) ;; *) exit 1 ;; esac
  trap 'rm -rf -- \"\$temp_dir\"' EXIT
  curl --fail --location --silent --show-error --retry 3 \"\$archive_url\" \
    | tar -xz -C \"\$temp_dir\" --strip-components=1
  test -f \"\$temp_dir/quant-service/entrypoint.py\"
  test -f \"\$temp_dir/deploy/intraday-edge/quant-intraday-edge.service\"
  test -f \"\$temp_dir/scripts/audit-fuyao-capabilities.py\"
  install -d -m 0750 -o root -g quant_edge \"\$release_dir\"
  cp -a \"\$temp_dir/quant-service\" \"\$release_dir/quant-service\"
  install -d \"\$release_dir/deploy\" \"\$release_dir/scripts\"
  cp -a \"\$temp_dir/deploy/intraday-edge\" \"\$release_dir/deploy/intraday-edge\"
  cp -a \"\$temp_dir/scripts/audit-fuyao-capabilities.py\" \"\$release_dir/scripts/audit-fuyao-capabilities.py\"
  chown -R root:quant_edge \"\$release_dir\"
  chmod -R g+rX \"\$release_dir\""

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
  install -m 0644 \"\$release_dir/deploy/intraday-edge/quant-intraday-edge-live-acceptance.service\" /etc/systemd/system/quant-intraday-edge-live-acceptance.service
  install -m 0644 \"\$release_dir/deploy/intraday-edge/quant-intraday-edge-live-acceptance.timer\" /etc/systemd/system/quant-intraday-edge-live-acceptance.timer
  install -m 0755 \"\$release_dir/deploy/intraday-edge/edge_export.sh\" /usr/local/sbin/quant-edge-export
  # The restricted export account may read only the journal and explicit
  # evidence tables. Re-apply idempotent grants with every edge release.
  # The release directory is intentionally not traversable by postgres. Root
  # opens the versioned SQL and streams it to psql so the database role never
  # gains filesystem access to release contents.
  sudo -u postgres psql -v ON_ERROR_STOP=1 -d quant_intraday_edge < \"\$release_dir/deploy/intraday-edge/edge_export_grants.sql\" >/dev/null
  ln -sfn \"\$release_dir\" \"\$edge_root/current\"
  systemctl daemon-reload
  systemctl enable --now quant-intraday-edge-live-acceptance.timer
  systemctl restart quant-intraday-edge.service
  # Alembic startup on the 2-core edge can exceed one minute when PostgreSQL is
  # under ingestion load.  Keep the wait bounded but do not report a false
  # rollback condition while systemd is still applying versioned migrations.
  for attempt in {1..90}; do curl -fsS http://127.0.0.1:18110/health >/tmp/quant-edge-release-health.json && break; sleep 2; done
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
