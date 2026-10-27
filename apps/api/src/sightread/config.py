"""Environment settings. `.env.example` at the repo root is the authoritative variable list."""

from __future__ import annotations

from decimal import Decimal
from functools import cached_property, lru_cache
from typing import Literal

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from .billing.credits import ModelPrice, parse_model_prices
from .billing.plans import FREE_PLAN_ID, Pack, Plan, parse_packs, parse_plans

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

    # The operator console (docs/admin.md): the Google addresses that join its built-in
    # owner group when they sign in with the address verified, comma-separated, in any
    # letter case — empty = no console, `/api/admin` not mounted; how many hours after a
    # sign-in a session may still use it; and the rows one page of its lists holds.
    owner_emails: str = ""
    admin_reauth_hours: int = 12
    admin_page_size: int = 50

    # The console's jobs and usage (docs/admin.md § Overview, jobs and usage): how long the
    # overview waits for each of Kano Proxy's and Kano Pay's catalogs, the most days one read
    # of the usage may span — never under the Usage page's longest range, 90 days — and how
    # many hours back the overview counts failed jobs, a year at most.
    admin_probe_timeout_seconds: float = 5
    admin_usage_max_days: int = 366
    admin_failed_window_hours: int = 24

    # Kano Proxy, the only upstream (docs/auth.md § 3): the origin only — vision calls go
    # to `{origin}/openai/v1`, chat to `{origin}/anthropic/v1` — and the operator's own key,
    # presented on every upstream call. Either empty = no upstream, 503 `upstream`.
    kano_proxy_base_url: str = ""
    kano_proxy_api_key: str = ""

    # The subscription (docs/billing.md): the models the deployment offers and their list
    # prices.
    parse_model: str = ""
    check_model: str = ""
    parse_fallback_model: str = ""
    # `{"<model>": {"input", "cache_read", "cache_write", "output"}}`, USD per 1M tokens. The
    # chat agent offers the live catalog's models priced here, in this order.
    model_prices_json: str = "{}"
    # What one credit is worth in list-price USD, and the flat rates (docs/billing.md
    # § Credits).
    credit_usd: Decimal = Decimal("0.002")
    parse_credits_per_page: int = 1
    chat_min_credits: int = 1
    # The Free plan (docs/billing.md § Plans).
    free_credits_per_period: int = 150
    free_storage_bytes: int = 512_000_000
    free_max_jobs: int = 1
    # What the public policy pages print (docs/web.md § Pages): the support address and
    # who operates the service, served by `GET /api/site`; empty = not printed.
    support_email: str = ""
    operator_name: str = ""
    # Kano Pay (docs/billing.md § Kano Pay): all four set = configured; otherwise every
    # user is on Free and checkout is unavailable.
    kano_pay_url: str = ""
    kano_pay_app_id: str = ""
    kano_pay_api_key: str = ""
    kano_pay_webhook_secret: str = ""
    # `[{"planId", "credits", "storageBytes", "sandbox", "maxJobs"}]`; prices are Pay's.
    kano_pay_plans_json: str = "[]"
    # `[{"packId", "credits", "validDays"}]`: what a top-up pack grants, Pro only; prices
    # are Pay's (docs/billing.md § Top-up packs). Empty = no pack is sold.
    kano_pay_packs_json: str = "[]"
    billing_refresh_seconds: int = 120
    # Each call to Pay's App API, and how far a notification's timestamp may be from
    # ours before it is refused as a replay.
    kano_pay_timeout_seconds: float = 15
    kano_pay_signature_tolerance_seconds: int = 300
    # How long one answer from Pay serves every catalog read, and the most a
    # notification may carry.
    kano_pay_catalog_ttl_seconds: int = 300
    kano_pay_event_max_bytes: int = 65_536
    # How long a checkout Pay opened can still be paid (Pay's session lifetime): an account
    # is not deleted while one of its own is that young (docs/auth.md § Deleting the
    # account).
    kano_pay_checkout_lifetime_seconds: int = 86_400
    # Redeem codes (docs/billing.md § Redeem codes): the prefix a code is printed and typed
    # with, how many random characters follow it, how many days one granted period of a
    # paid plan runs, and the refused attempts a user may make in the window before 429.
    redeem_code_prefix: str = "EMA"
    redeem_code_chars: int = 12
    grant_period_days: int = 30
    redeem_max_failures: int = 10
    redeem_failure_window_seconds: int = 3600

    upload_max_bytes: int = 134_217_728
    # A file of any other kind uploaded to the library, kept once in the tree
    # (docs/workspace.md § Files): the most it may be. Caddy caps that one route to match.
    file_max_bytes: int = 2_000_000_000
    page_cap: int = 500
    max_jobs_per_user: int = 2
    # The `Retry-After` a refused parse (429, the per-user job cap) tells the client.
    parse_retry_after_seconds: int = 30
    # A library upload retried after the first request's answer was lost is answered with
    # the job that answered the first, even one already finished, when it finished this
    # recently (docs/jobs.md § Dedup).
    upload_retry_window_seconds: int = 60
    vision_concurrency_per_job: int = 8
    # The tokens one vision call's answer may take; 0 sends no cap (docs/parsing.md
    # § Upstream usage).
    vision_max_output_tokens: int = 16_000
    render_workers: int = 0  # 0 means "CPU count", resolved by the worker
    upload_dir: str = "/data/uploads"
    # How old an entry of the upload directory may grow before the sweeper takes it, an
    # unsent chat attachment's included (docs/jobs.md § Retention).
    upload_max_age_seconds: int = 86_400
    # How many days a finished parse no file names is kept before the worker's sweep
    # deletes it; 0 keeps every one (docs/jobs.md § Retention).
    parse_retention_days: int = 30
    # Stored figure crops, kept with the result — outside the upload dir on purpose, so the
    # sweeper never touches them (docs/parsing.md § Figure crops).
    figures_dir: str = "/data/figures"
    # Cap on crops per page: a malicious user-defined upstream must not turn one response
    # into unbounded PNG encodes and durable disk (docs/parsing.md § Figure crops).
    figures_per_page_max: int = 40
    # Model rounds one page's figure check may take, one call each; at least 1
    # (docs/parsing.md § Figure check).
    figure_check_rounds: int = 2
    # Cap on a user's custom system prompt (docs/api.md § Limits).
    system_prompt_max_chars: int = 12_000
    # Cap on any upstream response body — a user-controlled endpoint must not be able to
    # exhaust memory with an unbounded reply (docs/parsing.md § Upstream usage).
    upstream_response_max_bytes: int = 33_554_432
    # The chat agent's event stream: one SSE line past this is a broken upstream, not a
    # long answer, and more content blocks than this is refused rather than allocated
    # for (docs/chat.md § Upstream).
    upstream_sse_line_max_bytes: int = 4 * 1024 * 1024
    upstream_content_blocks_max: int = 4096
    # Upload tickets minted by the MCP `parse` tool (docs/auth.md § 5).
    upload_ticket_ttl_seconds: int = 3600
    upload_ticket_rate_per_hour: int = 30

    # Per-user workspaces: every user's files and revisions, bind-mounted at the same path
    # into api, worker and the sandbox runner (docs/workspace.md).
    workspaces_dir: str = "/data/workspaces"
    workspace_quota_bytes: int = 10_737_418_240
    # The largest document file the API opens; the shell can write anything.
    content_max_bytes: int = 8_388_608
    # The largest file a revision records; a larger or a binary one is kept in the tree but
    # not in history (docs/workspace.md § Revisions).
    revision_max_file_bytes: int = 1_048_576
    # An image inserted into a document: the upload's cap, and the long edge it is kept at
    # (docs/workspace.md § Inserted images).
    document_image_max_bytes: int = 20 * 1024 * 1024
    document_image_long_edge: int = 2400
    # A page of an original drawn for the retry dialog: its long edge, and how many the
    # API process makes at once (docs/api.md § Limits).
    page_preview_long_edge: int = 1200
    page_preview_renders: int = 2
    # The format check of a Files upload: spots sent to the check agent, calls one check
    # may make, and the tokens of one answer (docs/parsing.md § Format check).
    check_spots_max: int = 80
    check_max_calls: int = 16
    check_max_output_tokens: int = 16_000

    # The chat agent's limits (docs/chat.md).
    max_chat_turns_per_user: int = 4
    # The `Retry-After` a refused send (429, the per-user turn cap) tells the client.
    chat_retry_after_seconds: int = 15
    chat_max_output_tokens: int = 64_000
    # High enough that a document translated block by block is one turn (docs/chat.md
    # § Translation); the cap is a guard against a loop, the timeout bounds the turn.
    chat_max_tool_calls_per_turn: int = 1000
    # Tool calls a turn may still make once the credits run out mid-turn, its subagents'
    # included; then it answers (docs/billing.md § A chat turn).
    chat_overdraft_max_tool_calls: int = 25
    chat_turn_timeout_seconds: int = 3600
    chat_stream_timeout_seconds: int = 900
    chat_tool_result_max_chars: int = 20_000
    chat_message_max_chars: int = 32_000
    chat_selection_max_chars: int = 16_000
    chat_bash_timeout_seconds: int = 120
    chat_writer_grace_seconds: int = 60
    revert_max_bytes: int = 64 * 1024 * 1024
    revert_max_paths: int = 2000
    workspace_git_timeout_seconds: int = 60
    # How many loose objects a workspace's history may hold before the worker's sweep
    # packs it (docs/workspace.md § Revisions).
    workspace_git_pack_loose_objects: int = 100
    # How many days an item stays in the Trash before the worker's sweep deletes it for
    # good; 0 = never (docs/workspace.md § The Trash).
    trash_retention_days: int = 30
    # The most of a git log the API reads to list revisions, and the paths it keeps per
    # revision; the rename and path lists a revert reads share the byte bound
    # (docs/workspace.md § Revisions).
    workspace_log_max_bytes: int = 4 * 1024 * 1024
    workspace_log_files_max: int = 200
    chat_image_round_max_bytes: int = 32 * 1024 * 1024
    chat_image_turn_max_bytes: int = 128 * 1024 * 1024
    # The most an image document may decode to for `view_page`, read from its header
    # before any decoding (docs/chat.md § Tools).
    chat_page_image_max_pixels: int = 40_000_000
    # The stored history a conversation may replay, every message together: a turn past
    # it is refused with a pointer to a new conversation (docs/chat.md § History).
    chat_history_max_bytes: int = 256 * 1024 * 1024
    # The composer's attachments: one file's upload cap, how many go with one message, how
    # much of a PDF's or text file's text the model is given, and the long edge an image is
    # kept at — what Claude would resize to (docs/chat.md § What a user message carries).
    chat_attachment_max_bytes: int = 20 * 1024 * 1024
    chat_attachments_per_message: int = 8
    chat_attachment_text_max_chars: int = 100_000
    chat_attachment_image_long_edge: int = 1568
    # How many of a user's attachments may wait for their message at once — each is bounded,
    # their number must be too, or uploads never sent could fill the upload directory for a
    # day — and how long the reader's browser may keep an attached image's bytes.
    chat_attachments_pending_max: int = 32
    chat_attachment_cache_seconds: int = 31_536_000
    # How long `pdftotext` may take on an attached PDF (docs/parsing.md).
    chat_attachment_pdf_timeout_seconds: float = 60.0
    # A user's own skills and subagents, and the tool calls one subagent run may make
    # (docs/chat.md § Skills, § Subagents).
    chat_skills_max: int = 64
    chat_subagents_max: int = 32
    chat_subagent_max_tool_calls: int = 1000
    # The reader's instructions, appended after the prompt (docs/chat.md § Instructions).
    chat_instructions_max_chars: int = 8000
    # The agent's memories of the reader: one memory's text, how many a reader keeps, and
    # how much of them one conversation loads into its memory block (docs/chat.md § Memory).
    chat_memory_max_chars: int = 500
    chat_memories_max: int = 200
    chat_memory_block_max_chars: int = 12_000
    # How much of the agent's memory file a conversation reads into its system prompt
    # (docs/chat.md § Memory, *The memory file*).
    chat_memory_file_max_chars: int = 16_000
    # Compaction (docs/chat.md § Compaction): the context a conversation must have reached
    # for the worker to compact it before its cache expires, how long before the expiry
    # it is enqueued, how often the worker looks, and the tool calls a compaction turn
    # may make (to bring memory.md up to date) before it answers.
    chat_idle_compact_min_tokens: int = 100_000
    chat_idle_compact_lead_seconds: int = 360
    chat_idle_compact_sweep_seconds: float = 60.0
    chat_compact_max_tool_calls: int = 8
    # The context window the chat panel's ring measures a conversation against when
    # `MODEL_PRICES_JSON` names no `context` for its model.
    chat_context_window_tokens: int = 200_000
    # Whole sections the reader adds to one message by their heading (docs/chat.md § What
    # a user message carries); each is capped like the selection, its heading on its own.
    chat_sections_per_message: int = 4
    chat_section_heading_max_chars: int = 512
    # The chat agent's web tools, run on the worker (docs/chat.md § Tools): the switch, the
    # keys `web_search` (Brave) and `search_papers` (OpenAlex) run on — each tool absent
    # while its key is empty — and what a web search costs the reader on top of the turn's
    # tokens (docs/billing.md § A chat turn).
    chat_web_access: bool = True
    brave_search_api_key: str = ""
    openalex_api_key: str = ""
    # The endpoints the two searches call: a deployment may point them at a mirror or
    # a proxy of its own.
    brave_search_url: str = "https://api.search.brave.com/res/v1/web/search"
    openalex_works_url: str = "https://api.openalex.org/works"
    web_search_credits: int = 3
    # Per turn, and per UTC day: the two searches for the whole deployment (the operator's
    # bill and OpenAlex budget), downloads per user.
    web_search_max_per_turn: int = 10
    web_search_max_per_day: int = 2000
    paper_search_max_per_turn: int = 10
    paper_search_max_per_day: int = 1000
    read_url_max_per_turn: int = 30
    download_max_per_day: int = 20
    # How much of a page `read_url` reads, the largest download (`file_max_bytes` caps it
    # too), and how long a search or read, and a download, may take.
    read_url_max_bytes: int = 5 * 1024 * 1024
    download_max_bytes: int = 100_000_000
    web_tool_timeout_seconds: float = 30.0
    download_timeout_seconds: float = 120.0

    # The sandbox runner (docs/sandbox.md). Empty URL = no sandbox: the chat agent runs
    # without its bash tool. The token is what api and worker present to the runner.
    sandbox_url: str = ""
    sandbox_token: str = ""
    # What the runner's stop, reset, restart and status calls are given: it must cover
    # the runner's own Docker timeout, the inspection after it and a reset's deletion.
    sandbox_control_timeout_seconds: int = 180
    # What a command may take beyond its own timeout before the runner client gives up:
    # the VM's cold start on a stopped sandbox (the runner's own SANDBOX_START_TIMEOUT_SECONDS)
    # and the reply's grace past the command's deadline (docs/sandbox.md § The runner).
    sandbox_start_timeout_seconds: int = 180
    # The runner's own `--network` and index URL; read here too, so the prompt tells the
    # model whether the VM can reach a package index (docs/chat.md § System prompt).
    sandbox_network: str = "none"
    sandbox_index_url: str = ""
    sandbox_exec_grace_seconds: int = 60

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
        self.kano_pay_url = self.kano_pay_url.strip().rstrip("/")
        self.brave_search_url = self.brave_search_url.strip()
        self.openalex_works_url = self.openalex_works_url.strip()
        if self.credit_usd <= 0:
            raise ValueError("CREDIT_USD must be positive")
        for name in (
            "parse_credits_per_page",
            "chat_min_credits",
            "free_credits_per_period",
            "trash_retention_days",
            "parse_retention_days",
            "web_search_credits",
            "web_search_max_per_turn",
            "web_search_max_per_day",
            "paper_search_max_per_turn",
            "paper_search_max_per_day",
            "read_url_max_per_turn",
            "download_max_per_day",
            "chat_idle_compact_min_tokens",
            "chat_idle_compact_lead_seconds",
            "chat_compact_max_tool_calls",
        ):
            if getattr(self, name) < 0:
                raise ValueError(f"{name.upper()} must be >= 0")
        for name in (
            "read_url_max_bytes",
            "download_max_bytes",
            "web_tool_timeout_seconds",
            "download_timeout_seconds",
            "chat_idle_compact_sweep_seconds",
            "chat_memory_file_max_chars",
            "chat_context_window_tokens",
            "redeem_code_chars",
            "grant_period_days",
            "redeem_max_failures",
            "redeem_failure_window_seconds",
            "admin_reauth_hours",
            "admin_probe_timeout_seconds",
        ):
            if getattr(self, name) <= 0:
                raise ValueError(f"{name.upper()} must be positive")
        if not 1 <= self.admin_page_size <= 500:
            raise ValueError("ADMIN_PAGE_SIZE must be between 1 and 500")
        # The Usage page offers 7, 30 and 90 days; a smaller cap would refuse its own presets.
        if self.admin_usage_max_days < 90:
            raise ValueError("ADMIN_USAGE_MAX_DAYS must be at least 90")
        # A year at most: a far larger window starts before the first date Python can
        # hold, and every overview read would fail computing it.
        if not 1 <= self.admin_failed_window_hours <= 8760:
            raise ValueError("ADMIN_FAILED_WINDOW_HOURS must be between 1 and 8760")
        self.redeem_code_prefix = self.redeem_code_prefix.strip().upper()
        if self.redeem_code_prefix and not self.redeem_code_prefix.isalnum():
            raise ValueError("REDEEM_CODE_PREFIX must be letters and digits only")
        if self.free_storage_bytes > self.workspace_quota_bytes:
            raise ValueError("FREE_STORAGE_BYTES exceeds WORKSPACE_QUOTA_BYTES")
        if self.free_max_jobs > self.max_jobs_per_user:
            raise ValueError("FREE_MAX_JOBS exceeds MAX_JOBS_PER_USER")
        # Parsed once, at start: a malformed table or an offered model nobody priced
        # refuses to start rather than running a model for free (docs/billing.md § Models).
        prices = self.model_prices
        unpriced = [model for model in self.offered_models if model not in prices]
        if unpriced:
            raise ValueError(f"MODEL_PRICES_JSON prices none of: {', '.join(sorted(unpriced))}")
        self.plans  # noqa: B018 - validated for its errors
        self.packs  # noqa: B018 - validated for its errors
        return self

    @cached_property
    def model_prices(self) -> dict[str, ModelPrice]:
        return parse_model_prices(self.model_prices_json)

    @cached_property
    def plans(self) -> dict[str, Plan]:
        """The paid plans, by Pay's plan id (docs/billing.md § Plans)."""
        return parse_plans(
            self.kano_pay_plans_json,
            storage_ceiling=self.workspace_quota_bytes,
            jobs_ceiling=self.max_jobs_per_user,
        )

    @cached_property
    def packs(self) -> dict[str, Pack]:
        """The top-up packs, by Pay's pack id (docs/billing.md § Top-up packs)."""
        return parse_packs(self.kano_pay_packs_json)

    @property
    def free_plan(self) -> Plan:
        return Plan(
            id=FREE_PLAN_ID,
            credits=self.free_credits_per_period,
            storage_bytes=self.free_storage_bytes,
            sandbox=False,
            max_jobs=self.free_max_jobs,
        )

    @property
    def offered_models(self) -> set[str]:
        """Every model the deployment runs on its own, each of which must be priced; the
        chat models are priced by definition (docs/billing.md § Models)."""
        fixed = (self.parse_model, self.check_model, self.parse_fallback_model)
        return {m.strip() for m in fixed if m.strip()}

    @property
    def kano_pay_configured(self) -> bool:
        return bool(
            self.kano_pay_url
            and self.kano_pay_app_id
            and self.kano_pay_api_key
            and self.kano_pay_webhook_secret
        )

    @property
    def dev_login_enabled(self) -> bool:
        """`POST /api/auth/dev-login` exists only under both conditions (docs/auth.md)."""
        return self.app_env == "local" and self.auth_dev_mode

    @property
    def owner_email_set(self) -> frozenset[str]:
        """`OWNER_EMAILS`, each address trimmed and lower-cased, empty items dropped."""
        return frozenset(
            item.strip().lower() for item in self.owner_emails.split(",") if item.strip()
        )

    @property
    def console_enabled(self) -> bool:
        """The operator console exists only while `OWNER_EMAILS` names an address
        (docs/admin.md § Off until configured)."""
        return bool(self.owner_email_set)

    @property
    def google_oidc_configured(self) -> bool:
        return bool(self.google_client_id and self.google_client_secret)

    @property
    def kano_proxy_configured(self) -> bool:
        return bool(self.kano_proxy_base_url and self.kano_proxy_api_key)

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

    @property
    def json_body_max_bytes(self) -> int:
        """The largest JSON request body the API reads: a document's content at its worst
        JSON escaping — a one-byte control character is a six-byte `\\uXXXX`, an emoji's
        four bytes a twelve-byte surrogate pair — plus room for the envelope, so no
        content under the cap is refused for its encoding (docs/api.md). Uploads stream
        under their own cap."""
        return self.content_max_bytes * 6 + 65_536

    @property
    def parse_json_body_max_bytes(self) -> int:
        """The largest JSON `/v1/parse` body: the upload cap in base64 (four bytes per
        three, rounded up) plus room for the envelope (docs/api.md § POST /v1/parse)."""
        return (self.upload_max_bytes + 2) // 3 * 4 + 65_536

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
