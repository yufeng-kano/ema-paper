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
    # Host path of the per-user workspaces as the Docker daemon sees it.
    sandbox_workspaces_dir: str = "/data/workspaces"
    sandbox_network: str = "none"
    sandbox_index_url: str = ""
    sandbox_cpus: str = "2"
    sandbox_memory: str = "4g"
    sandbox_pids: int = 512
    sandbox_idle_seconds: int = 1200
    sandbox_output_max_bytes: int = 262_144
    # The longest a single command may run, whatever the caller asked for.
    sandbox_command_timeout_max: int = 600
    sandbox_port: int = 8100

    @model_validator(mode="after")
    def _check(self) -> Settings:
        if self.app_env == "production":
            if not self.sandbox_token:
                raise ValueError("SANDBOX_TOKEN is required in production")
            if self.sandbox_backend != "docker" or self.sandbox_runtime == "runc":
                # No VM boundary is no sandbox (docs/sandbox.md § Isolation).
                raise ValueError("production needs the docker backend with a VM runtime")
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
