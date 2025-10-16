# Dockerfile.worker (robust)
FROM python:3.11-slim

# Install ffmpeg + build deps (if any wheels need compiling)
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python deps early (faster rebuilds). Ensure requirements.txt exists.
COPY requirements.txt /app/requirements.txt
RUN python -m pip install --upgrade pip \
    && pip install --no-cache-dir -r /app/requirements.txt

# Copy project files after deps are installed
COPY . /app

# Make scripts executable if present (safe no-op if missing)
RUN chmod +x /app/start.sh || true

ENV PYTHONUNBUFFERED=1

# Default: run rq worker. Use shell form so $REDIS_URL expands at runtime.
# NOTE: use -u (or --url) with the REDIS_URL env var (do NOT pass --tls or redis-cli wrappers).
CMD ["sh", "-c", "rq worker -u \"$REDIS_URL\" transcribe --verbose"]
