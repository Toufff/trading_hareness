#!/usr/bin/env bash
# A GET is deliberately unsupported by the machine batch endpoint.  A fast
# 401/403/405 response proves that Gunicorn can accept and dispatch requests;
# a timeout or other response means the single-worker control plane is no
# longer safe for n8n media uploads and should be restarted.
set -euo pipefail

service_name="${STOCK_REPORTS_IMPORT_SERVICE:-stock-reports-import.service}"
probe_url="${STOCK_REPORTS_IMPORT_PROBE_URL:-http://127.0.0.1:18083/api/v1/imports/batches}"
probe_timeout_seconds="${STOCK_REPORTS_IMPORT_PROBE_TIMEOUT_SECONDS:-8}"

if ! systemctl is-active --quiet "$service_name"; then
	logger -t stock-reports-import-watchdog "${service_name} inactive; restarting"
	systemctl restart "$service_name"
	exit 0
fi

status_code="$(curl --silent --show-error --max-time "$probe_timeout_seconds" --output /dev/null --write-out '%{http_code}' "$probe_url" || true)"
case "$status_code" in
	401|403|405) exit 0 ;;
	*)
		logger -t stock-reports-import-watchdog "probe returned ${status_code:-no-response}; restarting ${service_name}"
		systemctl restart "$service_name"
		;;
esac
