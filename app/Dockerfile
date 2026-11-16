# Build context is the repo root (see docker-compose.yml).
FROM python:3.15-slim@sha256:65a65fc7b30b06cb6df4930fd4650697cb23f4c2698ab81fc87e4b031bc36928
# Pick up Debian security fixes released after the pinned base image, and remove pip/ensurepip:
# the app's dependencies come from uv.lock into /opt/venv, and pip's vendored libraries only add attack surface.
RUN apt-get update && apt-get -y upgrade && rm -rf /var/lib/apt/lists/* \
 && rm -rf /usr/local/bin/pip* /usr/local/lib/python3.13/site-packages/pip* /usr/local/lib/python3.13/ensurepip
COPY --from=ghcr.io/astral-sh/uv:0.9@sha256:538e0b39736e7feae937a65983e49d2ab75e1559d35041f9878b7b7e51de91e4 /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PROJECT_ENVIRONMENT=/opt/venv \
    PATH="/opt/venv/bin:$PATH" PYTHONUNBUFFERED=1 \
    COVE_SCHEMA_DIR=/srv/schemas COVE_BRAND_DIR=/srv/brand COVE_BLOB_DIR=/data/blobs COVE_TEMPLATE_DIR=/srv/templates COVE_MIGRATIONS_DIR=/srv/db/migrations
WORKDIR /srv/app
COPY app/pyproject.toml app/uv.lock ./
RUN uv sync --frozen --no-install-project   # exactly what uv.lock says, or fail
COPY app/src ./src
RUN uv sync --frozen
COPY schemas /srv/schemas
COPY brand /srv/brand
COPY ETHOS.md /srv/ETHOS.md
COPY obsidian-plugin/main.js obsidian-plugin/manifest.json obsidian-plugin/styles.css /srv/obsidian-plugin/
COPY templates /srv/templates
COPY db/migrations /srv/db/migrations
COPY db/control /srv/db/control
RUN useradd --system --uid 10001 cove && mkdir -p /data/blobs && chown -R cove /data
USER cove
