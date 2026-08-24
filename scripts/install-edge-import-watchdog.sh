#!/usr/bin/env bash
# Install the version-controlled liveness watchdog for the remote import API.
# The command is a dry run until --apply is supplied.
set -euo pipefail

usage() { echo "usage: $0 [--apply]" >&2; exit 2; }
[[ $# -le 1 ]] || usage
apply=false
[[ "${1:-}" == "--apply" ]] && apply=true
[[ "${1:-}" == "" || "${1:-}" == "--apply" ]] || usage

source_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
edge_host="${RELAY_EDGE_HOST:-root@47.114.113.152}"
edge_key="${RELAY_EDGE_SSH_KEY:-/Users/papa/.ssh/feishu_relay_edge_ed25519}"
edge_ssh_control_path="${RELAY_EDGE_SSH_CONTROL_PATH:-}"
release_sha="$(git -C "$source_root" rev-parse HEAD)"
[[ -r "$edge_key" ]] || { echo "edge SSH key is not readable: $edge_key" >&2; exit 2; }
ssh_command=(ssh -i "$edge_key" -o BatchMode=yes -o IdentitiesOnly=yes -o StrictHostKeyChecking=yes)
[[ -z "$edge_ssh_control_path" ]] || ssh_command+=(-S "$edge_ssh_control_path" -o ControlMaster=no -o BatchMode=yes)

for command in git ssh tar; do command -v "$command" >/dev/null || { echo "missing required command: $command" >&2; exit 127; }; done
git -C "$source_root" diff --quiet --ignore-submodules -- || { echo "refusing to install from an uncommitted worktree" >&2; exit 1; }
git -C "$source_root" diff --cached --quiet --ignore-submodules -- || { echo "refusing to install staged but uncommitted changes" >&2; exit 1; }

printf 'release_sha=%s\nedge_host=%s\n' "$release_sha" "$edge_host"
if [[ "$apply" != true ]]; then
	echo "dry run only; append --apply after the committed revision is reviewed and pushed"
	exit 0
fi

git -C "$source_root" archive --format=tar "$release_sha" deploy/feishu-relay-edge/stock-reports-import-watchdog.sh deploy/feishu-relay-edge/stock-reports-import-watchdog.service deploy/feishu-relay-edge/stock-reports-import-watchdog.timer \
	| "${ssh_command[@]}" "$edge_host" "set -euo pipefail; install -d -m 0755 /tmp/stock-reports-import-watchdog; tar -xf - -C /tmp/stock-reports-import-watchdog"

"${ssh_command[@]}" "$edge_host" bash -s <<'REMOTE'
set -euo pipefail
root='/tmp/stock-reports-import-watchdog/deploy/feishu-relay-edge'
install -D -m 0755 "$root/stock-reports-import-watchdog.sh" /usr/local/libexec/stock-reports-import-watchdog
install -D -m 0644 "$root/stock-reports-import-watchdog.service" /etc/systemd/system/stock-reports-import-watchdog.service
install -D -m 0644 "$root/stock-reports-import-watchdog.timer" /etc/systemd/system/stock-reports-import-watchdog.timer
systemctl daemon-reload
systemctl enable --now stock-reports-import-watchdog.timer
systemctl start stock-reports-import-watchdog.service
systemctl is-active --quiet stock-reports-import.service
systemctl is-active --quiet stock-reports-import-watchdog.timer
systemctl list-timers --all --no-pager stock-reports-import-watchdog.timer
REMOTE
