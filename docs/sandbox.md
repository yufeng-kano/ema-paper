# Sandbox — one VM per user

The chat agent's `bash` tool runs inside a microVM that belongs to the user: `/workspace` is their workspace directory ([workspace.md](./workspace.md)), nothing of the service is reachable from inside, and the VM is stopped when idle and started again on the next command. Files survive; shell state and installed packages survive too — only `reset` discards them.

## The runner

A separate service, `apps/sandbox` (`sightread-sandbox` in compose), is the only container with the Docker socket. The API and the worker never talk to Docker; they call the runner over HTTP on the compose network with a shared token (`SANDBOX_TOKEN`). The runner starts one container per user from the sandbox image under the configured runtime and keeps shell sessions open inside it.

| Route | What it does |
|-------|--------------|
| `GET /sandboxes/{user_id}` | `{state: stopped\|starting\|running, last_used_at, shells, uptime_seconds}` |
| `POST /sandboxes/{user_id}/exec` | `{session, command, timeout_seconds}` → `{output, exit_code, timed_out, truncated}`. Starts the VM when stopped. The session names a persistent shell: working directory, environment and background processes carry from one command to the next, which is what the bash tool's contract expects. |
| `POST /sandboxes/{user_id}/restart` | `{session}` — kill that shell and open a fresh one (the bash tool's `restart`) |
| `POST /sandboxes/{user_id}/stop` | stop the VM, keep everything on disk |
| `POST /sandboxes/{user_id}/reset` | stop, and delete the package cache and shell state under `/workspace/.cache`; documents are untouched |

Output is captured as combined stdout and stderr, cut at `SANDBOX_OUTPUT_MAX_BYTES` with the head and the tail kept, so a command that prints a file cannot flood the conversation. A command past `timeout_seconds` is killed with its process group and reported as timed out; the shell survives.

Two backends implement the same interface: `docker` (production: `docker run` with `--runtime {SANDBOX_RUNTIME}`, the user directory bind-mounted at `/workspace`, `--network {SANDBOX_NETWORK}`, cpu/memory/pids limits, a read-only root filesystem and a tmpfs `/tmp`) and `local` (tests only: a bash process in a temporary directory, no isolation — never deployed).

## Isolation

- **Runtime:** Kata Containers (`SANDBOX_RUNTIME=kata`) — every container is its own KVM microVM with its own kernel; Cloud Hypervisor is Kata's default hypervisor, Firecracker is the alternative. The host needs `/dev/kvm` and the Kata runtime installed ([deployment.md](./deployment.md)). `runc` works for local development and gives no VM boundary; it is refused when `APP_ENV=production`.
- **Network:** `SANDBOX_NETWORK=none` by default. Package installs need one egress: deploy a PyPI-only proxy on an internal compose network, point `SANDBOX_NETWORK` at that network and `SANDBOX_INDEX_URL` at the proxy; the proxy allows GET to the index and wheel hosts and nothing else — it must not become a channel out of the VM.
- **No secrets in the VM.** The upstream key, the database and the API are unreachable from inside; the agent's model calls are made by the worker on the host, never from the VM.
- **Resource limits:** `SANDBOX_CPUS`, `SANDBOX_MEMORY`, `SANDBOX_PIDS`, per-command timeout, output cap. Shells per user are capped at `MAX_CHAT_TURNS_PER_USER`: a conversation turn holds one shell.
- **The document is in the VM.** A prompt injection in a parsed PDF can at worst make the agent act on that user's own files, which revisions undo; it cannot reach another user's directory or the service.

## Lifecycle

- **Start on first use.** The first `exec` of a user whose VM is stopped boots it (Kata boots in about a second; the UI shows a starting state meanwhile).
- **Stop when idle.** `SANDBOX_IDLE_SECONDS` after the last command the runner stops the VM. Nothing is deleted, ever, by the runner.
- **Reset is the user's.** The Usage page's reset deletes `/workspace/.cache` (uv's cache and installed environments) and shell state; documents, notes and history stay.

## The image

`deploy/sandbox-image/Dockerfile`: Debian with `uv` and Python, `poppler-utils`, `ripgrep`, `jq`, `pandoc`, `imagemagick` and `git`, a non-root `agent` user, `/workspace` as the working directory, `UV_CACHE_DIR=/workspace/.cache/uv` and `UV_INDEX_URL` from `SANDBOX_INDEX_URL`. Built and tagged by the operator like the service images; `SANDBOX_IMAGE` names it.

## Operations

- Install the Kata runtime and register it with Docker as `kata` (the runtime binary, the guest kernel and the rootfs image are Kata's packages; Docker's `daemon.json` gets `"runtimes": {"kata": {"path": "/usr/bin/containerd-shim-kata-v2"...}}` per Kata's Docker guide). Verify with `docker run --runtime kata --rm hello-world` before enabling the sandbox.
- `{WORKSPACES_DIR}` is a host directory bind-mounted into `api`, `worker` and `sandbox`; `SANDBOX_WORKSPACES_DIR` is the same directory as the Docker daemon sees it (the runner passes host paths to `docker run`). A per-user quota is a filesystem concern — XFS project quotas or one fixed-size image per user mounted under the directory — see [workspace.md](./workspace.md) § Quota.
- Logs: every command is recorded in the conversation (`chat_events`), never only in the runner's log.
