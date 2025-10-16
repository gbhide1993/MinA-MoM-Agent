#!/usr/bin/env bash
set -euo pipefail

# default to 8000 for local testing if PORT not set by the host
PORT="${PORT:-8000}"
WORKERS="${WEB_CONCURRENCY:-2}"
TIMEOUT="${GUNICORN_TIMEOUT:-120}"

# (Optional) Upgrade pip & install dependencies — ideally done in Dockerfile instead
pip install --upgrade pip
pip install -r requirements.txt

# Ensure database exists (no-op if init_db handles that safely)
# swallow errors if init_db is not present/needed
python -c "from app import init_db; init_db()" || true

# Determine whether app exposes factory create_app() or simple app object
if python - <<'PY' 2>/dev/null
import app, sys
sys.exit(0 if hasattr(app, 'create_app') else 1)
PY
then
    TARGET="app:create_app()"
else
    TARGET="app:app"
fi

echo "Starting gunicorn with target=${TARGET} on port ${PORT} (workers=${WORKERS})"

# Exec gunicorn via python -m to avoid path issues
exec python -m gunicorn "$TARGET" -w "$WORKERS" -b "0.0.0.0:${PORT}" --timeout "$TIMEOUT" --log-level info
