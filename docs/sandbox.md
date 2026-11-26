# Sandbox — one VM per user

The chat agent's `bash` tool runs inside a microVM that belongs to the user:

- `/workspace` is their workspace directory ([workspace.md](./workspace.md)).
- Nothing of the service is reachable from inside.
- The VM is stopped when idle and started again on the next command.
- Files survive; shell state and installed packages survive too — only `reset` discards them.

## The runner

A separate service, `apps/sandbox` (`ema-paper-sandbox` in compose), is the only container with the Docker socket. The API and the worker never talk to Docker; they call the runner over HTTP on the compose network with a shared token (`SANDBOX_TOKEN`). The runner starts one container per user from the sandbox image under the configured runtime and keeps shell sessions open inside it.

| Route | What it does |
|-------|--------------|
| `GET /sandboxes/{user_id}` | `{state: stopped\|running, last_used_at, shells, uptime_seconds}`; **503** when Docker cannot say |
| `POST /sandboxes/{user_id}/exec` | `{session, command, timeout_seconds}` → `{output, exit_code, timed_out, truncated}`; starts the VM when stopped; **422** for a command too long |
| `POST /sandboxes/{user_id}/restart` | `{session}` — kill that shell and open a fresh one; **503** when the command could not be confirmed ended |
| `POST /sandboxes/{user_id}/stop` | stop the VM, keep everything on disk; **503** when Docker could not remove it |
| `POST /sandboxes/{user_id}/reset` | stop, and delete the package cache and shell state under `/workspace/.cache`; documents are untouched; **503** when the deletion fails |

- **`GET /sandboxes/{user_id}`**
  - Docker cannot say when the daemon is away or an inspection timed out; the route never answers `stopped` for a VM nothing could start.
  - The API shows that **503** as `unavailable`.
- **`exec`**
  - Too long is longer than `SANDBOX_COMMAND_MAX_CHARS` (65536 characters); the bash tool reports the **422** to the model as a rejected command.
  - `session` names a persistent shell (§ The supervisor).
- **`restart`**
  - It is the bash tool's `restart`, and the worker's cleanup of a turn a dead worker left and of a command it could not confirm over.
  - The command inside the VM goes first, by the supervisor's pid the runner holds, as on a timeout; then the host-side client. Closing the client alone ends nothing inside.
  - **503** when the command could be ended neither inside the VM nor by removing the VM (the sandbox is then unstoppable, as under `stop`), after waiting the removal timeout for Docker to report the VM gone.
  - A restart that answers 200 has confirmed the command dead, which is what the worker records a tree on.
  - The fallback stop reuses the sandbox lock `restart` already holds rather than taking it again.
  - An `exec` whose request is dropped mid-command (the worker died) kills its command the same way, and so does one whose supervisor ends without a status line (a command that killed it): the in-VM kill and sweep run before the lost status is reported, and a VM where they cannot be confirmed is stopped.
- **`stop`**
  - A shell running a command is killed inside the VM first, by the pid the runner holds, before the VM is removed — so a removal that then fails leaves no command still writing behind a client already closed.
  - Idle shells are simply closed.
  - The **503** comes when Docker could not remove the VM and does not report it gone either. `SANDBOX_DOCKER_TIMEOUT_SECONDS` (120) is how long the removal may take.
  - The sandbox is then marked unstoppable and takes no command until Docker reports it gone.
  - The same mark can be set under a running `exec`: a timed-out command whose kill failed and whose VM Docker could not remove. That `exec` does not answer with its outcome: it waits the removal timeout for Docker to report the VM gone, and answers **503** otherwise, so the worker never commits a tree a surviving process may still write ([chat.md](./chat.md) § Tools).
- **`reset`**
  - Held under the user's sandbox lock from the stop through the deletion, so no command starts into a cache being removed.
  - A stop that fails (**503**, as under `stop`) deletes nothing, since a VM still alive could be writing into it.
  - Whatever sits at `.cache` goes — a directory, or the file or link a command put there in its place, removed without following it — so the next start can make the directory again.
  - A deletion that leaves anything behind has failed too (**503**), never a done the next VM would belie.

The api and the worker give the control calls (status, `restart`, `stop`, `reset`) `SANDBOX_CONTROL_TIMEOUT_SECONDS` (180). It must cover the removal, the inspection after it and a reset's cache deletion, so a slow but healthy stop is not reported as a failure.

### The supervisor

Each session's shell is a **supervisor** process in the VM. The supervisor owns the command's lifecycle, because a command that shared the supervisor's stdin could read the framing that follows it and forge the status line.

1. The host sends each command framed by a per-command random marker.
2. The supervisor reads it whole.
3. It runs the command in a fresh `bash` as a child, with its stdin at `/dev/null` and the session's saved working directory and exported environment restored.
4. The child `wait`s for its background jobs and saves the working directory and exported variables back. So `cd` and `export` carry from one command to the next, which is what the bash tool's contract expects; shell functions and unexported variables do not.
5. Only once the child is gone does the supervisor run the cleanup (§ The sweep) and print the status line.

The command never sees the marker and cannot end its own framing early.

### The sweep

Whatever the command started that is still running once its child `bash` is gone — a disowned job, a server, a process that left the shell's session with `setsid` — is killed before the status line.

- It kills every process in the VM but pid 1, the live shells and the exec wrappers they hang from.
- The live shells are named by the runner, which sends the pids of the user's live supervisors with every command. Each supervisor reported its own pid on its stdout before any command ran in it.
- The live shells are never named by anything in the VM a command could write, so a command cannot forge a shell for a process to hide behind.
- The sweep is repeated until a pass finds nothing left to kill. A process forking while the list is walked has children the snapshot missed, reparented to pid 1 once their parent is gone; the next pass takes them.
- A pid already gone when its kill comes — the sweep's own `ps` and the pipeline around it among them — is not left over; one that refuses the kill and still stands is.
- A VM that is not clean after fifty passes fails closed: the supervisor exits without its status line, the runner's kill inside the VM sweeps again and, failing too, stops the VM.
- The same sweep follows a timeout's kill and precedes an adoption.

So nothing a command started writes the tree after the command's revision, and there is no way to keep a process running between commands.

### Output and timeouts

- Output is captured as combined stdout and stderr in fixed-size reads, never line by line, so one newline-free value of any length is cut like any other flood.
- It is kept at `SANDBOX_OUTPUT_MAX_BYTES` with the head and the tail, so a command that prints a file cannot flood the conversation.
- `SANDBOX_COMMAND_TIMEOUT_MAX` (600) is the ceiling on what a caller may ask for.

A command past `timeout_seconds` is killed *inside the VM*:

1. The supervisor reports its pid on its stdout when it opens.
2. The runner kills that process, everything under it and everything in its process group through a second `docker exec`, since ending the host-side client does not end what runs in the container.
3. The host-side session is closed and the timeout reported.
4. The next command of that session opens a fresh shell.

- When that kill cannot be confirmed — the kill fails, the `docker exec` hangs — the runner stops the VM instead, so no command outlives its timeout and goes on writing the workspace after the host has moved on.
- A VM that cannot be stopped either takes no further commands until Docker positively reports it gone.
  - It cannot be stopped when the removal fails or times out and an inspection does not report it absent or stopped.
  - Every stop makes the same check — `stop`, `reset` and the idle sweep included — since closing the host-side clients confirms nothing about what runs inside.
  - An inspection that fails (the daemon away) is not "gone": it is unknown, and unknown keeps the sandbox closed.

### The VM's PATH

The VM's `PATH` starts in the workspace — the default Python environment's `bin`, then `~/.local/bin` (§ The image) — which commands write, so nothing the runner relies on is looked up through it:

- The runner starts its programs in the VM by their full path (`/usr/bin/setsid`, `/bin/sh`, the reaper's `/usr/bin/python3`).
- The supervisor, the timeout's kill, the adoption's sweep and the default environment's check look their own programs up in the image's system directories alone.

A `ps`, a `bash` or a `pgrep` a command put first on the PATH would otherwise stand in for the system's in the very sweep that ends what the command left running. The command itself, and every command after it, runs with the VM's PATH, as the session saved it.

### Backends

The runner has one backend, Docker:

- `docker run` with `--runtime {SANDBOX_RUNTIME}`.
- The user directory bind-mounted at `/workspace`.
- Over it, the user's `.sightread/` bind-mounted **read-only** at `/workspace/.sightread`: the parse baseline the agent compares against is presented as untouched, and the root shell must not be able to edit or delete it. The runner makes the directory before the start, so the nested mount always has its target.
- `--network {SANDBOX_NETWORK}`, with the egress proxy in `HTTPS_PROXY` and `HTTP_PROXY` when that is a network (§ Network).
- cpu/memory/pids limits, a read-only root filesystem and a tmpfs `/tmp`.
- Then, inside the started VM, the default Python environment when it is missing (§ The image).

The tests drive the shared backend and shell logic with a local bash stand-in (`apps/sandbox/tests/local_backend.py`: a bash process in a temporary directory, no isolation) that lives in the test suite alone: no setting selects it, and the runner cannot be configured to use it.

## Isolation

- **Runtime:** Kata Containers (`SANDBOX_RUNTIME=kata`).
  - Every container is its own KVM microVM with its own kernel. Cloud Hypervisor is Kata's default hypervisor; Firecracker is the alternative.
  - The host needs `/dev/kvm` and the Kata runtime installed ([deployment.md](./deployment.md)).
  - `runc` works for local development and gives no VM boundary.
  - In production the runner accepts only a runtime named in `SANDBOX_VM_RUNTIMES` (the Kata names, by default) and refuses to start otherwise, because any other OCI runtime would be a shared kernel under a VM's name.
- **Network:** none by default (`SANDBOX_NETWORK=none`), and the chat agent's prompt says so.
  - With a network, the one way out is the egress proxy, over HTTPS, to the sites the operator lists (§ Network).
  - The chat agent's web tools — `web_search`, `search_papers`, `read_url`, `download_file` — are not a route out of the VM either: they run on the worker, on the worker's keys, and only their results enter the conversation ([chat.md](./chat.md) § Tools).
- **No secrets in the VM.** The upstream key, the database and the API are unreachable from inside. The agent's model calls are made by the worker on the host, never from the VM.
- **Resource limits:** `SANDBOX_CPUS`, `SANDBOX_MEMORY`, `SANDBOX_PIDS`, `SANDBOX_TMP_SIZE` (the VM's writable `/tmp`, a tmpfs of 1g by default, counted against the memory limit), per-command timeout, output cap.
  - Shells per user are capped by the runner at `SANDBOX_MAX_SHELLS` (8). A conversation holds one shell.
  - When a new conversation needs one past the cap, the shell that has been idle longest is killed to make room. It is killed inside the VM first, like `restart`, so its supervisor does not linger as a live shell the sweep preserves until the pid limit is reached. Its conversation gets a fresh shell on its next command.
  - A shell running a command is never evicted. If every one is busy, the command is refused.
- **The document is in the VM; a parsed document's original is not.**
  - A prompt injection in a parsed PDF can at worst make the agent act on that user's own files, which revisions undo. It cannot reach another user's directory or the service.
  - The original upload — the one file no revision can bring back — lives beside the git directory, outside the mount. The `source.*` in the document directory is a copy the host makes again when it is missing ([workspace.md](./workspace.md) § Layout).
  - The parse baseline under `.sightread/results/` is in the VM read-only, a nested mount the shell cannot write through.
- **A file of the library is in the VM whole** ([workspace.md](./workspace.md) § Files).
  - It is kept once, in the tree, where the shell reads and rewrites it — that is what it is there for — so a command, or an injection that reaches one, can change or delete the reader's files.
  - A binary or large one has no revision to put it back: the daily backup is what does ([workspace.md](./workspace.md) § Backups).
  - That is the trade the one-copy rule makes for a tree the agent can work in, taken knowingly.

## Network

The operator lists the sites the sandbox may reach, on the console's Settings page. By default that is PyPI alone, so `uv pip install` works and nothing else does.

- **One way out.** `SANDBOX_NETWORK` names an internal Docker network on which the host has no IPv4 address: the compose files' `ema-paper-sandbox`, on the subnet `SANDBOX_SUBNET` (`10.250.250.0/24`).
  - The network is `internal: true`, so Docker routes nothing off it.
  - It has `com.docker.network.bridge.inhibit_ipv4`, so the host's bridge does not take the network's gateway address, where a VM would otherwise reach whatever the host listens on.
  - The VMs are started on it, their addresses from `SANDBOX_VM_RANGE` (`10.250.250.128/25`).
  - `sandbox-egress`, the egress proxy, is on it at the fixed address `SANDBOX_EGRESS_ADDRESS` (`10.250.250.2`), outside that range, so a VM started while the proxy restarts cannot take its address.
  - A VM is given the proxy's address, never a name, because Docker's DNS does not answer inside a Kata VM.
  - The runner sets `HTTPS_PROXY` and `HTTP_PROXY`, in both spellings, to `SANDBOX_PROXY_URL` in every VM; uv, pip, git, curl and Python's HTTP clients read them. A process that ignores them reaches nothing past the network.
  - The runner refuses to start on a network Docker does not report both internal and without the host's address, or without `SANDBOX_PROXY_URL` beside one. So a network named by mistake (`bridge`, or one made by hand without the option) cannot hand the VMs the internet or the host's ports.
  - The proxy holds no secret and no setting of the database. It is on no network but this one and its own way out (`egress`), never the compose network the database, the API and the runner share.
- **No IPv6 on the network.** The bridge keeps an IPv6 link-local address whatever `inhibit_ipv4` says, and a VM that holds one too reaches the host there, on whatever listens on `[::]` and the host's firewall lets through. Two layers close it:
  - **The VM's own setting.** The runner starts every VM on the network with IPv6 off (`net.ipv6.conf.all.disable_ipv6` and `net.ipv6.conf.default.disable_ipv6`). Kata applies both inside the guest, and root in the VM cannot set them back, `/proc/sys` being read-only there and the capability dropped.
    - After each start the runner checks that the VM holds no IPv6 address (`/proc/net/if_inet6` empty). A VM that does is removed the way `stop` removes one, and marked unstoppable when the removal fails (§ The runner), so the next command never takes it as started; the command fails.
    - A VM whose `docker run` timed out but came up later is checked, and given the default environment (§ The image), when a command first finds it running.
    - A VM a restarted runner adopts that holds one is stopped instead of adopted.
    - The loopback loses IPv6 too: in such a VM `localhost` is `127.0.0.1` alone, and a program that binds `::1` alone fails.
  - **The bridge.** The host turns IPv6 off on the bridge itself (§ Operations), so a VM kernel that ignored the setting would still find nothing there.
- **What it lets through.** An HTTP `CONNECT` to port 443 of a host on the list, and nothing else.
  - Another method — a plain `http://` request included — answers 405. Another port answers 403. A host not on the list answers 403.
  - These are decided before the name is looked up, so a refused name never reaches a resolver.
  - A listed name whose addresses are not all global — loopback, private, link-local, the compose networks' own — answers 403, the guard the worker's web fetches keep ([chat.md](./chat.md) § Tools), so no entry can point inside.
  - The proxy connects to the addresses it checked, each in the resolver's order, the next only when one cannot be reached.
  - Once the tunnel is open, the client's TLS hello must name the same host (SNI). Another name, none, or anything but TLS closes the tunnel before a byte reaches the site, since a CDN serves many sites at one address and would answer for any of them.
  - The rest passes through untouched: the proxy adds no certificate to the VM and sees nothing inside TLS. So a CDN that serves one site under another's name (domain fronting) is the CDN's to refuse.
- **Its limits.**
  - At most `SANDBOX_EGRESS_MAX_TUNNELS` (64) tunnels at once per VM, told apart by address; 429 past it.
  - The request, the connection and the TLS hello each within `SANDBOX_EGRESS_CONNECT_TIMEOUT_SECONDS` (10).
  - A tunnel quiet both ways for `SANDBOX_EGRESS_IDLE_SECONDS` (300) is closed.
  - Each tunnel is logged when it opens, and when it closes with the bytes it moved each way, with the VM's address and the host. Each refusal is logged with the VM's address and the reason, which names the host when the refusal is about the host. Never a byte of what passes is logged.
- **The list.** `SANDBOX_EGRESS_HOSTS` (`pypi.org,files.pythonhosted.org`), one of the settings the database may override ([admin.md](./admin.md) § Settings).
  - Host names separated by commas or whitespace (line breaks included), in any case.
  - `*.example.com` for every name under `example.com` (not `example.com` itself).
  - No scheme, port, path or address. An entry that is not a host name is refused in the check's words, naming it (`SANDBOX_EGRESS_HOSTS: https://github.com/astral-sh is not a host name; write a name such as github.com`).
  - Empty, it lets nothing through.
  - The worker writes the list in effect to `SANDBOX_EGRESS_HOSTS_FILE`, one entry a line, on a volume the proxy mounts read-only. It writes at its start and every `RUNTIME_SETTINGS_CACHE_SECONDS` after, reading the database's rows each time, and replaces the file whole when the list changed.
  - The proxy reads the file again whenever it changes. No file — a worker that has not written it yet — or one it cannot read lets nothing through.
- **What a listed site receives.** Whatever a command sends it: an injection in a document could have the agent send the user's files to any site on the list.
  - PyPI's two hosts serve downloads. Its uploads go to `upload.pypi.org`, which the default list leaves out.
  - A site that takes uploads, such as GitHub or Hugging Face, becomes a way out for the workspace's files.
  - The Settings page says so beside the list.
- **The VMs share the network.** Each can reach another's address on it.
  - Nothing listens in a VM between commands: the sweep after each command kills what it left running (§ The sweep).
  - A server a command runs is reachable from the other VMs while it runs, and the list does not cover that traffic: a command in one user's VM could send files to a server running in another's.
  - Taken knowingly: a network per VM would close it, at the cost of the runner making, connecting and removing one for every VM it starts.
- **The prompt follows the list.** For each turn the worker reads `SANDBOX_NETWORK` and the list in effect ([chat.md](./chat.md) § System prompt).
  - With `none` or an empty list, the prompt says nothing reaches out.
  - Otherwise it names the sites and says anything else fails, and that `uv pip install` works when PyPI's two hosts are on the list.
- `SANDBOX_INDEX_URL`, when set, is exported to the VM as `UV_INDEX_URL` on `docker run`: an index other than PyPI, reached through the proxy too, so its host must be on the list.

## Lifecycle

- **Start on first use.** The first `exec` of a user whose VM is stopped boots it (Kata boots in about a second).
- **Stop when idle.** `SANDBOX_IDLE_SECONDS` after the last command the runner stops the VM.
  - A VM with a command still running is never idle, whatever the clock says.
  - Idleness is decided twice: once to pick candidates and again under the user's lock, so a command that started in between keeps its VM.
  - Stopping deletes nothing in the workspace. The runner deletes no file but the `.cache` a `reset` removes (below).
- **Deleting the account stops the VM.** The API stops the user's VM through `stop` before it deletes the account, then removes the user directory the VM mounted ([auth.md](./auth.md) § Deleting the account).
  - The stop is best effort: a failure is logged and the deletion goes on.
  - The runner deletes none of the account's files: they go by the API's hand, through `workspace/`.
- **A restarted runner adopts what is running.** At start the docker backend lists the running `ema-paper-sandbox-*` containers Docker still has and takes them into its table as running and last used now, so the idle sweep stops them on schedule.
  - A runner that cannot list them does not start, since a VM it did not adopt would take commands beside whatever the previous runner's shell left running in it.
  - Their shells are gone with the old process, so before a VM is adopted everything running in it but pid 1 is killed: a command the old runner's shell was running must not go on writing after the worker finishes its turn.
  - A VM where that kill cannot be confirmed is stopped instead, and marked unstoppable if that fails too.
  - A conversation opens a new shell on its next command.
  - `restart` and the stop paths take the user's sandbox lock, so a restart never changes the shell table under a stop that is walking it.
- **Reset is the user's.** The reset in the settings dialog's Usage section deletes `/workspace/.cache` (uv's cache, the default environment and whatever else was installed) and shell state. Documents, notes and history stay. The next start makes the default environment again, empty (§ The image).

## The image

`deploy/sandbox-image/Dockerfile` holds:

- Debian with `uv` and Python.
- `poppler-utils`, `ripgrep`, `jq`, `pandoc`.
- `imagemagick` 6, its policy's edge limit raised from Debian's 16K pixels to 64K, so a full-page screenshot can be cut into the pieces the editor's `view` names ([chat.md](./chat.md) § Tools). The area, memory and disk limits stay Debian's 128 MP, 256 MiB and 1 GiB.
- `git`.
- `procps` and `util-linux`: the `pgrep` and `setsid` the shell relies on.
- `/workspace` as the working directory.
- An environment that keeps uv's state in the workspace's `.cache`, all of which survives a stop and goes with a reset:
  - `UV_CACHE_DIR=/workspace/.cache/uv`
  - `UV_PYTHON_INSTALL_DIR=/workspace/.cache/python` (the Pythons `uv python install` fetches)
  - `HOME=/workspace/.cache/home`

The runner passes `UV_CACHE_DIR` and `HOME` again on `docker run`. `UV_INDEX_URL` is not the image's; the runner sets it (§ Network).

- **uv is the Python tooling.** The image names a default environment, `VIRTUAL_ENV=/workspace/.cache/venv`, first on its `PATH`.
  - `python` is that environment's, and `uv pip install` installs into it.
  - `pip` (and `pip3`) is a short wrapper that runs `uv pip`, answering `--version` itself since uv's pip has none, so a reflexive `pip install` goes through uv too.
  - The system Python has no pip, and its root filesystem is read-only anyway.
  - The environment lives in the workspace's `.cache`, so what a command installs is there on the next boot and goes with a reset.
  - The runner makes it after every start when its interpreter is missing — a new workspace, a reset, an image whose Python moved on and left the link dangling — with `uv venv --allow-existing`, which keeps the packages already there.
  - The check only looks (`-x`) and runs nothing the VM wrote.
  - It reads the path from the image's own `VIRTUAL_ENV`, so an image without one gets nothing.
  - A failure is logged and the VM used as it is, since the shell works without the environment.
  - uv links an installed package's files from its cache into the environment, one file under two names, which the quota counts once ([workspace.md](./workspace.md) § Quota).
- **Fonts for every script.** `fonts-noto-core`, `fonts-noto-cjk` and `fonts-noto-color-emoji` with `fontconfig` (about 145 MB installed), so a figure, an image or a page a command renders draws Chinese, Japanese, Korean and the other scripts instead of empty boxes.
  - matplotlib does not use fontconfig and falls back to the DejaVu Sans it ships, which has no CJK.
  - `MATPLOTLIBRC=/etc/matplotlibrc` points it at an rc that lists Noto Sans CJK TC first, then Noto Sans and DejaVu Sans, each glyph drawn from the first family that has it.
  - A `matplotlibrc` in the working directory still wins, as matplotlib's own order has it.

The chat agent's prompt names both, uv as the Python tooling and the fonts for every script ([chat.md](./chat.md) § System prompt).

The image's pid 1 is a few lines of Python that do nothing but reap. What a command left running is killed after the command and reparented to pid 1, and under a `sleep infinity` it would stay a zombie against the VM's pid limit. The runner passes the same program on `docker run`, so the image's `CMD` and the runner cannot drift.

Commands run as root **inside the VM**. `/workspace` is a bind mount the api and worker write as root, and one identity on both sides is what keeps the files writable from the shell. The isolation is the VM boundary, with every capability dropped and a read-only root filesystem, not a guest UID.

The image is built and tagged by the operator like the service images. `SANDBOX_IMAGE` names it (`ema-paper-sandbox`), and compose names the runner's own image `ema-paper-sandbox-runner` so the two never share a tag.

## Operations

- **The image.** Build it with `docker build -t ema-paper-sandbox deploy/sandbox-image`, the name `SANDBOX_IMAGE` holds, and build it again when `deploy/sandbox-image/` changes (§ The image). A running VM keeps its image until it stops.
- **The Kata runtime.** Install it and register it with Docker as `kata`. The runtime binary, the guest kernel and the rootfs image are Kata's packages; Docker's `daemon.json` gets `"runtimes": {"kata": {"path": "/usr/bin/containerd-shim-kata-v2"...}}` per Kata's Docker guide. Verify with `docker run --runtime kata --rm hello-world` before enabling the sandbox.
- **The workspaces directory.** `WORKSPACES_HOST_DIR` is the host directory bind-mounted at `{WORKSPACES_DIR}` into `api`, `worker` and `sandbox`. The runner holds it twice:
  - `SANDBOX_WORKSPACES_DIR` is the host path it hands to `docker run -v`.
    - It is absolute, as the daemon resolves nothing relative to the compose project; the runner refuses to start with a relative one.
    - Compose sets it to `WORKSPACES_HOST_DIR`, the same host path api and worker mount, whatever `.env` says of `SANDBOX_WORKSPACES_DIR`.
    - Locally, with `WORKSPACES_HOST_DIR` empty as `.env.example` leaves it, compose sets it to `${PWD}/workspaces`, which is the directory compose mounts only when it runs from the checkout's root. From anywhere else, set an absolute `WORKSPACES_HOST_DIR`.
    - `SANDBOX_START_TIMEOUT_SECONDS` (180) is how long that `docker run` may take. api and worker allow it on top of a command's timeout, so a cold start never looks like a lost command.
  - `SANDBOX_WORKSPACES_MOUNT` is where compose mounted it inside the runner, which is what the runner reads and clears itself (the `.cache` it creates and `reset` removes).
- **Quota.** A per-user quota is a filesystem concern — XFS project quotas or one fixed-size image per user mounted under the directory — see [workspace.md](./workspace.md) § Quota.
- **Turning the network on** (§ Network):
  1. Start `sandbox-egress`. Its first start makes the network, which the runner refuses to start without.
  2. Turn IPv6 off on the network's bridge, named `br-` and the first twelve characters of `docker network inspect -f '{{.Id}}' ema-paper-sandbox`: the line `net.ipv6.conf.<bridge>.disable_ipv6=1` in `/etc/sysctl.d/90-ema-paper-sandbox.conf`, applied with `sudo sysctl -p` on that file. On a systemd host, udev applies it again whenever the bridge appears, so it holds across reboots. After that, `ip addr show dev <bridge>` lists no address at all.
  3. Set `SANDBOX_NETWORK=ema-paper-sandbox` in `.env`, then recreate `sandbox`, `worker` and `api`: the runner starts the VMs on it, the worker tells the prompt, the API the Settings page.
  - A VM already running keeps no network until it stops; the next start puts it on the network.
  - `SANDBOX_SUBNET` must not overlap another network of the host — compose refuses to create it otherwise.
  - `SANDBOX_VM_RANGE` and `SANDBOX_EGRESS_ADDRESS` must be inside `SANDBOX_SUBNET`, the address outside the range.
  - The bridge's name follows the network: a network made again (`docker compose down` removes it) has a new bridge, and the sysctl line must name the new one. Until it does, the VMs' own setting is the one layer left.
  - Back to `none` turns the VMs' network off at their next start, whatever the list holds.
- **Logs.** Every command is recorded in the conversation (`chat_events`), never only in the runner's log. The proxy's tunnels and refusals are in `ema-paper-sandbox-egress`'s.
