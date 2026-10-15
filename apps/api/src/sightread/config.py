"""Environment settings. `.env.example` at the repo root is the authoritative variable list."""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Only ever used when APP_ENV=local and SECRET_KEY is unset, so `uv run uvicorn` works
# without a .env file. Production refuses to start without a real SECRET_KEY.
LOCAL_FALLBACK_SECRET_KEY = "local-development-insecure-secret-key"  # noqa: S105


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=None, extra="ignore", case_sensitive=False)

    app_env: Literal["local", "production"] = "local"
    app_url: str = "http://localhost:8000"
    web_url: str = "http://localhost:3000"
    # The web app's non-default locale prefixes, comma-separated, mirroring its i18n config
    # (apps/web/nuxt.config.ts uses `prefix_except_default`, so the default locale has none).
    # Sign-in returns the visitor to `WEB_URL/<prefix>` when they picked one; a value outside
    # this list is ignored, which is what keeps the return destination un-forgeable.
    web_locale_prefixes: str = "zh-TW"

    database_url: str = "postgresql+asyncpg://sightread:sightread@localhost:5432/sightread"
    postgres_user: str = "sightread"
    postgres_password: str = "sightread"  # noqa: S105 - compose default, not a secret
    postgres_db: str = "sightread"

    secret_key: str = ""

    google_client_id: str = ""
    google_client_secret: str = ""

    auth_dev_mode: bool = False

    # Kano Proxy, the built-in upstream (docs/auth.md § 6). The origin only: vision calls
    # go to `{origin}/openai/v1`, consent to `{origin}/connect/authorize`, the token
    # exchange to `{origin}/api/connect/token`. Any of the three empty = unavailable.
    kano_proxy_base_url: str = ""
    kano_proxy_client_id: str = ""
    kano_proxy_client_secret: str = ""
    # Lifetime of a pending connect state (state + PKCE verifier).
    kano_connect_ttl_seconds: int = 600

    upload_max_bytes: int = 134_217_728
    page_cap: int = 500
    max_jobs_per_user: int = 2
    vision_concurrency_per_job: int = 8
    render_workers: int = 0  # 0 means "CPU count", resolved by the worker
    upload_dir: str = "/data/uploads"
    # Stored figure crops, kept with the result — outside the upload dir on purpose, so the
    # sweeper never touches them (docs/parsing.md § Figure crops).
    figures_dir: str = "/data/figures"
    # Cap on crops per page: a malicious user-defined upstream must not turn one response
    # into unbounded PNG encodes and durable disk (docs/parsing.md § Figure crops).
    figures_per_page_max: int = 40
    # Cap on a user's custom system prompt (docs/api.md § Limits).
    system_prompt_max_chars: int = 8000
    # Cap on any upstream response body — a user-controlled endpoint must not be able to
    # exhaust memory with an unbounded reply (docs/parsing.md § Upstream usage).
    upstream_response_max_bytes: int = 33_554_432
    # Upload tickets minted by the MCP `parse` tool (docs/auth.md § 5).
    upload_ticket_ttl_seconds: int = 3600
    upload_ticket_rate_per_hour: int = 30

    # Per-user workspaces: every user's files and revisions, bind-mounted at the same path
    # into api, worker and the sandbox runner (docs/workspace.md).
    workspaces_dir: str = "/data/workspaces"
    workspace_quota_bytes: int = 10_737_418_240
    # The largest document file the API opens; the shell can write anything.
    content_max_bytes: int = 8_388_608

    # The chat agent's limits (docs/chat.md).
    max_chat_turns_per_user: int = 4
    chat_max_output_tokens: int = 64_000
    chat_max_tool_calls_per_turn: int = 50
    chat_turn_timeout_seconds: int = 900
    chat_tool_result_max_chars: int = 20_000
    chat_bash_timeout_seconds: int = 120

    # The sandbox runner (docs/sandbox.md). Empty URL = no sandbox: the chat agent runs
    # without its bash tool. The token is what api and worker present to the runner.
    sandbox_url: str = ""
    sandbox_token: str = ""

    domain: str = ""
    acme_email: str = ""

    @model_validator(mode="after")
    def _check_secrets(self) -> Settings:
        if not self.secret_key:
            if self.app_env != "local":
                raise ValueError("SECRET_KEY is required when APP_ENV is not 'local'")
            self.secret_key = LOCAL_FALLBACK_SECRET_KEY
        # One spelling of the origin everywhere it is joined or compared — the dedup
        # snapshot included (docs/jobs.md § Dedup).
        self.kano_proxy_base_url = self.kano_proxy_base_url.strip().rstrip("/")
        return self

    @property
    def dev_login_enabled(self) -> bool:
        """`POST /api/auth/dev-login` exists only under both conditions (docs/auth.md)."""
        return self.app_env == "local" and self.auth_dev_mode

    @property
    def google_oidc_configured(self) -> bool:
        return bool(self.google_client_id and self.google_client_secret)

    @property
    def kano_proxy_configured(self) -> bool:
        return bool(
            self.kano_proxy_base_url and self.kano_proxy_client_id and self.kano_proxy_client_secret
        )

    @property
    def kano_proxy_api_base(self) -> str:
        """The OpenAI-compatible base vision calls and the model catalog use."""
        return f"{self.kano_proxy_base_url}/openai/v1"

    @property
    def kano_proxy_anthropic_base(self) -> str:
        """The Anthropic-format base the chat agent calls (docs/chat.md § Upstream)."""
        return f"{self.kano_proxy_base_url}/anthropic/v1"

    @property
    def sandbox_configured(self) -> bool:
        return bool(self.sandbox_url and self.sandbox_token)

    @property
    def kano_redirect_uri(self) -> str:
        """Exactly what the authorize request and the token exchange both send."""
        return f"{self.app_url.rstrip('/')}/api/kano/callback"

    def web_url_for_locale(self, prefix: str) -> str:
        """`WEB_URL`, prefixed when the visitor picked a non-default locale.

        An unknown prefix is not an error, it is simply the default locale: the value
        reaches us from a query string, so treating it as an allowlist rather than as a
        path fragment is what stops it becoming an open redirect (docs/auth.md § 1).
        """
        allowed = {item.strip() for item in self.web_locale_prefixes.split(",") if item.strip()}
        return f"{self.web_url}/{prefix}" if prefix in allowed else self.web_url


@lru_cache
def get_settings() -> Settings:
    return Settings()
