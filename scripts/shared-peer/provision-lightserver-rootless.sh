#!/usr/bin/env bash
set -euo pipefail

PEER_USER="${PEER_USER:-stockpeer}"
AUTHORIZED_KEY_FILE="${AUTHORIZED_KEY_FILE:-}"
# Restricts the peer key so it cannot get an interactive shell and can only
# forward (-L/-R) to the two loopback ports the peer runtime actually needs:
# the Postgres tunnel (15432) and the read-only licensed gateway (15681).
AUTHORIZED_KEY_OPTIONS="${AUTHORIZED_KEY_OPTIONS:-restrict,port-forwarding,permitopen=\"127.0.0.1:15432\",permitopen=\"127.0.0.1:15681\"}"
ROOTLESS_DOCKER_VERSION="${ROOTLESS_DOCKER_VERSION:-29.7.2}"
# SHA256 of docker-${ROOTLESS_DOCKER_VERSION}.tgz (linux/static/stable/x86_64).
# Obtain it from Docker's published checksums, e.g.:
#   curl -fsSL https://download.docker.com/linux/static/stable/x86_64/docker-${ROOTLESS_DOCKER_VERSION}.tgz.sha256
# or, if no per-file .sha256 is published for this version, download the
# archive once through a trusted path, verify it out-of-band, and pin the
# result of `sha256sum docker-${ROOTLESS_DOCKER_VERSION}.tgz` here. Required
# only when a fresh archive actually needs to be downloaded (see below); a
# pre-installed dockerd already pinned at ROOTLESS_DOCKER_VERSION skips the
# download+verify step entirely.
ROOTLESS_DOCKER_SHA256="${ROOTLESS_DOCKER_SHA256:-}"
# Registry mirrors are opt-in only: leave unset (the default) to use Docker's
# default registry directly. Set a comma-separated list to inject third-party
# mirrors into the peer's rootless daemon.json.
PEER_REGISTRY_MIRRORS="${PEER_REGISTRY_MIRRORS:-}"

if [[ "$(id -u)" -ne 0 ]]; then
  echo "run as root" >&2
  exit 1
fi

apt-get update
DEBIAN_FRONTEND=noninteractive apt-get install -y uidmap dbus-user-session slirp4netns docker-ce-rootless-extras

if ! id "${PEER_USER}" >/dev/null 2>&1; then
  useradd --create-home --shell /bin/bash "${PEER_USER}"
fi
peer_uid="$(id -u "${PEER_USER}")"
peer_home="$(getent passwd "${PEER_USER}" | cut -d: -f6)"

grep -q "^${PEER_USER}:" /etc/subuid || echo "${PEER_USER}:100000:65536" >> /etc/subuid
grep -q "^${PEER_USER}:" /etc/subgid || echo "${PEER_USER}:100000:65536" >> /etc/subgid

install -d -m 0700 -o "${PEER_USER}" -g "${PEER_USER}" "${peer_home}/.ssh"
touch "${peer_home}/.ssh/authorized_keys"
chown "${PEER_USER}:${PEER_USER}" "${peer_home}/.ssh/authorized_keys"
chmod 0600 "${peer_home}/.ssh/authorized_keys"
if [[ -n "${AUTHORIZED_KEY_FILE}" ]]; then
  test -r "${AUTHORIZED_KEY_FILE}"
  raw_key="$(tr -d '\r\n' < "${AUTHORIZED_KEY_FILE}")"
  if [[ -n "${AUTHORIZED_KEY_OPTIONS}" ]]; then
    key="${AUTHORIZED_KEY_OPTIONS} ${raw_key}"
  else
    key="${raw_key}"
  fi
  grep -qxF "${key}" "${peer_home}/.ssh/authorized_keys" || echo "${key}" >> "${peer_home}/.ssh/authorized_keys"
fi

loginctl enable-linger "${PEER_USER}"
systemctl start "user@${peer_uid}.service"

# Keep the collaborator daemon independent from the host's rootful Docker.
# The 29.2 daemon mis-detects AppArmor support in this rootless network
# namespace; a current official static daemon includes the upstream fix.  It
# is installed only under stockpeer's home and never replaces /usr/bin/docker.
peer_bin="${peer_home}/bin"
install -d -m 0755 -o "${PEER_USER}" -g "${PEER_USER}" "${peer_bin}"
if [[ ! -x "${peer_bin}/dockerd" ]] || ! "${peer_bin}/dockerd" --version | grep -q "${ROOTLESS_DOCKER_VERSION}"; then
  if [[ -z "${ROOTLESS_DOCKER_SHA256}" ]]; then
    echo "ROOTLESS_DOCKER_SHA256 is required to download docker-${ROOTLESS_DOCKER_VERSION}.tgz; see the comment above ROOTLESS_DOCKER_SHA256 for how to obtain it." >&2
    exit 1
  fi
  archive="$(mktemp)"
  extract="$(mktemp -d)"
  trap 'rm -f "${archive}"; rm -rf "${extract}"' EXIT
  curl -fsSL "https://download.docker.com/linux/static/stable/x86_64/docker-${ROOTLESS_DOCKER_VERSION}.tgz" \
    -o "${archive}"
  actual_sha256="$(sha256sum "${archive}" | cut -d' ' -f1)"
  if [[ "${actual_sha256}" != "${ROOTLESS_DOCKER_SHA256}" ]]; then
    echo "SHA256 mismatch for docker-${ROOTLESS_DOCKER_VERSION}.tgz: expected ${ROOTLESS_DOCKER_SHA256}, got ${actual_sha256}" >&2
    exit 1
  fi
  tar -xzf "${archive}" -C "${extract}"
  find "${extract}/docker" -maxdepth 1 -type f -exec install -m 0755 -o "${PEER_USER}" -g "${PEER_USER}" {} "${peer_bin}/" \;
fi

unit_dropin="${peer_home}/.config/systemd/user/docker.service.d"
install -d -m 0755 -o "${PEER_USER}" -g "${PEER_USER}" "${unit_dropin}"
peer_path="${peer_bin}:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
printf '[Service]\nEnvironment="PATH=%s"\n' "${peer_path}" > "${unit_dropin}/10-peer-static-path.conf"
chown "${PEER_USER}:${PEER_USER}" "${unit_dropin}/10-peer-static-path.conf"
if [[ -n "${PEER_REGISTRY_MIRRORS}" ]]; then
  daemon_config="${peer_home}/.config/docker/daemon.json"
  install -d -m 0755 -o "${PEER_USER}" -g "${PEER_USER}" "$(dirname "${daemon_config}")"
  runuser -u "${PEER_USER}" -- python3 - "${daemon_config}" "${PEER_REGISTRY_MIRRORS}" <<'PY'
import json
import sys
from pathlib import Path

path = Path(sys.argv[1])
mirrors = [item.strip() for item in sys.argv[2].split(",") if item.strip()]
payload = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
payload["registry-mirrors"] = mirrors
path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
PY
fi
runuser -u "${PEER_USER}" -- env \
  HOME="${peer_home}" \
  XDG_RUNTIME_DIR="/run/user/${peer_uid}" \
  DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/${peer_uid}/bus" \
  systemctl --user daemon-reload
runuser -u "${PEER_USER}" -- env \
  HOME="${peer_home}" XDG_RUNTIME_DIR="/run/user/${peer_uid}" \
  DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/${peer_uid}/bus" \
  systemctl --user reset-failed docker.service || true

runuser -u "${PEER_USER}" -- env \
  HOME="${peer_home}" \
  PATH="${peer_path}" \
  XDG_RUNTIME_DIR="/run/user/${peer_uid}" \
  DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/${peer_uid}/bus" \
  dockerd-rootless-setuptool.sh install --force

runuser -u "${PEER_USER}" -- env \
  HOME="${peer_home}" \
  PATH="${peer_path}" \
  XDG_RUNTIME_DIR="/run/user/${peer_uid}" \
  DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/${peer_uid}/bus" \
  systemctl --user enable docker
runuser -u "${PEER_USER}" -- env \
  HOME="${peer_home}" PATH="${peer_path}" XDG_RUNTIME_DIR="/run/user/${peer_uid}" \
  DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/${peer_uid}/bus" \
  systemctl --user restart docker

runuser -u "${PEER_USER}" -- env \
  HOME="${peer_home}" \
  PATH="${peer_path}" \
  XDG_RUNTIME_DIR="/run/user/${peer_uid}" \
  DOCKER_HOST="unix:///run/user/${peer_uid}/docker.sock" \
  docker info --format '{{json .SecurityOptions}}'

cat <<EOF
peer_user=${PEER_USER}
peer_uid=${peer_uid}
docker_host=unix:///run/user/${peer_uid}/docker.sock
rootless_environment=ready
EOF
