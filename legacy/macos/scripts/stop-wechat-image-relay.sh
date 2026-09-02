#!/bin/zsh

set -euo pipefail

relay_root="/Users/papa/codebase/n8n"
relay_pidfile="${relay_root}/state/wechat-image-relay.pid"
relay_label="com.papa.wechat-image-relay"
relay_domain="gui/$(id -u)"

if launchctl print "${relay_domain}/${relay_label}" >/dev/null 2>&1; then
	launchctl bootout "${relay_domain}/${relay_label}"
	echo "unloaded ${relay_label}"
	exit 0
fi

if [[ ! -f "$relay_pidfile" ]]; then
	echo "wechat-image-relay is not running"
	exit 0
fi

relay_pid="$(cat "$relay_pidfile")"
if [[ "$relay_pid" =~ '^[0-9]+$' ]] && kill -0 "$relay_pid" >/dev/null 2>&1; then
	kill "$relay_pid"
	echo "stopped wechat-image-relay pid ${relay_pid}"
else
	echo "wechat-image-relay pid ${relay_pid} is not active"
fi
rm -f "$relay_pidfile"
