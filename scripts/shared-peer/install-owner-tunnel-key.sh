#!/usr/bin/env bash
# Provision a dedicated, restricted ssh key on lightServer for the OWNER's
# unattended tunnel scripts (start-stock-dashboard.ps1, start-shared-tunnels.ps1,
# verify-shared-runtime.ps1), replacing the current pattern of those scripts
# using a root-capable `ssh <alias>` config entry for a Windows scheduled
# task that stays online at all times.
#
# This does NOT touch the collaborator/peer key (see
# provision-lightserver-rootless.sh); it provisions a second, separate key
# for the platform owner's own Windows automation.
#
# Usage (run as root on lightServer):
#   OWNER_TUNNEL_PUBLIC_KEY_FILE=/path/to/owner_tunnel_ed25519.pub \
#     bash scripts/shared-peer/install-owner-tunnel-key.sh
#
# After this script succeeds, set the following four keys in
# G:\StockPlatform\config\runtime.env on the Windows owner host so the
# tunnel/verification scripts stop falling back to the root-capable alias:
#   OWNER_TUNNEL_SSH_USER=<OWNER_TUNNEL_USER>
#   OWNER_TUNNEL_SSH_KEY=<path to the matching private key, e.g.
#       G:\StockPlatform\peer\secrets\owner_tunnel_ed25519>
#   OWNER_TUNNEL_SSH_HOST=<lightServer hostname or IP>
#   OWNER_TUNNEL_SSH_PORT=<lightServer sshd port>
set -euo pipefail

OWNER_TUNNEL_USER="${OWNER_TUNNEL_USER:-stockowner}"
OWNER_TUNNEL_PUBLIC_KEY_FILE="${OWNER_TUNNEL_PUBLIC_KEY_FILE:-}"
# The owner side listens for forwarded connections (-R) rather than
# forwarding out (-L) like the peer, so its authorized_keys entry grants
# permitlisten on the three reserved loopback ports instead of permitopen:
# the Postgres/API reverse tunnels (15432/15681) and the dashboard adapter
# reverse tunnel (15680).
AUTHORIZED_KEY_OPTIONS="${AUTHORIZED_KEY_OPTIONS:-restrict,port-forwarding,permitlisten=\"127.0.0.1:15432\",permitlisten=\"127.0.0.1:15680\",permitlisten=\"127.0.0.1:15681\"}"

if [[ "$(id -u)" -ne 0 ]]; then
  echo "run as root" >&2
  exit 1
fi

if [[ -z "${OWNER_TUNNEL_PUBLIC_KEY_FILE}" ]]; then
  echo "Set OWNER_TUNNEL_PUBLIC_KEY_FILE to the owner tunnel's public key file" >&2
  exit 1
fi
test -r "${OWNER_TUNNEL_PUBLIC_KEY_FILE}"

if ! id "${OWNER_TUNNEL_USER}" >/dev/null 2>&1; then
  useradd --create-home --shell /usr/sbin/nologin "${OWNER_TUNNEL_USER}"
fi
owner_home="$(getent passwd "${OWNER_TUNNEL_USER}" | cut -d: -f6)"

install -d -m 0700 -o "${OWNER_TUNNEL_USER}" -g "${OWNER_TUNNEL_USER}" "${owner_home}/.ssh"
touch "${owner_home}/.ssh/authorized_keys"
chown "${OWNER_TUNNEL_USER}:${OWNER_TUNNEL_USER}" "${owner_home}/.ssh/authorized_keys"
chmod 0600 "${owner_home}/.ssh/authorized_keys"

raw_key="$(tr -d '\r\n' < "${OWNER_TUNNEL_PUBLIC_KEY_FILE}")"
if [[ -n "${AUTHORIZED_KEY_OPTIONS}" ]]; then
  key="${AUTHORIZED_KEY_OPTIONS} ${raw_key}"
else
  key="${raw_key}"
fi
grep -qxF "${key}" "${owner_home}/.ssh/authorized_keys" || echo "${key}" >> "${owner_home}/.ssh/authorized_keys"

cat <<EOF
owner_tunnel_user=${OWNER_TUNNEL_USER}
authorized_keys=${owner_home}/.ssh/authorized_keys
next_step="set OWNER_TUNNEL_SSH_USER=${OWNER_TUNNEL_USER}, OWNER_TUNNEL_SSH_KEY, OWNER_TUNNEL_SSH_HOST, OWNER_TUNNEL_SSH_PORT in runtime.env"
warning="This account has a nologin shell. start-stock-dashboard.ps1 and verify-shared-runtime.ps1 run remote commands (curl/ss/fuser/probe) over the same SSH target, so setting those four variables today stops the reverse dashboard tunnel from starting. See docs/SHARED_PEER_RUNTIME.md before enabling them."
EOF
