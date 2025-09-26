#!/usr/bin/env bash
set -e

# Upgrade pip and install dependencies
pip install --upgrade pip
pip install -r requirements.txt

# Ensure database exists
python -c "from app import init_db; init_db()"

# Start the app with Gunicorn, using Railway's PORT
exec gunicorn -w 4 -b 0.0.0.0:${PORT:-5000} app:app
