# Dockerfile.worker (robust)
FROM python:3.11-slim

# Install ffmpeg + build deps (if any wheels need compiling)
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copy project files
COPY . .

# Install dependencies: requirements.txt if present, and ensure rq + redis are installed
RUN pip install --no-cache-dir -r requirements.txt || true \
    && pip install --no-cache-dir rq redis

ENV REDIS_URL=redis://host.docker.internal:6379/0
ENV PYTHONUNBUFFERED=1

# Run RQ worker via python -m so it works even if console script not present
CMD ["rq", "worker", "transcribe", "--url", "redis://redis:6379/0", "--verbose"]

