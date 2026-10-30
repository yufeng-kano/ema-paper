# What runs inside a user's VM (docs/sandbox.md § The image). Built and tagged by the
# operator: `docker build -t sightread-sandbox deploy/sandbox-image`.
FROM debian:bookworm-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        bash ca-certificates curl git \
        poppler-utils ripgrep jq pandoc imagemagick procps util-linux \
        python3 \
        fontconfig fonts-noto-core fonts-noto-cjk fonts-noto-color-emoji \
    && rm -rf /var/lib/apt/lists/*

# uv manages Python environments inside the workspace; its cache lives there too, so a
# package installed once is there on the next boot (docs/sandbox.md § Lifecycle).
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /usr/local/bin/

# `pip` is uv's: a reflexive `pip install` goes through uv into the default environment,
# the one place a command can install to (docs/sandbox.md § The image).
COPY pip /usr/local/bin/pip
RUN chmod 755 /usr/local/bin/pip && ln -s pip /usr/local/bin/pip3

# matplotlib does not ask fontconfig and falls back to its own DejaVu Sans, which has no
# CJK: this rc names the Noto families first (docs/sandbox.md § The image).
COPY matplotlibrc /etc/matplotlibrc

# Commands run as root *inside the VM*, deliberately: /workspace is a bind mount the api
# and worker containers write as root, so the same identity is what makes the user's
# files writable from the shell. The boundary is the VM (docs/sandbox.md § Isolation),
# not a guest UID; the container also starts with every capability dropped and a
# read-only root filesystem (apps/sandbox).
RUN mkdir -p /workspace

WORKDIR /workspace
# The default environment: what `python` runs and `uv pip install` installs into, made by
# the runner after each start when its interpreter does not run (docs/sandbox.md § The
# image). In the workspace's .cache, so it survives a stop and goes with a reset.
ENV UV_CACHE_DIR=/workspace/.cache/uv \
    UV_PYTHON_INSTALL_DIR=/workspace/.cache/python \
    HOME=/workspace/.cache/home \
    VIRTUAL_ENV=/workspace/.cache/venv \
    MATPLOTLIBRC=/etc/matplotlibrc \
    PATH=/workspace/.cache/venv/bin:/workspace/.cache/home/.local/bin:/usr/local/bin:/usr/bin:/bin

# pid 1 reaps: what a command left running is killed after the command and reparented
# here, and must not stay a zombie against the pid limit (docs/sandbox.md § The image).
# The runner passes this same program on `docker run`.
CMD ["/usr/bin/python3", "-c", "import os, time\nwhile True:\n    try:\n        os.wait()\n    except ChildProcessError:\n        time.sleep(1)\n"]
