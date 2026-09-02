#!/bin/zsh

# launchd starts this at login. Wait for the separately managed Colima daemon,
# then reconcile the n8n Compose project. No secret values are written to logs.
set -euo pipefail

export PATH=/opt/homebrew/bin:/opt/homebrew/sbin:/usr/bin:/bin:/usr/sbin:/sbin

for attempt in {1..60}; do
	if /opt/homebrew/bin/docker info >/dev/null 2>&1; then
		cd /Users/papa/codebase/n8n
		exec /opt/homebrew/bin/docker compose up -d
	fi
	sleep 2
done

echo "Timed out waiting for Colima's Docker daemon" >&2
exit 1
