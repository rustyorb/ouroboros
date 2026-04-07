FROM python:3.11-slim

# System deps: git (for self-update), curl/nodejs (for Claude Code CLI, optional)
RUN apt-get update && apt-get install -y --no-install-recommends \
        git \
        curl \
        ca-certificates \
        nodejs \
        npm \
    && rm -rf /var/lib/apt/lists/*

# Working directory = the repo
WORKDIR /app

# Install Python deps first (cached layer)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Install Playwright browsers (needed for browser automation tools)
RUN pip install --no-cache-dir playwright playwright-stealth \
    && playwright install chromium --with-deps || true

# Copy the rest of the repo
COPY . .

# Data volume — mount a host directory here for persistent storage
# (state, logs, memory — replaces Google Drive in Colab)
VOLUME ["/data/ouroboros"]

# Tell docker_launcher.py where to find the Drive root and repo
ENV OUROBOROS_DRIVE_ROOT=/data/ouroboros
ENV OUROBOROS_REPO_DIR=/app

# Entrypoint
CMD ["python", "docker_launcher.py"]
