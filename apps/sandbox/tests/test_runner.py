"""The runner over a local bash stand-in for its backend: no Docker, no network."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import shutil
import subprocess
import time
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient

from local_backend import GROUP_CLEANUP, LocalBackend
from sightread_sandbox import backends
from sightread_sandbox.backends import (
    ADOPT_SWEEP,
    CACHE_DIR,
    DEFAULT_ENV,
    KILL_TREE,
    REAPER,
    REAPER_PYTHON,
    VM_SWEEP,
    DockerBackend,
    UserSandbox,
    adopted_user_ids,
)
from sightread_sandbox.config import Settings
from sightread_sandbox.main import create_app
from sightread_sandbox.shell import SAFE_PATH, ShellSession, supervisor

TOKEN = "runner-token-not-a-real-one"
PROXY_URL = "http://10.250.250.2:3128"


def local_settings(tmp_path: Path, **overrides: Any) -> Settings:
    """The runner's settings for the tests, the workspaces under the test's own directory."""
    return Settings(
        **{
            "app_env": "local",
            "sandbox_token": TOKEN,
            "sandbox_workspaces_dir": str(tmp_path / "ws"),
            "sandbox_workspaces_mount": str(tmp_path / "ws"),
            **overrides,
        }
    )


def docker_settings(tmp_path: Path, **overrides: Any) -> Settings:
    """The Docker backend's settings on `runc`, a runtime only `APP_ENV=local` accepts."""
    return local_settings(tmp_path, **{"sandbox_runtime": "runc", **overrides})


@contextlib.asynccontextmanager
async def runner(backend: backends.Backend) -> AsyncIterator[AsyncClient]:
    """An authenticated client of the runner app over `backend` and its settings."""
    app = create_app(backend.settings, backend)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://runner") as http:
        http.headers["Authorization"] = f"Bearer {TOKEN}"
        yield http


@pytest.fixture
async def client(tmp_path) -> AsyncIterator[AsyncClient]:
    backend = LocalBackend(local_settings(tmp_path, sandbox_output_max_bytes=400))
    async with runner(backend) as http:
        yield http
        for user_id in list(backend._users):
            await backend.stop(user_id)


class FakeDocker:
    """Stands in for `backends._docker`: records every call with its timeout, answers
    `inspect` as Docker does for a VM that does not exist and every other call with
    success, unless `answer` returns something for the call."""

    def __init__(self) -> None:
        self.calls: list[tuple[tuple[str, ...], float]] = []
        self.answer: Callable[[tuple[str, ...]], tuple[int, str] | None] = lambda args: None

    async def __call__(self, *args: str, timeout: float) -> tuple[int, str]:
        self.calls.append((args, timeout))
        answered = self.answer(args)
        if answered is not None:
            return answered
        if args[0] == "inspect":
            return 1, "Error: No such object"
        return 0, ""

    def args(self, kind: str | None = None) -> list[tuple[str, ...]]:
        """The arguments of every call, or of every `docker <kind>` call."""
        return [args for args, _ in self.calls if kind is None or args[0] == kind]

    def timeouts(self, kind: str) -> list[float]:
        return [timeout for args, timeout in self.calls if args[0] == kind]


@pytest.fixture
def docker(monkeypatch) -> FakeDocker:
    fake = FakeDocker()
    monkeypatch.setattr(backends, "_docker", fake)
    return fake


def _proc_state(pid: int) -> str | None:
    """The host process's state letter, or None once it is gone."""
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
    except (FileNotFoundError, ProcessLookupError):
        return None
    # The name in parentheses may hold spaces or parentheses itself.
    return stat.rsplit(")", 1)[1].split()[0]


async def wait_ended(pid: int, within: float = 5.0) -> bool:
    """Whether the host process `pid` ends within `within` seconds. The cleanup sends the
    kill without waiting for it to land, and this host's init may be slow to reap: gone,
    a zombie, or one being reaped all count as ended."""
    deadline = time.monotonic() + within
    while time.monotonic() < deadline:
        if _proc_state(pid) in (None, "Z", "X", "x"):
            return True
        await asyncio.sleep(0.05)
    return False


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


async def test_the_shell_cap_closes_the_longest_idle_shell(tmp_path) -> None:
    async with runner(LocalBackend(local_settings(tmp_path, sandbox_max_shells=2))) as http:
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
    listing = "ema-paper-sandbox-12\nema-paper-sandbox-runner\nother\nema-paper-sandbox-7\n"
    assert adopted_user_ids(listing) == [12, 7]


async def test_the_idle_sweep_spares_a_running_command(tmp_path) -> None:
    inspected: list[int] = []

    class Counting(LocalBackend):
        async def is_running(self, user_id: int) -> bool:
            inspected.append(user_id)
            return await super().is_running(user_id)

    backend = Counting(local_settings(tmp_path, sandbox_idle_seconds=0))
    running = asyncio.create_task(backend.exec(8, "s", "sleep 1; echo done", timeout=10))
    await asyncio.sleep(0.2)
    assert await backend.stop_idle() == 0
    outcome = await running
    assert outcome.output == "done"
    assert await backend.stop_idle() == 1
    # A VM the sweep removed is not asked about again until something starts it; one
    # Docker merely reports not running is asked about on every sweep.
    backend._user(9)
    for _ in range(2):
        inspected.clear()
        assert await backend.stop_idle() == 0
        assert inspected == [9]
    await backend.exec(8, "s", "true", timeout=10)
    inspected.clear()
    assert await backend.stop_idle() == 1
    assert inspected == [8, 9]


async def test_a_vm_whose_start_timed_out_is_swept_once_it_comes_up(tmp_path, docker) -> None:
    # The `docker run` CLI timed out, but the daemon goes on starting the VM: an
    # inspection first finds it created and not running, later running.
    vm = {"state": "absent"}

    def answer(args: tuple[str, ...]) -> tuple[int, str] | None:
        if args[0] == "run":
            vm["state"] = "created"
            return 1, "docker timed out"
        if args[0] == "rm":
            vm["state"] = "absent"
        if args[0] == "inspect" and vm["state"] != "absent":
            return 0, "true\n" if vm["state"] == "running" else "false\n"
        return None

    docker.answer = answer
    backend = DockerBackend(docker_settings(tmp_path, sandbox_idle_seconds=0))
    with pytest.raises(RuntimeError):
        await backend.exec(5, "s", "true", timeout=5)
    assert await backend.stop_idle() == 0
    vm["state"] = "running"
    assert await backend.stop_idle() == 1
    assert vm["state"] == "absent"


async def test_an_underscore_flood_is_bounded_too(client) -> None:
    flood = await client.post(
        "/sandboxes/9/exec",
        json={"session": "s", "command": "head -c 300000 /dev/zero | tr '\\0' '_'; echo; echo end"},
    )
    assert flood.status_code == 200, flood.text
    body = flood.json()
    assert body["truncated"] is True
    assert body["exit_code"] == 0
    assert body["output"].rstrip().endswith("end")
    assert len(body["output"]) < 2000


async def test_a_timeout_runs_the_backends_kill_before_closing_the_shell() -> None:
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
    def production(**overrides: Any) -> Settings:
        return docker_settings(tmp_path, app_env="production", **overrides)

    assert production(sandbox_runtime="kata").sandbox_runtime == "kata"
    for runtime in ("runc", "crun", "kata2"):
        with pytest.raises(ValueError):
            production(sandbox_runtime=runtime)
    # The operator names their own VM runtime.
    assert production(sandbox_runtime="kata2", sandbox_vm_runtimes="kata2").sandbox_runtime


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
    # The local backend's pids are this host's.
    assert await wait_ended(int(disowned.json()["output"].strip()))


async def test_a_vm_that_cannot_be_removed_refuses_commands_and_keeps_its_cache(tmp_path) -> None:
    class Stuck(LocalBackend):
        gone = False

        async def stop_vm(self, sandbox: UserSandbox) -> bool:
            return False  # `docker rm -f` failed or timed out

        async def is_gone(self, user_id: int) -> bool:
            return self.gone

    backend = Stuck(local_settings(tmp_path, sandbox_idle_seconds=0))
    async with runner(backend) as http:
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
    backend = LocalBackend(local_settings(tmp_path))
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
    assert (
        await client.post("/sandboxes/13/exec", json={"session": "s", "command": "true"})
    ).status_code == 200
    assert (await client.post("/sandboxes/13/stop")).status_code == 204
    cache = tmp_path / "ws" / "13" / CACHE_DIR
    shutil.rmtree(cache)
    cache.write_text("not a directory")  # what a command left in its place
    assert (await client.post("/sandboxes/13/reset")).status_code == 204
    assert not cache.exists()
    again = await client.post("/sandboxes/13/exec", json={"session": "s", "command": "echo ok"})
    assert again.status_code == 200, again.text
    assert again.json()["output"] == "ok"
    assert cache.is_dir()


def test_the_vm_scripts_are_valid_sh_and_the_docker_backend_sweeps_the_vm(tmp_path) -> None:
    for script in (KILL_TREE, ADOPT_SWEEP):
        subprocess.run(["sh", "-n"], input=script, text=True, check=True)  # noqa: S607
    # The supervisor a VM's shell runs ends every command with the VM-wide sweep.
    argv, _cwd = DockerBackend(docker_settings(tmp_path)).shell_argv(1)
    assert VM_SWEEP in argv[-1]
    # The timeout kill and the adoption both end with the sweep.
    assert VM_SWEEP.strip() in KILL_TREE and VM_SWEEP.strip() in ADOPT_SWEEP


async def test_a_command_cannot_forge_its_own_status_line(client, tmp_path) -> None:
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
    compile(REAPER, "reaper", "exec")
    dockerfile = Path(__file__).resolve().parents[3] / "deploy" / "sandbox-image" / "Dockerfile"
    # The interpreter by its full path too: the VM's PATH starts in the workspace.
    assert f"CMD {json.dumps([REAPER_PYTHON, '-c', REAPER])}" in dockerfile.read_text()


@pytest.fixture
def kill_fails(tmp_path, monkeypatch) -> SimpleNamespace:
    """The Docker backend's own kill inside the VM, over a fake `docker` whose KILL_TREE
    always fails: the shells run on this host, everything else is answered as Docker
    would. `vm.removable` says whether `docker rm -f` succeeds."""
    calls: list[tuple[str, ...]] = []
    vm = SimpleNamespace(running=False, removable=True)

    async def fake_docker(*args, **_):
        calls.append(args)
        if args[0] == "inspect":
            return (0, "true\n") if vm.running else (1, "Error: No such object")
        if args[0] == "run":
            vm.running = True
        if args[0] == "rm":
            if not vm.removable:
                return 1, "Error: timed out"
            vm.running = False
        if args[0] == "exec" and KILL_TREE in args:
            return 1, "kill failed"
        return 0, ""

    class HostShells(DockerBackend):
        def shell_argv(self, user_id: int) -> tuple[list[str], str | None]:
            return ["sh", "-c", supervisor(GROUP_CLEANUP)], str(self.workspace(user_id))

    monkeypatch.setattr(backends, "_docker", fake_docker)
    settings = docker_settings(tmp_path, sandbox_docker_timeout_seconds=1)

    def count(kind: str) -> int:
        """How many `docker <kind>` calls were made — `kill`: KILL_TREE execs."""
        if kind == "kill":
            return len([args for args in calls if args[0] == "exec" and KILL_TREE in args])
        return len([args for args in calls if args[0] == kind])

    return SimpleNamespace(backend=HostShells(settings), vm=vm, calls=calls, count=count)


async def test_a_restart_whose_in_vm_kill_fails_removes_the_vm_under_its_own_lock(
    kill_fails,
) -> None:
    backend = kill_fails.backend
    await backend.exec(15, "s", "true", timeout=10)
    kill_fails.calls.clear()
    # The fallback stop runs under the lock restart holds rather than waiting for it.
    await asyncio.wait_for(backend.restart(15, "s"), 5)
    assert kill_fails.count("kill") == 1
    assert kill_fails.count("rm") == 1
    assert not kill_fails.vm.running
    assert not backend._user(15).lock.locked()
    assert backend._user(15).unstoppable is False


async def test_a_stop_whose_in_vm_kill_fails_removes_the_vm_once(kill_fails) -> None:
    backend = kill_fails.backend
    running = asyncio.create_task(backend.exec(16, "busy", "sleep 30", timeout=60))
    await asyncio.sleep(0.3)
    kill_fails.calls.clear()
    await asyncio.wait_for(backend.stop(16), 10)
    # The kill saw the stop under way and left the removal to it.
    assert kill_fails.count("kill") == 1
    assert kill_fails.count("rm") == 1
    outcome = await asyncio.wait_for(running, 10)
    assert outcome.exit_code is None and not outcome.timed_out
    assert backend._user(16).sessions == {}


async def test_a_restart_whose_kill_and_removal_both_fail_leaves_the_vm_unstoppable(
    kill_fails,
) -> None:
    backend = kill_fails.backend
    await backend.exec(17, "s", "true", timeout=10)
    kill_fails.vm.removable = False
    with pytest.raises(RuntimeError, match="could not be stopped"):
        await asyncio.wait_for(backend.restart(17, "s"), 10)
    assert backend._user(17).unstoppable is True
    assert not backend._user(17).lock.locked()
    with pytest.raises(RuntimeError, match="no command runs there"):
        await backend.exec(17, "s", "true", timeout=10)


async def test_a_supervisor_killed_by_its_command_is_swept(tmp_path) -> None:
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

    backend = Recording(local_settings(tmp_path, sandbox_max_shells=1))
    await backend.exec(16, "a", "true", timeout=10)
    await backend.exec(16, "b", "true", timeout=10)
    # `a` made room for `b`: killed where it runs, under the lock exec holds.
    assert calls == [True]
    await backend.stop(16)


async def test_the_supervisor_reports_its_pid_before_any_command(tmp_path) -> None:
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
    class Recording(LocalBackend):
        def shell_argv(self, user_id):
            return ["sh", "-c", supervisor('printf "KEEP=[%s]\\n" "$__sr_keep_in"')], str(
                self.workspace(user_id)
            )

    backend = Recording(local_settings(tmp_path))
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
    # The script runs on this host: should the pid check ever fail, the sweep after it
    # would kill every process the account owns. Its kill is `true` here, so a broken
    # check leaves every pid standing and fails the test with 5 instead.
    env = {**os.environ, "__sr_kill": "true"}
    for bad in ("", "abc", "shell.pid", "12 13"):
        done = subprocess.run(  # noqa: S603
            ["sh", "-c", KILL_TREE, "sh", bad, ""],  # noqa: S607
            capture_output=True,
            env=env,
            timeout=30,
        )
        assert done.returncode == 3, bad


async def test_a_stop_kills_a_running_command_inside_the_vm_first(tmp_path) -> None:
    calls: list[tuple[int, bool]] = []

    class Recording(LocalBackend):
        def on_timeout(self, user_id):
            async def kill_inside(*, pid: int, locked: bool = False) -> None:
                calls.append((pid, locked))

            return kill_inside

    backend = Recording(local_settings(tmp_path))
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
    async def daemon_away(*args, **_):
        return 1, "Cannot connect to the Docker daemon"

    monkeypatch.setattr(backends, "_docker", daemon_away)
    with pytest.raises(RuntimeError):
        await DockerBackend(docker_settings(tmp_path)).startup()


async def test_the_runner_starts_only_on_an_internal_network(tmp_path, monkeypatch) -> None:
    inspected = {"ema-paper-sandbox": "true true"}

    async def fake_docker(*args, **_):
        if args[:2] == ("network", "inspect"):
            answer = inspected.get(args[-1])
            return (0, f"{answer}\n") if answer else (1, "not found")
        return 0, ""

    monkeypatch.setattr(backends, "_docker", fake_docker)

    def settings(network: str) -> Settings:
        return docker_settings(tmp_path, sandbox_network=network, sandbox_proxy_url=PROXY_URL)

    # A network Docker routes off — `bridge`, or the sandbox's made without `internal` —
    # would hand the VMs the internet past the proxy; one where the host has an address,
    # whatever the host listens on; one Docker cannot find, nothing.
    for answer, network in (
        ("false <no value>", "bridge"),
        ("true <no value>", "host-has-an-address"),
        ("true false", "host-has-an-address-too"),
        (None, "missing"),
    ):
        if answer:
            inspected[network] = answer
        with pytest.raises(RuntimeError, match="not an internal Docker network"):
            await DockerBackend(settings(network)).startup()
    await DockerBackend(settings("ema-paper-sandbox")).startup()
    await DockerBackend(settings("none")).startup()


def test_a_network_needs_the_proxy(tmp_path) -> None:
    with pytest.raises(ValueError, match="SANDBOX_PROXY_URL"):
        docker_settings(tmp_path, sandbox_network="ema-paper-sandbox")


async def test_a_vm_on_the_network_is_pointed_at_the_proxy(tmp_path, docker) -> None:
    for network, user_id in (("ema-paper-sandbox", 81), ("none", 82)):
        backend = DockerBackend(
            docker_settings(tmp_path, sandbox_network=network, sandbox_proxy_url=PROXY_URL)
        )
        await backend.ensure_running(backend._user(user_id))
    on, off = docker.args("run")
    assert on[on.index("--network") + 1] == "ema-paper-sandbox"
    execs = docker.args("exec")
    # The default environment is made in the VM just started, the network's as any other.
    made = [args[1] for args in execs if args[-1] == DEFAULT_ENV]
    assert made == ["ema-paper-sandbox-81", "ema-paper-sandbox-82"]
    # Both spellings of both: uv, pip, git, curl and Python's clients each read one.
    for name in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy"):
        assert f"{name}={PROXY_URL}" in on
    assert off[off.index("--network") + 1] == "none"
    assert not [arg for arg in off if "PROXY" in arg.upper()]
    # IPv6 off, and checked off, on the network alone: its bridge keeps a link-local
    # address the host answers on.
    assert "net.ipv6.conf.all.disable_ipv6=1" in on
    assert "net.ipv6.conf.default.disable_ipv6=1" in on
    assert not [arg for arg in off if "ipv6" in arg]
    assert [args[1] for args in execs if args[-1] == backends.NO_IPV6] == ["ema-paper-sandbox-81"]


async def test_a_vm_on_the_network_that_keeps_ipv6_is_never_used(tmp_path, docker) -> None:
    def keeps_ipv6(args: tuple[str, ...]) -> tuple[int, str] | None:
        return (1, "") if args[0] == "exec" and args[-1] == backends.NO_IPV6 else None

    docker.answer = keeps_ipv6
    backend = DockerBackend(
        docker_settings(tmp_path, sandbox_network="ema-paper-sandbox", sandbox_proxy_url=PROXY_URL)
    )
    sandbox = backend._user(83)
    with pytest.raises(RuntimeError, match="could not be started"):
        await backend.ensure_running(sandbox)
    # Removed before anything ran in it, and not counted as started.
    assert docker.args()[-1] == ("rm", "-f", "ema-paper-sandbox-83")
    assert not [args for args in docker.args("exec") if args[-1] == DEFAULT_ENV]
    assert sandbox.started_at is None


async def test_a_vm_with_ipv6_that_cannot_be_removed_takes_no_command(
    tmp_path, monkeypatch
) -> None:
    calls: list[tuple[str, ...]] = []
    vm = {"running": False, "removable": False}

    async def fake_docker(*args, **_):
        calls.append(args)
        if args[0] == "inspect":
            return (0, "true\n") if vm["running"] else (1, "Error: No such object")
        if args[0] == "run":
            vm["running"] = True
        if args[0] == "rm":
            if not vm["removable"]:
                return 1, "Error: timed out"
            vm["running"] = False
        if args[0] == "exec" and args[-1] == backends.NO_IPV6:
            return 1, ""
        return 0, ""

    monkeypatch.setattr(backends, "_docker", fake_docker)
    backend = DockerBackend(
        docker_settings(tmp_path, sandbox_network="ema-paper-sandbox", sandbox_proxy_url=PROXY_URL)
    )

    def checks() -> int:
        return len([args for args in calls if args[0] == "exec" and args[-1] == backends.NO_IPV6])

    with pytest.raises(RuntimeError, match="could not be started"):
        await backend.exec(84, "s", "true", timeout=5)
    assert backend._user(84).unstoppable is True
    # The VM still runs, with IPv6: the next command neither takes it as started nor
    # opens a shell in it.
    with pytest.raises(RuntimeError, match="no command runs there"):
        await backend.exec(84, "s", "true", timeout=5)
    assert checks() == 1
    assert len([args for args in calls if args[0] == "run"]) == 1
    # Once Docker reports it gone, the next command starts a VM, checked again.
    vm.update(running=False, removable=True)
    with pytest.raises(RuntimeError, match="could not be started"):
        await backend.exec(84, "s", "true", timeout=5)
    assert checks() == 2
    assert backend._user(84).unstoppable is False


# One line of /proc/net/if_inet6 as the kernel writes it: ::1 on lo.
IF_INET6_LINE = "00000000000000000000000000000001 01 80 10 80       lo\n"


@pytest.mark.parametrize(
    ("content", "passes"),
    [(None, True), ("", True), (IF_INET6_LINE, False)],
    ids=["no-file", "empty", "an-address"],
)
def test_the_ipv6_check_reads_what_the_file_holds(tmp_path, content, passes) -> None:
    path = tmp_path / "if_inet6"
    if content is not None:
        path.write_text(content)
    script = backends.NO_IPV6.replace("/proc/net/if_inet6", str(path))
    result = subprocess.run(["/bin/sh", "-c", script], capture_output=True, check=False)  # noqa: S603
    assert (result.returncode == 0) is passes


def test_the_ipv6_check_does_not_trust_a_procfs_size() -> None:
    # procfs gives a file that reads non-empty a size of 0, /proc/net/if_inet6 with
    # addresses in it included: a size test would pass every VM.
    status = Path("/proc/self/status")
    assert status.stat().st_size == 0 and status.read_text()
    script = backends.NO_IPV6.replace("/proc/net/if_inet6", str(status))
    assert subprocess.run(["/bin/sh", "-c", script], check=False).returncode == 1  # noqa: S603


async def test_an_adopted_vm_on_the_network_with_ipv6_is_stopped(tmp_path, monkeypatch) -> None:
    calls: list[tuple[str, ...]] = []

    async def fake_docker(*args, **_):
        calls.append(args)
        if args[:2] == ("network", "inspect"):
            return 0, "true true\n"
        if args[0] == "ps":
            return 0, "ema-paper-sandbox-91\nema-paper-sandbox-92\n"
        # 91 holds an IPv6 address; 92 has none.
        if args[0] == "exec" and args[-1] == backends.NO_IPV6:
            return (1, "") if args[1] == "ema-paper-sandbox-91" else (0, "")
        return 0, ""

    monkeypatch.setattr(backends, "_docker", fake_docker)
    for network in ("ema-paper-sandbox", "none"):
        calls.clear()
        settings = docker_settings(tmp_path, sandbox_network=network, sandbox_proxy_url=PROXY_URL)
        await DockerBackend(settings).startup()
        removed = [args[-1] for args in calls if args[0] == "rm"]
        # Off the network an adopted VM's IPv6 reaches nothing, so it is not looked at.
        assert removed == (["ema-paper-sandbox-91"] if network != "none" else [])


async def test_a_vm_start_takes_its_limits_from_settings(tmp_path, docker) -> None:
    settings = docker_settings(
        tmp_path,
        sandbox_tmp_size="256m",
        sandbox_start_timeout_seconds=333,
        sandbox_docker_timeout_seconds=444,
    )
    backend = DockerBackend(settings)
    await backend.ensure_running(backend._user(41))
    (run,) = docker.args("run")
    assert "/tmp:rw,exec,size=256m" in run  # noqa: S108 - the VM scratch, not the host
    assert "size=1g" not in " ".join(run)
    # The parse baseline is a nested read-only mount over the workspace mount, its target
    # made before the start (docs/sandbox.md § The runner).
    host = tmp_path / "ws" / "41"
    baseline = f"{host}/.sightread:/workspace/.sightread:ro"
    assert baseline in run
    assert run.index(f"{host}:/workspace") < run.index(baseline)
    assert (host / ".sightread").is_dir()
    # The start waits as long as configured; the stale VM before it is removed within
    # Docker's own timeout.
    assert docker.timeouts("run") == [333]
    assert docker.timeouts("rm") == [444]


async def test_the_default_environment_is_made_after_each_start(tmp_path, docker) -> None:
    env_code = 0

    def environment(args: tuple[str, ...]) -> tuple[int, str] | None:
        if args[0] != "exec":
            return None
        return env_code, "" if env_code == 0 else "error: no space left on device"

    docker.answer = environment
    backend = DockerBackend(docker_settings(tmp_path))
    await backend.ensure_running(backend._user(71))
    calls = docker.args()
    run_at = next(index for index, args in enumerate(calls) if args[0] == "run")
    # Every program the runner starts in the VM by its full path, the reaper included.
    assert calls[run_at][-3:] == (REAPER_PYTHON, "-c", REAPER)
    assert calls[run_at + 1] == ("exec", "ema-paper-sandbox-71", "/bin/sh", "-c", DEFAULT_ENV)
    argv, _cwd = backend.shell_argv(71)
    assert argv[argv.index("ema-paper-sandbox-71") + 1 :][:4] == [
        "/usr/bin/setsid",
        "-w",
        "/bin/sh",
        "-c",
    ]
    # A start whose environment could not be made still gives a shell.
    env_code = 1
    await backend.ensure_running(backend._user(72))
    assert backend._user(72).started_at is not None


def test_the_default_environment_check_only_looks(tmp_path) -> None:
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
        # The real pgrep ran: the job was ended.
        assert await wait_ended(int(lines[-1]))
        after = await shell.run("echo ran; command -v pgrep", timeout=10)
        assert after.output.split()[0] == "ran"
        # The command itself still sees what it planted: the PATH is the VM's.
        assert after.output.split()[1] == f"{planted}/pgrep"
    finally:
        await shell.close()


def test_the_vm_sweep_repeats_until_nothing_but_the_shells_is_left(tmp_path) -> None:
    """The sweep against a fake process table: `ps` and `kill` on PATH read and change a
    file, and killing the forker spawns a child the first snapshot never saw."""
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
        docker_settings(tmp_path, sandbox_workspaces_dir="./workspaces")


async def test_an_eviction_that_left_the_vm_unstoppable_starts_no_shell(tmp_path) -> None:
    class Stuck(LocalBackend):
        async def _make_room(self, sandbox):
            evicting = len(sandbox.sessions) >= self.settings.sandbox_max_shells
            await super()._make_room(sandbox)
            if evicting:
                sandbox.unstoppable = True  # the evicted shell's command could not be ended

    backend = Stuck(local_settings(tmp_path, sandbox_max_shells=1))
    await backend.exec(71, "a", "true", timeout=10)
    with pytest.raises(RuntimeError):
        await backend.exec(71, "b", "true", timeout=10)
    assert "b" not in backend._user(71).sessions


async def test_a_vm_docker_cannot_inspect_is_unavailable_not_stopped(tmp_path, monkeypatch) -> None:
    answer = {"out": (1, "Cannot connect to the Docker daemon")}

    async def fake_docker(*args, **_):
        if args[0] == "inspect":
            if isinstance(answer["out"], Exception):
                raise answer["out"]
            return answer["out"]
        return 0, ""

    monkeypatch.setattr(backends, "_docker", fake_docker)
    backend = DockerBackend(docker_settings(tmp_path, sandbox_idle_seconds=0))
    # The daemon away, or an inspection that timed out: unknown, never a stopped VM the
    # dashboard would offer to start.
    with pytest.raises(RuntimeError):
        await backend.is_running(7)
    async with runner(backend) as http:
        assert (await http.get("/sandboxes/7")).status_code == 503
        # Docker's own "no such" is a positive absence: stopped.
        answer["out"] = (1, "Error: No such object: ema-paper-sandbox-7")
        assert (await http.get("/sandboxes/7")).json()["state"] == "stopped"
    # The idle sweep skips what it cannot ask about and goes on.
    answer["out"] = (1, "docker timed out")
    backend._user(7).last_used = 0
    assert await backend.stop_idle() == 0
    # Docker could not even be run (out of file descriptors): the sweep fails, and the
    # next one asks again and stops the VM it then finds running.
    answer["out"] = OSError(24, "Too many open files")
    with pytest.raises(OSError):
        await backend.stop_idle()
    answer["out"] = (0, "true\n")
    assert await backend.stop_idle() == 1


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

    backend = Stuck(local_settings(tmp_path, sandbox_docker_timeout_seconds=1))
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
    class Stuck(LocalBackend):
        gone = False

        def on_timeout(self, user_id):
            async def kill_inside(*, pid: int, locked: bool = False) -> None:
                self._user(user_id).unstoppable = True  # neither killed nor removed

            return kill_inside

        async def is_gone(self, user_id: int) -> bool:
            return self.gone

    backend = Stuck(local_settings(tmp_path, sandbox_docker_timeout_seconds=1))
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
    async with runner(backend) as http:
        assert (await http.post("/sandboxes/91/restart", json={"session": "s"})).json() == {
            "ok": True
        }
    await backend.stop(91)


async def test_a_command_past_the_cap_is_refused(tmp_path) -> None:
    """The cap is the runner's setting, not a constant in the request model
    (docs/sandbox.md § The runner)."""
    async with runner(LocalBackend(local_settings(tmp_path, sandbox_command_max_chars=16))) as http:
        refused = await http.post(
            "/sandboxes/9/exec", json={"session": "s", "command": "echo " + "x" * 20}
        )
        assert refused.status_code == 422
        allowed = await http.post("/sandboxes/9/exec", json={"session": "s", "command": "echo ok"})
        assert allowed.status_code == 200 and allowed.json()["output"].strip() == "ok"
