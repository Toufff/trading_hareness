#!/bin/sh
set -eu

# Opening an SSH listener says nothing about its target. Authenticate and query
# the actual database; suppress driver details so health logs never leak secrets.
if ! result="$(timeout 5 env PGHOST=127.0.0.1 PGPORT=5432 PGCONNECT_TIMEOUT=3 \
  PGOPTIONS='-c statement_timeout=2000' \
  psql -X -w -A -t -v ON_ERROR_STOP=1 -c 'SELECT 1' 2>/dev/null)"; then
  echo 'unhealthy: postgresql_query_failed'
  exit 1
fi
if [ "$result" != '1' ]; then
  echo 'unhealthy: postgresql_unexpected_result'
  exit 1
fi
if ! timeout 4 wget -q -T 3 -O /dev/null http://127.0.0.1:5681/health 2>/dev/null; then
  echo 'unhealthy: owner_api_failed'
  exit 1
fi
echo 'healthy: postgresql_query=ok owner_api=ok'
