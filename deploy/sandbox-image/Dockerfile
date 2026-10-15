# What runs inside a user's VM (docs/sandbox.md § The image). Built and tagged by the
# operator: `docker build -t sightread-sandbox deploy/sandbox-image`.
FROM debian:bookworm-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        bash ca-certificates curl git \
        poppler-utils ripgrep jq pandoc imagemagick \
        python3 \
    && rm -rf /var/lib/apt/lists/*

# uv manages Python environments inside the workspace; its cache lives there too, so a
# package installed once is there on the next boot (docs/sandbox.md § Lifecycle).
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /usr/local/bin/

RUN useradd --create-home --uid 1000 --shell /bin/bash agent \
    && mkdir -p /workspace && chown agent:agent /workspace

USER agent
WORKDIR /workspace
ENV UV_CACHE_DIR=/workspace/.cache/uv \
    UV_PYTHON_INSTALL_DIR=/workspace/.cache/python \
    HOME=/workspace/.cache/home \
    PATH=/workspace/.cache/home/.local/bin:/usr/local/bin:/usr/bin:/bin

CMD ["sleep", "infinity"]
