FROM python:3.11-slim

# System deps: git for repo ops, curl/bash for Claude CLI install, nodejs/npm for claude-code
RUN apt-get update && apt-get install -y --no-install-recommends \
    git curl bash nodejs npm \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Python deps
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Install Playwright browsers (needed for browser automation tools)
RUN playwright install --with-deps chromium

# Install Claude Code CLI (best-effort — may fail if npm is old, that's OK)
RUN npm install -g @anthropic-ai/claude-code || true

# Copy repo
COPY . .

# Create local Drive volume mount point
RUN mkdir -p /data/ouroboros

# Environment defaults (override via docker-compose env_file or -e flags)
ENV DRIVE_ROOT=/data/ouroboros
ENV REPO_DIR=/app/ouroboros_repo
ENV PYTHONPATH=/app
ENV PYTHONUNBUFFERED=1

CMD ["python", "docker_launcher.py"]
