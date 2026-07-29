# NewsPulse — dashboard + daily sweep in one image.
#
# The image carries the Claude Code CLI because analysis and Captain Comms both
# shell out to it (subscription-first, never an API key baked in). Its
# credentials are NOT in the image: they live in the config directory mounted at
# runtime, so the image holds no secret and can be rebuilt freely.
FROM python:3.11-slim

# curl for the CLI installer and the healthcheck; ca-certificates so the feed
# fetches can verify TLS at all.
RUN apt-get update \
 && apt-get install -y --no-install-recommends curl ca-certificates nodejs npm \
 && rm -rf /var/lib/apt/lists/*

# The analyzer and the assistant both invoke `claude`; without it the sweep
# fails per batch and the drawer reports "CLI not found".
RUN npm install -g @anthropic-ai/claude-code && npm cache clean --force

# This file lives at the repository root, and the app lives in newspulse/, hence
# the prefixed COPY paths. It sits here so a PaaS that auto-detects a Dockerfile
# finds it with no configuration at all: an earlier layout kept it beside the app
# and depended on railway.json to point at it, which fails silently if the
# platform doesn't read that file. Building must not depend on config discovery.
# See .dockerignore for what the repo-root context excludes.
WORKDIR /app
COPY newspulse/pyproject.toml newspulse/uv.lock newspulse/README.md ./
COPY newspulse/src ./src
COPY newspulse/migrations ./migrations
COPY newspulse/alembic.ini ./

RUN pip install --no-cache-dir uv && uv sync --frozen --no-dev

# The database is a single SQLite file and must outlive the container.
# Both the database and the CLI login live under one mount: a platform that
# gives a service a single volume (Railway) cannot attach two, and nothing is
# gained by separating them — they are lost together or kept together.
ENV NEWSPULSE_DATABASE_PATH=/data/newspulse.db \
    NEWSPULSE_WEB_HOST=0.0.0.0 \
    NEWSPULSE_CLAUDE_CONFIG_DIR=/data/claude
VOLUME ["/data"]
EXPOSE 8000

# Migrations run on start so a deploy never serves a schema older than its code.
CMD ["sh", "-c", "uv run alembic upgrade head && uv run newspulse-web"]
