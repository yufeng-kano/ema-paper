"""The runner over the local backend: a bash process, no Docker, no network."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from sightread_sandbox.backends import LocalBackend, adopted_user_ids
from sightread_sandbox.config import Settings
from sightread_sandbox.main import create_app

TOKEN = "runner-token-not-a-real-one"


@pytest.fixture
async def client(tmp_path):
    settings = Settings(
        app_env="local",
        sandbox_token=TOKEN,
        sandbox_backend="local",
        sandbox_workspaces_dir=str(tmp_path / "ws"),
        sandbox_workspaces_mount=str(tmp_path / "ws"),
        sandbox_output_max_bytes=400,
        sandbox_idle_seconds=0,
    )
    app = create_app(settings, LocalBackend(settings))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://runner") as http:
        http.headers["Authorization"] = f"Bearer {TOKEN}"
        yield http


async def test_the_token_is_required(client) -> None:
    anonymous = await client.get("/sandboxes/1", headers={"Authorization": ""})
    assert anonymous.status_code == 401


async def test_a_session_keeps_its_working_directory_and_environment(client, tmp_path) -> None:
    first = await client.post(
        "/sandboxes/1/exec",
        json={"session": "s1", "command": "mkdir -p files/x && cd files/x && export A=1 && pwd"},
    )
    assert first.status_code == 200, first.text
    assert first.json()["output"].endswith("/ws/1/files/x")
    assert first.json()["exit_code"] == 0

    second = await client.post(
        "/sandboxes/1/exec", json={"session": "s1", "command": "echo $A; basename $(pwd)"}
    )
    assert second.json()["output"] == "1\nx"
    status = (await client.get("/sandboxes/1")).json()
    assert status["state"] == "running" and status["shells"] == 1

    # Another session is another shell, starting at the workspace root.
    other = await client.post(
        "/sandboxes/1/exec", json={"session": "s2", "command": "echo $A; basename $(pwd)"}
    )
    assert other.json()["output"] == "\n1"
    assert (await client.get("/sandboxes/1")).json()["shells"] == 2

    failed = await client.post("/sandboxes/1/exec", json={"session": "s1", "command": "false"})
    assert failed.json()["exit_code"] == 1


async def test_a_timeout_kills_the_command_and_the_next_one_gets_a_fresh_shell(client) -> None:
    slow = await client.post(
        "/sandboxes/2/exec",
        json={"session": "s", "command": "cd files 2>/dev/null; sleep 30", "timeout_seconds": 1},
    )
    assert slow.json()["timed_out"] is True
    after = await client.post("/sandboxes/2/exec", json={"session": "s", "command": "echo back"})
    assert after.json() == {
        "output": "back",
        "exit_code": 0,
        "timed_out": False,
        "truncated": False,
    }


async def test_output_is_capped_head_and_tail(client) -> None:
    flood = await client.post("/sandboxes/3/exec", json={"session": "s", "command": "seq 1 5000"})
    body = flood.json()
    assert body["truncated"] is True
    assert body["output"].startswith("1\n")
    assert body["output"].rstrip().endswith("5000")
    assert "omitted by the sandbox" in body["output"]


async def test_restart_stop_and_reset(client, tmp_path) -> None:
    await client.post("/sandboxes/4/exec", json={"session": "s", "command": "export B=2"})
    restarted = await client.post("/sandboxes/4/restart", json={"session": "s"})
    assert restarted.status_code == 200
    gone = await client.post("/sandboxes/4/exec", json={"session": "s", "command": "echo [$B]"})
    assert gone.json()["output"] == "[]"

    cache = tmp_path / "ws" / "4" / ".cache" / "uv"
    cache.mkdir(parents=True)
    (cache / "wheel").write_text("x")
    (tmp_path / "ws" / "4" / "files").mkdir(exist_ok=True)
    (tmp_path / "ws" / "4" / "files" / "keep.md").write_text("keep")
    stopped = await client.post("/sandboxes/4/stop")
    assert stopped.status_code == 204
    assert (await client.get("/sandboxes/4")).json()["state"] == "stopped"
    reset = await client.post("/sandboxes/4/reset")
    assert reset.status_code == 204
    assert not cache.exists()
    assert (tmp_path / "ws" / "4" / "files" / "keep.md").read_text() == "keep"


async def test_a_newline_free_flood_is_cut_like_any_other(client) -> None:
    flood = await client.post(
        "/sandboxes/5/exec",
        json={
            "session": "s",
            "command": "head -c 200000 /dev/zero | tr '\\0' 'a'; echo; echo done",
        },
    )
    assert flood.status_code == 200, flood.text
    body = flood.json()
    assert body["truncated"] is True
    assert body["exit_code"] == 0
    assert body["output"].rstrip().endswith("done")
    assert len(body["output"]) < 2000


async def test_the_shell_cap_closes_the_longest_idle_shell(tmp_path) -> None:
    settings = Settings(
        app_env="local",
        sandbox_token=TOKEN,
        sandbox_backend="local",
        sandbox_workspaces_dir=str(tmp_path / "ws"),
        sandbox_workspaces_mount=str(tmp_path / "ws"),
        sandbox_max_shells=2,
    )
    app = create_app(settings, LocalBackend(settings))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://runner") as http:
        http.headers["Authorization"] = f"Bearer {TOKEN}"
        await http.post("/sandboxes/6/exec", json={"session": "a", "command": "export A=1"})
        await http.post("/sandboxes/6/exec", json={"session": "b", "command": "export B=1"})
        assert (await http.get("/sandboxes/6")).json()["shells"] == 2
        third = await http.post("/sandboxes/6/exec", json={"session": "c", "command": "echo c"})
        assert third.status_code == 200, third.text
        assert (await http.get("/sandboxes/6")).json()["shells"] == 2
        # `a` was idle longest: its state is gone, `b` kept its own.
        kept = await http.post("/sandboxes/6/exec", json={"session": "b", "command": "echo [$B]"})
        assert kept.json()["output"] == "[1]"
        assert (await http.get("/sandboxes/6")).json()["shells"] == 2
        fresh = await http.post("/sandboxes/6/exec", json={"session": "a", "command": "echo [$A]"})
        assert fresh.json()["output"] == "[]"


def test_running_sandboxes_are_recognised_by_name() -> None:
    listing = "sightread-sandbox-12\nsightread-sandbox-runner\nother\nsightread-sandbox-7\n"
    assert adopted_user_ids(listing) == [12, 7]


async def test_the_idle_sweep_spares_a_running_command(tmp_path) -> None:
    import asyncio

    settings = Settings(
        app_env="local",
        sandbox_token=TOKEN,
        sandbox_backend="local",
        sandbox_workspaces_dir=str(tmp_path / "ws"),
        sandbox_workspaces_mount=str(tmp_path / "ws"),
        sandbox_idle_seconds=0,
    )
    backend = LocalBackend(settings)
    running = asyncio.create_task(backend.exec(8, "s", "sleep 1; echo done", timeout=10))
    await asyncio.sleep(0.2)
    assert await backend.stop_idle() == 0
    outcome = await running
    assert outcome.output == "done"
    assert await backend.stop_idle() == 1


async def test_an_underscore_flood_is_bounded_too(client) -> None:
    flood = await client.post(
        "/sandboxes/9/exec",
        json={"session": "s", "command": "head -c 300000 /dev/zero | tr '\\0' '_'; echo; echo end"},
    )
    assert flood.status_code == 200, flood.text
    body = flood.json()
    assert body["truncated"] is True
    assert body["output"].rstrip().endswith("end")
    assert len(body["output"]) < 2000


async def test_a_timeout_runs_the_backends_kill_before_closing_the_shell() -> None:
    from sightread_sandbox.shell import GROUP_CLEANUP, ShellSession, supervisor

    killed = []

    async def kill_inside(*, pid: int, locked: bool = False) -> None:
        killed.append(pid == shell.pid)

    shell = ShellSession(
        ["sh", "-c", supervisor(GROUP_CLEANUP)], max_bytes=1000, on_timeout=kill_inside
    )
    outcome = await shell.run("sleep 30", timeout=0.2)
    assert outcome.timed_out is True
    assert killed == [True]
    assert not shell.alive


def test_production_accepts_only_a_vm_runtime(tmp_path) -> None:
    base = {
        "app_env": "production",
        "sandbox_token": TOKEN,
        "sandbox_backend": "docker",
        "sandbox_workspaces_dir": str(tmp_path / "ws"),
        "sandbox_workspaces_mount": str(tmp_path / "ws"),
    }
    assert Settings(**base, sandbox_runtime="kata").sandbox_runtime == "kata"
    for runtime in ("runc", "crun", "kata2"):
        with pytest.raises(ValueError):
            Settings(**base, sandbox_runtime=runtime)
    # The operator names their own VM runtime.
    assert Settings(**base, sandbox_runtime="kata2", sandbox_vm_runtimes="kata2").sandbox_runtime


async def test_a_command_returns_only_once_its_background_jobs_are_done(client, tmp_path) -> None:
    done = await client.post(
        "/sandboxes/3/exec",
        json={
            "session": "s",
            "command": "(sleep 0.3; echo late > late.txt) &",
            "timeout_seconds": 5,
        },
    )
    assert done.json()["exit_code"] == 0
    # The job the command left behind finished before the command returned.
    assert (tmp_path / "ws" / "3" / "late.txt").read_text().strip() == "late"


async def test_nothing_a_command_started_outlives_it(client) -> None:
    import time

    started = time.monotonic()
    disowned = await client.post(
        "/sandboxes/10/exec",
        json={
            "session": "s",
            "command": "nohup sleep 30 >/dev/null 2>&1 & disown; echo $!",
            "timeout_seconds": 10,
        },
    )
    assert disowned.status_code == 200, disowned.text
    assert time.monotonic() - started < 5  # not waited for: killed
    pid = int(disowned.json()["output"].strip())
    after = await client.post(
        "/sandboxes/10/exec",
        json={
            "session": "s",
            # Dead, or killed and not yet reaped by this host's init: a zombie is not
            # alive (in the VM, pid 1 reaps; here, the test host's init may not).
            "command": f"grep -q 'State:.Z' /proc/{pid}/status 2>/dev/null && echo dead;"
            f" [ -d /proc/{pid} ] && ! grep -q 'State:.Z' /proc/{pid}/status && echo alive;"
            f" [ -d /proc/{pid} ] || echo dead",
        },
    )
    assert after.json()["output"] == "dead"


async def test_a_vm_that_cannot_be_removed_refuses_commands_and_keeps_its_cache(tmp_path) -> None:
    from sightread_sandbox.backends import CACHE_DIR, UserSandbox

    class Stuck(LocalBackend):
        gone = False

        async def stop_vm(self, sandbox: UserSandbox) -> bool:
            return False  # `docker rm -f` failed or timed out

        async def is_gone(self, user_id: int) -> bool:
            return self.gone

    settings = Settings(
        app_env="local",
        sandbox_token=TOKEN,
        sandbox_backend="local",
        sandbox_workspaces_dir=str(tmp_path / "ws"),
        sandbox_workspaces_mount=str(tmp_path / "ws"),
        sandbox_idle_seconds=0,
    )
    backend = Stuck(settings)
    app = create_app(settings, backend)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://runner") as http:
        http.headers["Authorization"] = f"Bearer {TOKEN}"
        assert (
            await http.post("/sandboxes/11/exec", json={"session": "s", "command": "true"})
        ).status_code == 200
        cache = tmp_path / "ws" / "11" / CACHE_DIR
        assert cache.is_dir()
        assert (await http.post("/sandboxes/11/stop")).status_code == 503
        # No command runs in a VM that may still be alive, and no reset deletes under it.
        refused = await http.post("/sandboxes/11/exec", json={"session": "s", "command": "true"})
        assert refused.status_code == 503
        assert (await http.post("/sandboxes/11/reset")).status_code == 503
        assert cache.is_dir()
        # The idle sweep neither counts it nor dies on it.
        assert await backend.stop_idle() == 0
        # Once Docker positively reports it gone, commands run again.
        backend.gone = True
        again = await http.post("/sandboxes/11/exec", json={"session": "s", "command": "echo ok"})
        assert again.status_code == 200, again.text
        assert again.json()["output"] == "ok"


async def test_a_restart_ends_the_command_the_shell_is_running(tmp_path) -> None:
    import asyncio
    import time

    settings = Settings(
        app_env="local",
        sandbox_token=TOKEN,
        sandbox_backend="local",
        sandbox_workspaces_dir=str(tmp_path / "ws"),
        sandbox_workspaces_mount=str(tmp_path / "ws"),
        sandbox_idle_seconds=0,
    )
    backend = LocalBackend(settings)
    running = asyncio.create_task(backend.exec(12, "s", "sleep 30; echo late", timeout=60))
    await asyncio.sleep(0.3)
    started = time.monotonic()
    await backend.restart(12, "s")
    outcome = await running
    assert time.monotonic() - started < 5
    assert outcome.exit_code is None and "late" not in outcome.output
    fresh = await backend.exec(12, "s", "echo fresh", timeout=10)
    assert fresh.output == "fresh"
    await backend.stop(12)


async def test_a_dropped_request_kills_its_command_where_it_runs() -> None:
    import asyncio

    from sightread_sandbox.shell import GROUP_CLEANUP, ShellSession, supervisor

    killed = []

    async def kill_inside(*, pid: int, locked: bool = False) -> None:
        killed.append(True)

    shell = ShellSession(
        ["sh", "-c", supervisor(GROUP_CLEANUP)], max_bytes=1000, on_timeout=kill_inside
    )
    running = asyncio.create_task(shell.run("sleep 30", timeout=60))
    await asyncio.sleep(0.3)
    running.cancel()
    with pytest.raises(asyncio.CancelledError):
        await running
    assert killed == [True]
    assert not shell.alive


async def test_reset_removes_whatever_sits_at_the_cache(client, tmp_path) -> None:
    from sightread_sandbox.backends import CACHE_DIR

    assert (
        await client.post("/sandboxes/13/exec", json={"session": "s", "command": "true"})
    ).status_code == 200
    assert (await client.post("/sandboxes/13/stop")).status_code == 204
    cache = tmp_path / "ws" / "13" / CACHE_DIR
    import shutil

    shutil.rmtree(cache)
    cache.write_text("not a directory")  # what a command left in its place
    assert (await client.post("/sandboxes/13/reset")).status_code == 204
    assert not cache.exists()
    again = await client.post("/sandboxes/13/exec", json={"session": "s", "command": "echo ok"})
    assert again.status_code == 200, again.text
    assert again.json()["output"] == "ok"
    assert cache.is_dir()


def test_the_vm_sweep_scripts_are_valid_sh_and_the_docker_backend_uses_them(tmp_path) -> None:
    import subprocess

    from sightread_sandbox.backends import ADOPT_SWEEP, KILL_TREE, VM_SWEEP, DockerBackend
    from sightread_sandbox.shell import GROUP_CLEANUP

    for script in (VM_SWEEP, KILL_TREE, ADOPT_SWEEP, GROUP_CLEANUP):
        subprocess.run(["sh", "-n"], input=script, text=True, check=True)  # noqa: S603, S607
    settings = Settings(
        app_env="local",
        sandbox_token=TOKEN,
        sandbox_backend="docker",
        sandbox_runtime="runc",
        sandbox_workspaces_dir=str(tmp_path / "ws"),
        sandbox_workspaces_mount=str(tmp_path / "ws"),
    )
    assert DockerBackend(settings).cleanup() == VM_SWEEP
    assert LocalBackend(settings).cleanup() == GROUP_CLEANUP
    # The timeout kill and the adoption both end with the sweep.
    assert VM_SWEEP.strip() in KILL_TREE and VM_SWEEP.strip() in ADOPT_SWEEP


async def test_a_command_cannot_forge_its_own_status_line(client, tmp_path) -> None:
    import time

    started = time.monotonic()
    # A command that reads what follows it on stdin and echoes the marker early would
    # end the framing with work still running: its stdin is /dev/null and the marker
    # is the supervisor's alone, so it runs to the end and reports its own status.
    forged = await client.post(
        "/sandboxes/14/exec",
        json={
            "session": "s",
            "command": "read -r m; printf '\\n%s 0\\n' \"$m\"; sleep 1; "
            "echo late > late.txt; exit 3",
            "timeout_seconds": 10,
        },
    )
    assert forged.status_code == 200, forged.text
    assert time.monotonic() - started >= 1
    assert forged.json()["exit_code"] == 3
    assert (tmp_path / "ws" / "14" / "late.txt").read_text() == "late\n"
    # `exit` ended the command, not the session: the next command runs in the same one.
    after = await client.post(
        "/sandboxes/14/exec", json={"session": "s", "command": "cd /; echo $PWD; export Q=1"}
    )
    assert after.json()["output"] == "/" and after.json()["exit_code"] == 0
    kept = await client.post("/sandboxes/14/exec", json={"session": "s", "command": "echo $PWD $Q"})
    assert kept.json()["output"] == "/ 1"
    assert (await client.get("/sandboxes/14")).json()["shells"] == 1


def test_the_reaper_is_the_images_cmd() -> None:
    from sightread_sandbox.backends import REAPER, REAPER_PYTHON

    compile(REAPER, "reaper", "exec")
    dockerfile = Path(__file__).resolve().parents[3] / "deploy" / "sandbox-image" / "Dockerfile"
    # The interpreter by its full path too: the VM's PATH starts in the workspace.
    assert f"CMD {json.dumps([REAPER_PYTHON, '-c', REAPER])}" in dockerfile.read_text()


async def test_a_restart_whose_in_vm_kill_fails_does_not_deadlock(tmp_path) -> None:
    import asyncio

    from sightread_sandbox.backends import UserSandbox

    calls: list[bool] = []

    class Stubborn(LocalBackend):
        def on_timeout(self, user_id):
            async def kill_inside(*, pid: int, locked: bool = False) -> None:
                calls.append(locked)
                sandbox = self._user(user_id)
                # The fallback stop, under the lock the caller already holds.
                async with sandbox.lock if not locked else _nothing():
                    await self._stop_locked(sandbox)

            return kill_inside

        async def _stop_locked(self, sandbox: UserSandbox) -> None:
            sandbox.sessions.clear()
            sandbox.session_used.clear()
            self._running.discard(sandbox.user_id)

    import contextlib

    _nothing = contextlib.nullcontext
    settings = Settings(
        app_env="local",
        sandbox_token=TOKEN,
        sandbox_backend="local",
        sandbox_workspaces_dir=str(tmp_path / "ws"),
        sandbox_workspaces_mount=str(tmp_path / "ws"),
        sandbox_idle_seconds=0,
    )
    backend = Stubborn(settings)
    await backend.exec(15, "s", "true", timeout=10)
    await asyncio.wait_for(backend.restart(15, "s"), 5)
    assert calls == [True]
    assert not backend._user(15).lock.locked()


async def test_a_supervisor_killed_by_its_command_is_swept(tmp_path) -> None:
    from sightread_sandbox.shell import ShellSession, supervisor

    swept: list[bool] = []

    async def kill_inside(*, pid: int, locked: bool = False) -> None:
        swept.append(pid == reported)

    shell = ShellSession(
        ["sh", "-c", supervisor(":")], cwd=str(tmp_path), max_bytes=1000, on_timeout=kill_inside
    )
    await shell.start()
    reported = shell.pid
    outcome = await shell.run("kill -KILL $PPID; sleep 1", timeout=10)
    assert outcome.exit_code is None and not outcome.timed_out
    assert swept == [True]
    assert not shell.alive


async def test_an_evicted_shell_is_killed_inside_the_vm(tmp_path) -> None:
    calls: list[bool] = []

    class Recording(LocalBackend):
        def on_timeout(self, user_id):
            async def kill_inside(*, pid: int, locked: bool = False) -> None:
                calls.append(locked)

            return kill_inside

    settings = Settings(
        app_env="local",
        sandbox_token=TOKEN,
        sandbox_backend="local",
        sandbox_workspaces_dir=str(tmp_path / "ws"),
        sandbox_workspaces_mount=str(tmp_path / "ws"),
        sandbox_max_shells=1,
    )
    backend = Recording(settings)
    await backend.exec(16, "a", "true", timeout=10)
    await backend.exec(16, "b", "true", timeout=10)
    # `a` made room for `b`: killed where it runs, under the lock exec holds.
    assert calls == [True]
    await backend.stop(16)


async def test_the_supervisor_reports_its_pid_before_any_command(tmp_path) -> None:
    from sightread_sandbox.shell import ShellSession, supervisor

    # The cleanup sees the keep line the runner sent with the command, never a file.
    shell = ShellSession(
        ["sh", "-c", supervisor('printf "KEEP=[%s]\\n" "$__sr_keep_in"')],
        cwd=str(tmp_path),
        max_bytes=1000,
    )
    await shell.start()
    assert shell.pid is not None
    outcome = await shell.run("echo $PPID", timeout=10, keep=[shell.pid, 4242])
    # The command's parent is the supervisor, whose pid is the one it reported.
    assert outcome.output.splitlines()[0] == str(shell.pid)
    assert f"KEEP=[{shell.pid} 4242]" in outcome.output
    assert outcome.exit_code == 0
    # No pid line, no shell: a plain bash is not a supervisor.
    plain = ShellSession(["bash", "--norc", "--noprofile"], max_bytes=1000, start_timeout=1)
    with pytest.raises(RuntimeError):
        await plain.start()
    assert not plain.alive and plain.pid is None
    await shell.close()


async def test_the_runner_names_the_other_live_shells_to_each_cleanup(tmp_path) -> None:
    from sightread_sandbox.shell import supervisor

    class Recording(LocalBackend):
        def shell_argv(self, user_id):
            return ["sh", "-c", supervisor('printf "KEEP=[%s]\\n" "$__sr_keep_in"')], str(
                self.workspace(user_id)
            )

    settings = Settings(
        app_env="local",
        sandbox_token=TOKEN,
        sandbox_backend="local",
        sandbox_workspaces_dir=str(tmp_path / "ws"),
        sandbox_workspaces_mount=str(tmp_path / "ws"),
    )
    backend = Recording(settings)
    first = await backend.exec(21, "a", "true", timeout=10)
    pid_a = backend._user(21).sessions["a"].pid
    assert f"KEEP=[{pid_a}]" in first.output
    second = await backend.exec(21, "b", "true", timeout=10)
    pid_b = backend._user(21).sessions["b"].pid
    assert pid_a != pid_b
    kept = second.output.split("KEEP=[")[1].split("]")[0].split()
    assert sorted(kept) == sorted([str(pid_a), str(pid_b)])
    # A shell whose supervisor died while idle keeps the pid it reported until it is
    # restarted or closed, and the VM may give that number to a command's process: the
    # cleanup is told to keep the live shells only (docs/sandbox.md § The runner).
    dead = backend._user(21).sessions["b"]
    process = dead._process
    os.killpg(process.pid, 9)
    await process.wait()
    assert not dead.alive and dead.pid == pid_b
    third = await backend.exec(21, "a", "true", timeout=10)
    assert third.output.split("KEEP=[")[1].split("]")[0].split() == [str(pid_a)]
    await backend.restart(21, "a")
    await backend.restart(21, "b")


def test_kill_tree_refuses_anything_but_a_pid() -> None:
    import subprocess

    from sightread_sandbox.backends import KILL_TREE

    for bad in ("", "abc", "shell.pid", "12 13"):
        done = subprocess.run(["sh", "-c", KILL_TREE, "sh", bad, ""], capture_output=True)  # noqa: S603, S607
        assert done.returncode == 3, bad


async def test_a_stop_kills_a_running_command_inside_the_vm_first(tmp_path) -> None:
    import asyncio

    calls: list[tuple[int, bool]] = []

    class Recording(LocalBackend):
        def on_timeout(self, user_id):
            async def kill_inside(*, pid: int, locked: bool = False) -> None:
                calls.append((pid, locked))

            return kill_inside

    settings = Settings(
        app_env="local",
        sandbox_token=TOKEN,
        sandbox_backend="local",
        sandbox_workspaces_dir=str(tmp_path / "ws"),
        sandbox_workspaces_mount=str(tmp_path / "ws"),
    )
    backend = Recording(settings)
    await backend.exec(31, "idle", "true", timeout=10)
    running = asyncio.create_task(backend.exec(31, "busy", "sleep 30", timeout=60))
    await asyncio.sleep(0.3)
    busy_pid = backend._user(31).sessions["busy"].pid
    assert busy_pid is not None
    await asyncio.wait_for(backend.stop(31), 10)
    # The busy shell was killed where it runs, by its pid; the idle one merely closed.
    assert calls == [(busy_pid, True)]
    outcome = await asyncio.wait_for(running, 10)
    assert outcome.exit_code is None and not outcome.timed_out
    assert backend._user(31).sessions == {}


async def test_a_runner_that_cannot_list_sandboxes_does_not_start(tmp_path, monkeypatch) -> None:
    from sightread_sandbox import backends

    async def daemon_away(*args, timeout=120.0):
        return 1, "Cannot connect to the Docker daemon"

    monkeypatch.setattr(backends, "_docker", daemon_away)
    settings = Settings(
        app_env="local",
        sandbox_token=TOKEN,
        sandbox_backend="docker",
        sandbox_runtime="runc",
        sandbox_workspaces_dir=str(tmp_path / "ws"),
        sandbox_workspaces_mount=str(tmp_path / "ws"),
    )
    with pytest.raises(RuntimeError):
        await backends.DockerBackend(settings).startup()


async def test_the_runner_starts_only_on_an_internal_network(tmp_path, monkeypatch) -> None:
    from sightread_sandbox import backends

    inspected = {"sightread-sandbox": "true true"}

    async def fake_docker(*args, timeout=120.0):
        if args[:2] == ("network", "inspect"):
            answer = inspected.get(args[-1])
            return (0, f"{answer}\n") if answer else (1, "not found")
        return 0, ""

    monkeypatch.setattr(backends, "_docker", fake_docker)

    def settings(network: str) -> Settings:
        return Settings(
            app_env="local",
            sandbox_token=TOKEN,
            sandbox_backend="docker",
            sandbox_runtime="runc",
            sandbox_network=network,
            sandbox_proxy_url="http://10.250.250.2:3128",
            sandbox_workspaces_dir=str(tmp_path / "ws"),
            sandbox_workspaces_mount=str(tmp_path / "ws"),
        )

    # A network Docker routes off — `bridge`, or the sandbox's made without `internal` —
    # would hand the VMs the internet past the proxy; one where the host has an address,
    # whatever the host listens on; one Docker cannot find, nothing.
    for answer, network in (
        ("false <no value>", "bridge"),
        ("false <no value>", "made-without-internal"),
        ("true <no value>", "host-has-an-address"),
        ("true false", "host-has-an-address-too"),
        (None, "missing"),
    ):
        if answer:
            inspected[network] = answer
        with pytest.raises(RuntimeError, match="not an internal Docker network"):
            await backends.DockerBackend(settings(network)).startup()
    await backends.DockerBackend(settings("sightread-sandbox")).startup()
    await backends.DockerBackend(settings("none")).startup()


def test_a_network_needs_the_proxy(tmp_path) -> None:
    with pytest.raises(ValueError, match="SANDBOX_PROXY_URL"):
        Settings(
            app_env="local",
            sandbox_token=TOKEN,
            sandbox_backend="docker",
            sandbox_runtime="runc",
            sandbox_network="sightread-sandbox",
            sandbox_workspaces_dir=str(tmp_path / "ws"),
            sandbox_workspaces_mount=str(tmp_path / "ws"),
        )


async def test_a_vm_on_the_network_is_pointed_at_the_proxy(tmp_path, monkeypatch) -> None:
    from sightread_sandbox import backends

    calls: list[tuple[str, ...]] = []

    async def fake_docker(*args, timeout=120.0):
        calls.append(args)
        if args[0] == "inspect":
            return 1, "Error: No such object"
        return 0, ""

    monkeypatch.setattr(backends, "_docker", fake_docker)
    for network, user_id in (("sightread-sandbox", 81), ("none", 82)):
        settings = Settings(
            app_env="local",
            sandbox_token=TOKEN,
            sandbox_backend="docker",
            sandbox_runtime="runc",
            sandbox_network=network,
            sandbox_proxy_url="http://10.250.250.2:3128",
            sandbox_workspaces_dir=str(tmp_path / "ws"),
            sandbox_workspaces_mount=str(tmp_path / "ws"),
        )
        backend = backends.DockerBackend(settings)
        await backend.ensure_running(backend._user(user_id))
    on, off = [args for args in calls if args[0] == "run"]
    assert on[on.index("--network") + 1] == "sightread-sandbox"
    # The default environment is made in the VM just started, the network's as any other.
    made = [args[1] for args in calls if args[0] == "exec" and args[-1] == backends.DEFAULT_ENV]
    assert made == ["sightread-sandbox-81", "sightread-sandbox-82"]
    # Both spellings of both: uv, pip, git, curl and Python's clients each read one.
    for name in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy"):
        assert f"{name}=http://10.250.250.2:3128" in on
    assert off[off.index("--network") + 1] == "none"
    assert not [arg for arg in off if "PROXY" in arg.upper()]
    # IPv6 off, and checked off, on the network alone: its bridge keeps a link-local
    # address the host answers on.
    assert "net.ipv6.conf.all.disable_ipv6=1" in on
    assert "net.ipv6.conf.default.disable_ipv6=1" in on
    assert not [arg for arg in off if "ipv6" in arg]
    checked = [args[1] for args in calls if args[0] == "exec" and args[-1] == backends.NO_IPV6]
    assert checked == ["sightread-sandbox-81"]


async def test_a_vm_on_the_network_that_keeps_ipv6_is_never_used(tmp_path, monkeypatch) -> None:
    from sightread_sandbox import backends

    calls: list[tuple[str, ...]] = []

    async def fake_docker(*args, timeout=120.0):
        calls.append(args)
        if args[0] == "inspect":
            return 1, "Error: No such object"
        if args[0] == "exec" and args[-1] == backends.NO_IPV6:
            return 1, ""
        return 0, ""

    monkeypatch.setattr(backends, "_docker", fake_docker)
    settings = Settings(
        app_env="local",
        sandbox_token=TOKEN,
        sandbox_backend="docker",
        sandbox_runtime="runc",
        sandbox_network="sightread-sandbox",
        sandbox_proxy_url="http://10.250.250.2:3128",
        sandbox_workspaces_dir=str(tmp_path / "ws"),
        sandbox_workspaces_mount=str(tmp_path / "ws"),
    )
    backend = backends.DockerBackend(settings)
    sandbox = backend._user(83)
    with pytest.raises(RuntimeError, match="could not be started"):
        await backend.ensure_running(sandbox)
    # Removed before anything ran in it, and not counted as started.
    assert calls[-1] == ("rm", "-f", "sightread-sandbox-83")
    assert not [args for args in calls if args[0] == "exec" and args[-1] == backends.DEFAULT_ENV]
    assert sandbox.started_at is None


async def test_an_adopted_vm_on_the_network_with_ipv6_is_stopped(tmp_path, monkeypatch) -> None:
    from sightread_sandbox import backends

    calls: list[tuple[str, ...]] = []

    async def fake_docker(*args, timeout=120.0):
        calls.append(args)
        if args[:2] == ("network", "inspect"):
            return 0, "true true\n"
        if args[0] == "ps":
            return 0, "sightread-sandbox-91\nsightread-sandbox-92\n"
        # A runner before the IPv6 rule started 91; 92 has none.
        if args[0] == "exec" and args[-1] == backends.NO_IPV6:
            return (1, "") if args[1] == "sightread-sandbox-91" else (0, "")
        return 0, ""

    monkeypatch.setattr(backends, "_docker", fake_docker)
    for network in ("sightread-sandbox", "none"):
        calls.clear()
        settings = Settings(
            app_env="local",
            sandbox_token=TOKEN,
            sandbox_backend="docker",
            sandbox_runtime="runc",
            sandbox_network=network,
            sandbox_proxy_url="http://10.250.250.2:3128",
            sandbox_workspaces_dir=str(tmp_path / "ws"),
            sandbox_workspaces_mount=str(tmp_path / "ws"),
        )
        await backends.DockerBackend(settings).startup()
        removed = [args[-1] for args in calls if args[0] == "rm"]
        # Off the network an adopted VM's IPv6 reaches nothing, so it is not looked at.
        assert removed == (["sightread-sandbox-91"] if network != "none" else [])


async def test_the_vm_scratch_size_comes_from_settings(tmp_path, monkeypatch) -> None:
    from sightread_sandbox import backends

    calls: list[tuple[str, ...]] = []

    async def fake_docker(*args, timeout=120.0):
        calls.append(args)
        if args[0] == "inspect":
            return 1, "Error: No such object"
        return 0, ""

    monkeypatch.setattr(backends, "_docker", fake_docker)
    settings = Settings(
        app_env="local",
        sandbox_token=TOKEN,
        sandbox_backend="docker",
        sandbox_runtime="runc",
        sandbox_tmp_size="256m",
        sandbox_workspaces_dir=str(tmp_path / "ws"),
        sandbox_workspaces_mount=str(tmp_path / "ws"),
    )
    backend = backends.DockerBackend(settings)
    await backend.ensure_running(backend._user(41))
    run = next(args for args in calls if args[0] == "run")
    assert "/tmp:rw,exec,size=256m" in run  # noqa: S108 - the VM scratch, not the host
    assert "size=1g" not in " ".join(run)
    # The parse baseline is a nested read-only mount over the workspace mount, its target
    # made before the start (docs/sandbox.md § Backends).
    host = tmp_path / "ws" / "41"
    baseline = f"{host}/.sightread:/workspace/.sightread:ro"
    assert baseline in run
    assert run.index(f"{host}:/workspace") < run.index(baseline)
    assert (host / ".sightread").is_dir()


async def test_the_default_environment_is_made_after_each_start(tmp_path, monkeypatch) -> None:
    from sightread_sandbox import backends

    calls: list[tuple[str, ...]] = []
    env_code = 0

    async def fake_docker(*args, timeout=120.0):
        calls.append(args)
        if args[0] == "inspect":
            return 1, "Error: No such object"
        if args[0] == "exec":
            return env_code, "" if env_code == 0 else "error: no space left on device"
        return 0, ""

    monkeypatch.setattr(backends, "_docker", fake_docker)
    settings = Settings(
        app_env="local",
        sandbox_token=TOKEN,
        sandbox_backend="docker",
        sandbox_runtime="runc",
        sandbox_workspaces_dir=str(tmp_path / "ws"),
        sandbox_workspaces_mount=str(tmp_path / "ws"),
    )
    backend = backends.DockerBackend(settings)
    await backend.ensure_running(backend._user(71))
    run_at = next(index for index, args in enumerate(calls) if args[0] == "run")
    # Every program the runner starts in the VM by its full path, the reaper included.
    assert calls[run_at][-3:] == (backends.REAPER_PYTHON, "-c", backends.REAPER)
    assert calls[run_at + 1] == (
        "exec",
        "sightread-sandbox-71",
        "/bin/sh",
        "-c",
        backends.DEFAULT_ENV,
    )
    argv, _cwd = backend.shell_argv(71)
    assert argv[argv.index("sightread-sandbox-71") + 1 :][:4] == [
        "/usr/bin/setsid",
        "-w",
        "/bin/sh",
        "-c",
    ]
    # A start whose environment could not be made still gives a shell.
    env_code = 1
    calls.clear()
    await backend.ensure_running(backend._user(72))
    assert backend._user(72).started_at is not None


def test_the_default_environment_check_only_looks(tmp_path) -> None:
    import subprocess

    from sightread_sandbox.backends import DEFAULT_ENV, SAFE_PATH

    venv = tmp_path / "venv"
    (venv / "bin").mkdir(parents=True)
    fake_uv = tmp_path / "bin" / "uv"
    fake_uv.parent.mkdir()
    fake_uv.write_text('#!/bin/sh\necho "uv $*" > "$VIRTUAL_ENV.made"\n')
    fake_uv.chmod(0o755)
    made = tmp_path / "venv.made"
    script = DEFAULT_ENV.replace(f"PATH={SAFE_PATH}", f"PATH={fake_uv.parent}:{SAFE_PATH}")
    env = {**os.environ, "VIRTUAL_ENV": str(venv)}

    def check(environment: dict[str, str]) -> None:
        subprocess.run(["sh", "-c", script], env=environment, check=True)  # noqa: S603, S607

    # None yet: made, keeping whatever the directory holds.
    check(env)
    assert made.read_text() == f"uv venv --quiet --allow-existing {venv}\n"
    made.unlink()
    # A dangling link (the image's Python moved on) is made again, in place.
    (venv / "bin" / "python").symlink_to(tmp_path / "gone-python3.11")
    check(env)
    assert made.exists()
    made.unlink()
    # An interpreter that is there is left as it is, and nothing of it is run.
    (venv / "bin" / "python").unlink()
    (venv / "bin" / "python").write_text('#!/bin/sh\ntouch "$VIRTUAL_ENV.ran"\n')
    (venv / "bin" / "python").chmod(0o755)
    check(env)
    assert not made.exists() and not (tmp_path / "venv.ran").exists()
    # An image that names no environment gets none.
    check({key: value for key, value in env.items() if key != "VIRTUAL_ENV"})
    assert not made.exists()


async def test_a_program_a_command_put_first_on_the_path_does_not_run_the_cleanup(
    tmp_path, monkeypatch
) -> None:
    """The VM's PATH starts in the workspace (the default environment's bin): a `pgrep`
    a command writes there must not stand in for the system's in the cleanup that ends
    what the command left running, nor a `bash` run the next command."""
    from sightread_sandbox.shell import GROUP_CLEANUP, ShellSession, supervisor

    planted = tmp_path / "venv" / "bin"
    planted.mkdir(parents=True)
    monkeypatch.setenv("PATH", f"{planted}:{os.environ['PATH']}")
    shell = ShellSession(["sh", "-c", supervisor(GROUP_CLEANUP)], cwd=str(tmp_path), max_bytes=1000)
    await shell.start()
    try:
        # The command finds its PATH as the VM gave it, and plants the fakes there.
        outcome = await shell.run(
            f"""case "$PATH" in {planted}:*) echo first;; esac
printf '#!/bin/sh\nexit 1\n' > {planted}/pgrep
printf '#!/bin/sh\necho planted\n' > {planted}/bash
chmod 755 {planted}/pgrep {planted}/bash
nohup sleep 30 >/dev/null 2>&1 & disown
echo $!""",
            timeout=10,
        )
        assert outcome.exit_code == 0, outcome.output
        lines = outcome.output.split()
        assert lines[0] == "first"
        pid = int(lines[-1])
        # The real pgrep ran: the job was ended (dead, or a zombie this host's init
        # has not reaped yet).
        status = Path(f"/proc/{pid}/status")
        assert not status.exists() or "State:\tZ" in status.read_text()
        after = await shell.run("echo ran; command -v pgrep", timeout=10)
        assert after.output.split()[0] == "ran"
        # The command itself still sees what it planted: the PATH is the VM's.
        assert after.output.split()[1] == f"{planted}/pgrep"
    finally:
        await shell.close()


def test_the_vm_sweep_repeats_until_nothing_but_the_shells_is_left(tmp_path) -> None:
    """The sweep against a fake process table: `ps` and `kill` on PATH read and change a
    file, and killing the forker spawns a child the first snapshot never saw."""
    import os
    import subprocess

    from sightread_sandbox.backends import VM_SWEEP

    state = tmp_path / "procs"
    # pid ppid stat: init, the exec root (ppid 0), the live supervisor, a leftover that
    # forks on death, a detached job, and a zombie waiting for pid 1.
    state.write_text("1 0 S\n10 0 S\n20 10 S\n30 20 S\n40 1 S\n50 1 Z\n")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "ps").write_text(
        "#!/bin/sh\n"
        'if [ "$1" = -o ]; then grep "^$4 " "$FAKE_PROCS" | cut -d" " -f2; exit 0; fi\n'
        'cut -d" " -f1,3 "$FAKE_PROCS"\n'
    )
    (bin_dir / "kill").write_text(
        "#!/bin/sh\n"
        'pid="$2"\n'
        'grep -q "^$pid " "$FAKE_PROCS" || exit 1\n'
        'grep -v "^$pid " "$FAKE_PROCS" > "$FAKE_PROCS.new"; mv "$FAKE_PROCS.new" "$FAKE_PROCS"\n'
        '[ "$pid" = 30 ] && echo "31 1 S" >> "$FAKE_PROCS"\n'
        'echo "$pid" >> "$FAKE_PROCS.killed"\n'
        "exit 0\n"
    )
    for script in ("ps", "kill"):
        os.chmod(bin_dir / script, 0o700)
    env = {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}", "FAKE_PROCS": str(state)}
    # `kill` is the shell's builtin: the sweep takes the fake through `$__sr_kill`.
    script = (
        f'__sr_keep_in="20"\n__sr_kill="{bin_dir / "kill"}"\n{VM_SWEEP}\n'
        'echo "LEFT=$__sr_left PASS=$__sr_pass"'
    )
    done = subprocess.run(  # noqa: S603
        ["sh", "-c", script],  # noqa: S607
        capture_output=True,
        text=True,
        env=env,
        check=True,
    )
    assert "LEFT=0" in done.stdout
    assert int(done.stdout.split("PASS=")[1]) >= 2  # the late child took a second pass
    killed = sorted(int(pid) for pid in (tmp_path / "procs.killed").read_text().split())
    assert killed == [30, 31, 40]
    assert sorted(state.read_text().split("\n")[:-1]) == ["1 0 S", "10 0 S", "20 10 S", "50 1 Z"]


def test_the_vm_sweep_does_not_count_its_own_ps_as_left_over(tmp_path) -> None:
    """Every snapshot holds the pipeline that took it, gone by the time its kill comes:
    those pids are not left over, or no sweep would ever finish. A process that refuses
    the kill and still stands is, and the sweep fails closed."""
    import os
    import subprocess

    from sightread_sandbox.backends import VM_SWEEP

    state = tmp_path / "procs"
    state.write_text("1 0 S\n10 0 S\n20 10 S\n30 20 S\n")
    calls = tmp_path / "calls"
    calls.write_text("0")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    # Each full listing also shows a fresh pid of its own, absent from the table.
    (bin_dir / "ps").write_text(
        "#!/bin/sh\n"
        'if [ "$1" = -o ]; then grep "^$4 " "$FAKE_PROCS" | cut -d" " -f2; exit 0; fi\n'
        'n=$(($(cat "$FAKE_CALLS") + 1)); echo "$n" > "$FAKE_CALLS"\n'
        'cut -d" " -f1,3 "$FAKE_PROCS"\n'
        'echo "$((900 + n)) R"\n'
    )
    # Pid 60 refuses the kill and stays.
    (bin_dir / "kill").write_text(
        "#!/bin/sh\n"
        'pid="$2"\n'
        '[ "$pid" = 60 ] && exit 1\n'
        'grep -q "^$pid " "$FAKE_PROCS" || exit 1\n'
        'grep -v "^$pid " "$FAKE_PROCS" > "$FAKE_PROCS.new"; mv "$FAKE_PROCS.new" "$FAKE_PROCS"\n'
        "exit 0\n"
    )
    for script in ("ps", "kill"):
        os.chmod(bin_dir / script, 0o700)
    env = {
        **os.environ,
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "FAKE_PROCS": str(state),
        "FAKE_CALLS": str(calls),
    }
    script = (
        f'__sr_keep_in="20"\n__sr_kill="{bin_dir / "kill"}"\n{VM_SWEEP}\n'
        'echo "LEFT=$__sr_left PASS=$__sr_pass"'
    )

    def sweep() -> str:
        return subprocess.run(  # noqa: S603
            ["sh", "-c", script],  # noqa: S607
            capture_output=True,
            text=True,
            env=env,
            check=True,
        ).stdout

    assert "LEFT=0 PASS=2" in sweep()  # the leftover goes, the next pass finds only ps
    assert sorted(state.read_text().split("\n")[:-1]) == ["1 0 S", "10 0 S", "20 10 S"]

    with state.open("a") as table:
        table.write("60 1 S\n")
    assert "LEFT=1 PASS=50" in sweep()


async def test_a_reset_whose_cache_cannot_be_removed_reports_it(
    client, tmp_path, monkeypatch
) -> None:
    import shutil

    from sightread_sandbox import backends

    await client.post("/sandboxes/51/exec", json={"session": "s", "command": "true"})
    cache = tmp_path / "ws" / "51" / ".cache"
    cache.mkdir(parents=True, exist_ok=True)
    (cache / "state").write_text("x")

    real = shutil.rmtree

    def refusing(path, *args, **kwargs):
        raise OSError(30, "Read-only file system")

    monkeypatch.setattr(backends.shutil, "rmtree", refusing)
    assert (await client.post("/sandboxes/51/reset")).status_code == 503
    assert (cache / "state").exists()
    monkeypatch.setattr(backends.shutil, "rmtree", real)
    assert (await client.post("/sandboxes/51/reset")).status_code == 204
    assert not cache.exists()


def test_the_runner_refuses_a_relative_workspace_host_path(tmp_path) -> None:
    with pytest.raises(ValueError, match="absolute"):
        Settings(
            app_env="local",
            sandbox_token=TOKEN,
            sandbox_backend="docker",
            sandbox_runtime="runc",
            sandbox_workspaces_dir="./workspaces",
            sandbox_workspaces_mount=str(tmp_path / "ws"),
        )
    # The local backend runs no `docker run` and takes any path.
    Settings(
        app_env="local",
        sandbox_token=TOKEN,
        sandbox_backend="local",
        sandbox_workspaces_dir="./workspaces",
        sandbox_workspaces_mount=str(tmp_path / "ws"),
    )


async def test_the_vm_start_waits_as_long_as_configured(tmp_path, monkeypatch) -> None:
    from sightread_sandbox import backends

    timeouts: list[float] = []

    async def fake_docker(*args, timeout=120.0):
        if args[0] == "run":
            timeouts.append(timeout)
        if args[0] == "inspect":
            return 1, "Error: No such object"
        return 0, ""

    monkeypatch.setattr(backends, "_docker", fake_docker)
    settings = Settings(
        app_env="local",
        sandbox_token=TOKEN,
        sandbox_backend="docker",
        sandbox_runtime="runc",
        sandbox_start_timeout_seconds=333,
        sandbox_workspaces_dir=str(tmp_path / "ws"),
        sandbox_workspaces_mount=str(tmp_path / "ws"),
    )
    backend = backends.DockerBackend(settings)
    await backend.ensure_running(backend._user(61))
    assert timeouts == [333]


async def test_an_eviction_that_left_the_vm_unstoppable_starts_no_shell(tmp_path) -> None:
    class Stuck(LocalBackend):
        async def _make_room(self, sandbox):
            evicting = len(sandbox.sessions) >= self.settings.sandbox_max_shells
            await super()._make_room(sandbox)
            if evicting:
                sandbox.unstoppable = True  # the evicted shell's command could not be ended

    settings = Settings(
        app_env="local",
        sandbox_token=TOKEN,
        sandbox_backend="local",
        sandbox_max_shells=1,
        sandbox_workspaces_dir=str(tmp_path / "ws"),
        sandbox_workspaces_mount=str(tmp_path / "ws"),
    )
    backend = Stuck(settings)
    await backend.exec(71, "a", "true", timeout=10)
    with pytest.raises(RuntimeError):
        await backend.exec(71, "b", "true", timeout=10)
    assert "b" not in backend._user(71).sessions


async def test_a_stale_vm_is_removed_within_the_configured_timeout(tmp_path, monkeypatch) -> None:
    from sightread_sandbox import backends

    timeouts: dict[str, float] = {}

    async def fake_docker(*args, timeout=120.0):
        timeouts[args[0]] = timeout
        if args[0] == "inspect":
            return 1, "Error: No such object"
        return 0, ""

    monkeypatch.setattr(backends, "_docker", fake_docker)
    settings = Settings(
        app_env="local",
        sandbox_token=TOKEN,
        sandbox_backend="docker",
        sandbox_runtime="runc",
        sandbox_docker_timeout_seconds=444,
        sandbox_workspaces_dir=str(tmp_path / "ws"),
        sandbox_workspaces_mount=str(tmp_path / "ws"),
    )
    backend = backends.DockerBackend(settings)
    await backend.ensure_running(backend._user(81))
    assert timeouts["rm"] == 444


async def test_a_vm_docker_cannot_inspect_is_unavailable_not_stopped(tmp_path, monkeypatch) -> None:
    from sightread_sandbox import backends

    answer = {"out": (1, "Cannot connect to the Docker daemon")}

    async def fake_docker(*args, timeout=120.0):
        if args[0] == "inspect":
            return answer["out"]
        return 0, ""

    monkeypatch.setattr(backends, "_docker", fake_docker)
    settings = Settings(
        app_env="local",
        sandbox_token=TOKEN,
        sandbox_backend="docker",
        sandbox_runtime="runc",
        sandbox_idle_seconds=0,
        sandbox_workspaces_dir=str(tmp_path / "ws"),
        sandbox_workspaces_mount=str(tmp_path / "ws"),
    )
    backend = backends.DockerBackend(settings)
    # The daemon away, or an inspection that timed out: unknown, never a stopped VM the
    # dashboard would offer to start.
    with pytest.raises(RuntimeError):
        await backend.is_running(7)
    app = create_app(settings, backend)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://runner") as http:
        http.headers["Authorization"] = f"Bearer {TOKEN}"
        assert (await http.get("/sandboxes/7")).status_code == 503
        # Docker's own "no such" is a positive absence: stopped.
        answer["out"] = (1, "Error: No such object: sightread-sandbox-7")
        assert (await http.get("/sandboxes/7")).json()["state"] == "stopped"
    # The idle sweep skips what it cannot ask about and goes on.
    answer["out"] = (1, "docker timed out")
    backend._user(7).last_used = 0
    assert await backend.stop_idle() == 0


async def test_a_command_whose_vm_became_unstoppable_is_not_reported_done(tmp_path) -> None:
    class Stuck(LocalBackend):
        gone = False

        def on_timeout(self, user_id):
            async def kill_inside(*, pid: int, locked: bool = False) -> None:
                # The in-VM kill failed and so did the removal: the process may live on.
                self._user(user_id).unstoppable = True

            return kill_inside

        async def is_gone(self, user_id: int) -> bool:
            return self.gone

    settings = Settings(
        app_env="local",
        sandbox_token=TOKEN,
        sandbox_backend="local",
        sandbox_docker_timeout_seconds=1,
        sandbox_workspaces_dir=str(tmp_path / "ws"),
        sandbox_workspaces_mount=str(tmp_path / "ws"),
    )
    backend = Stuck(settings)
    # Not an outcome the worker may commit a tree over: 503, after the removal timeout
    # passed without Docker reporting the VM gone.
    with pytest.raises(RuntimeError):
        await backend.exec(81, "s", "sleep 30", timeout=0.2)
    assert backend._user(81).unstoppable is True
    # Still unstoppable: no command runs until Docker says the VM is gone.
    with pytest.raises(RuntimeError):
        await backend.exec(81, "s", "true", timeout=5)
    backend.gone = True
    outcome = await backend.exec(81, "s", "true", timeout=5)
    assert outcome.exit_code == 0 and backend._user(81).unstoppable is False
    await backend.stop(81)


async def test_a_restart_that_cannot_confirm_the_kill_is_not_done(tmp_path) -> None:
    import asyncio

    class Stuck(LocalBackend):
        gone = False

        def on_timeout(self, user_id):
            async def kill_inside(*, pid: int, locked: bool = False) -> None:
                self._user(user_id).unstoppable = True  # neither killed nor removed

            return kill_inside

        async def is_gone(self, user_id: int) -> bool:
            return self.gone

    settings = Settings(
        app_env="local",
        sandbox_token=TOKEN,
        sandbox_backend="local",
        sandbox_docker_timeout_seconds=1,
        sandbox_workspaces_dir=str(tmp_path / "ws"),
        sandbox_workspaces_mount=str(tmp_path / "ws"),
    )
    backend = Stuck(settings)
    running = asyncio.ensure_future(backend.exec(91, "s", "sleep 30", timeout=60))
    await asyncio.sleep(0.3)
    # The worker asks for the shell to be restarted to settle an unconfirmed command:
    # a restart that could not end it answers 503, never a done the tree is recorded on.
    with pytest.raises(RuntimeError):
        await backend.restart(91, "s")
    running.cancel()
    with pytest.raises((asyncio.CancelledError, RuntimeError)):
        await running
    backend.gone = True
    app = create_app(settings, backend)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://runner") as http:
        http.headers["Authorization"] = f"Bearer {TOKEN}"
        assert (await http.post("/sandboxes/91/restart", json={"session": "s"})).json() == {
            "ok": True
        }
    await backend.stop(91)


async def test_a_command_past_the_cap_is_refused(tmp_path) -> None:
    """The cap is the runner's setting, not a constant in the request model
    (docs/sandbox.md § The runner)."""
    settings = Settings(
        app_env="local",
        sandbox_token=TOKEN,
        sandbox_backend="local",
        sandbox_workspaces_dir=str(tmp_path / "ws"),
        sandbox_workspaces_mount=str(tmp_path / "ws"),
        sandbox_command_max_chars=16,
    )
    app = create_app(settings, LocalBackend(settings))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://runner") as http:
        http.headers["Authorization"] = f"Bearer {TOKEN}"
        refused = await http.post(
            "/sandboxes/9/exec", json={"session": "s", "command": "echo " + "x" * 20}
        )
        assert refused.status_code == 422
        allowed = await http.post("/sandboxes/9/exec", json={"session": "s", "command": "echo ok"})
        assert allowed.status_code == 200 and allowed.json()["output"].strip() == "ok"
