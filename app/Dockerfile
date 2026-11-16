# Build context is the repo root (see docker-compose.yml).
FROM python:3.13-slim
COPY --from=ghcr.io/astral-sh/uv:0.9 /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PROJECT_ENVIRONMENT=/opt/venv \
    PATH="/opt/venv/bin:$PATH" PYTHONUNBUFFERED=1 \
    COVE_SCHEMA_DIR=/srv/schemas COVE_BRAND_DIR=/srv/brand COVE_TEMPLATE_DIR=/srv/templates COVE_MIGRATIONS_DIR=/srv/db/migrations
WORKDIR /srv/app
COPY app/pyproject.toml ./
RUN uv sync --no-install-project
COPY app/src ./src
RUN uv sync
COPY schemas /srv/schemas
COPY brand /srv/brand
COPY templates /srv/templates
COPY db/migrations /srv/db/migrations
RUN useradd --system --uid 10001 cove
USER cove
