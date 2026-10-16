"""The runner's settings — `SANDBOX_*` in the environment (docs/sandbox.md)."""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=None, extra="ignore", case_sensitive=False)

    app_env: Literal["local", "production"] = "local"
    # What api and worker present; empty refuses every request.
    sandbox_token: str = ""
    # `docker` (production) or `local` (tests and nothing else: a bash process, no VM).
    sandbox_backend: Literal["docker", "local"] = "docker"
    # The Docker runtime: `kata` is a VM per container; `runc` is for local development.
    sandbox_runtime: str = "kata"
    sandbox_image: str = "sightread-sandbox"
    # Host path of the per-user workspaces as the Docker daemon sees it: what `docker run`
    # bind-mounts into a user's VM.
    sandbox_workspaces_dir: str = "/data/workspaces"
    # The same directory as this runner sees it (compose mounts the host path here): what
    # the runner itself reads and clears.
    sandbox_workspaces_mount: str = "/data/workspaces"
    sandbox_network: str = "none"
    sandbox_index_url: str = ""
    sandbox_cpus: str = "2"
    sandbox_memory: str = "4g"
    sandbox_pids: int = 512
    # The VM's writable scratch space at /tmp (a tmpfs, so it counts against memory).
    sandbox_tmp_size: str = "1g"
    sandbox_idle_seconds: int = 1200
    # Shells one user's VM keeps open at once; the longest idle one makes room for a new one.
    sandbox_max_shells: int = 8
    sandbox_output_max_bytes: int = 262_144
    # The longest command the runner accepts, in characters; a longer one answers 422.
    sandbox_command_max_chars: int = 65_536
    # The longest a single command may run, whatever the caller asked for.
    sandbox_command_timeout_max: int = 600
    # How long Docker is given to remove a VM on stop.
    sandbox_docker_timeout_seconds: int = 120
    # How long `docker run` may take to start a VM; api and worker allow the same on top
    # of a command's timeout, so a cold start never looks like a lost command.
    sandbox_start_timeout_seconds: int = 180
    sandbox_port: int = 8100
    # The runtime names production accepts: VM runtimes, so a shared-kernel runtime
    # under another name cannot pass for a VM boundary (docs/sandbox.md § Isolation).
    sandbox_vm_runtimes: str = "kata,kata-runtime,io.containerd.kata.v2,kata-qemu,kata-clh,kata-fc"

    @model_validator(mode="after")
    def _check(self) -> Settings:
        if self.sandbox_backend == "docker" and not self.sandbox_workspaces_dir.startswith("/"):
            # `docker run -v` takes the host path as the daemon sees it: relative to
            # nothing this runner knows (docs/sandbox.md § Operations).
            raise ValueError("SANDBOX_WORKSPACES_DIR must be an absolute host path")
        if self.app_env == "production":
            if not self.sandbox_token:
                raise ValueError("SANDBOX_TOKEN is required in production")
            allowed = {name.strip() for name in self.sandbox_vm_runtimes.split(",") if name.strip()}
            if self.sandbox_backend != "docker" or self.sandbox_runtime not in allowed:
                # No VM boundary is no sandbox (docs/sandbox.md § Isolation).
                raise ValueError(
                    "production needs the docker backend with a VM runtime "
                    f"(SANDBOX_RUNTIME must be one of SANDBOX_VM_RUNTIMES: {sorted(allowed)})"
                )
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
