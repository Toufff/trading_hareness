#!/bin/sh
set -eu

: "${PEER_SSH_HOST:?PEER_SSH_HOST is required}"
: "${PEER_SSH_PORT:?PEER_SSH_PORT is required}"
: "${PEER_SSH_USER:?PEER_SSH_USER is required}"
: "${REMOTE_DB_PORT:?REMOTE_DB_PORT is required}"
: "${REMOTE_API_PORT:?REMOTE_API_PORT is required}"

test -r /run/secrets/peer_ssh_key
test -r /run/secrets/known_hosts
install -m 0600 /run/secrets/peer_ssh_key /tmp/peer_ssh_key

# Routing address and verified host identity may differ for a same-host peer.
# Keep the existing known_hosts identity when using the host's private address.
set --
if [ -n "${PEER_SSH_HOST_KEY_ALIAS:-}" ]; then
  set -- -o "HostKeyAlias=${PEER_SSH_HOST_KEY_ALIAS}"
fi

# Optional third forward: the owner's batch tunnel (a separate SSH connection
# on the owner side, reserved remote port 15433) so bulk/COPY jobs stop sharing
# the intraday connection's TCP window. Unset means "not deployed" and this
# container behaves exactly as before.
#
# A -L forward binds locally and does not require the far end to be listening,
# so ExitOnForwardFailure=yes does not make the sidecar depend on the owner's
# batch tunnel being up: if the owner side is down, connections to 5433 fail
# individually while 5432/5681 keep working. That is also why the healthcheck
# below is deliberately left alone - batch is optional, and gating container
# health on it would turn an optimization into an outage.
if [ -n "${PEER_BATCH_DB_PORT:-}" ]; then
  set -- "$@" -L \
    "${PEER_LOCAL_BIND_ADDRESS:-127.0.0.1}:${PEER_BATCH_DB_PORT}:127.0.0.1:${PEER_BATCH_REMOTE_PORT:-15433}"
fi

exec ssh -NT \
  "$@" \
  -o BatchMode=yes \
  -o ExitOnForwardFailure=yes \
  -o ConnectTimeout=5 \
  -o ServerAliveInterval=30 \
  -o ServerAliveCountMax=3 \
  -o StrictHostKeyChecking=yes \
  -o UserKnownHostsFile=/run/secrets/known_hosts \
  -i /tmp/peer_ssh_key \
  -p "${PEER_SSH_PORT}" \
  -L "${PEER_LOCAL_BIND_ADDRESS:-127.0.0.1}:5432:127.0.0.1:${REMOTE_DB_PORT}" \
  -L "${PEER_LOCAL_BIND_ADDRESS:-127.0.0.1}:5681:127.0.0.1:${REMOTE_API_PORT}" \
  "${PEER_SSH_USER}@${PEER_SSH_HOST}"
