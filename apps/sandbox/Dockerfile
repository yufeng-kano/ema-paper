# The sandbox runner (docs/sandbox.md): a small FastAPI service that owns the Docker
# socket and starts one VM per user. Only the Docker CLI is needed — the daemon is the
# host's, reached through the mounted socket.
FROM docker:27-cli AS docker-cli

FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim

COPY --from=docker-cli /usr/local/bin/docker /usr/local/bin/docker

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/usr/local \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev --no-install-project

COPY src ./src
RUN uv sync --locked --no-dev

EXPOSE 8100
CMD ["python", "-m", "sightread_sandbox"]
