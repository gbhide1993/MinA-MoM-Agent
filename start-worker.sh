#!/bin/sh
set -e

# Optional: default redis (useful for local dev)
: "${REDIS_URL:=redis://redis:6379/0}"

# simple wait-for-redis with timeout
timeout=60
interval=2
elapsed=0

echo "Waiting for Redis at ${REDIS_URL} ..."

while ! python - <<PY
import sys, os, time
from urllib.parse import urlparse
try:
    import redis
    u = urlparse(os.environ.get('REDIS_URL'))
    # redis-py accepts whole URL
    r = redis.from_url(os.environ.get('REDIS_URL'))
    r.ping()
    sys.exit(0)
except Exception:
    sys.exit(1)
PY
do
  elapsed=$((elapsed + interval))
  if [ "$elapsed" -ge "$timeout" ]; then
    echo "Redis not available after ${timeout}s - continuing anyway (worker may fail)."
    break
  fi
  sleep $interval
done

# Exec the RQ worker replacing shell so it receives signals.
exec rq worker -u "${REDIS_URL}" transcribe --verbose
