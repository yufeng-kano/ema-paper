# API and worker share this image (docs/project-structure.md).
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim

# Poppler is the PDF engine, invoked only as a subprocess (docs/parsing.md); git keeps
# every workspace's revisions, invoked the same way (docs/workspace.md § Revisions).
RUN apt-get update \
    && apt-get install -y --no-install-recommends poppler-utils git \
    && rm -rf /var/lib/apt/lists/*

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/usr/local \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Dependency layer first so source edits do not reinstall the world.
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev --no-install-project

COPY alembic.ini ./
COPY migrations ./migrations
COPY src ./src
RUN uv sync --locked --no-dev

EXPOSE 8000
CMD ["uvicorn", "sightread.main:app", "--host", "0.0.0.0", "--port", "8000"]
