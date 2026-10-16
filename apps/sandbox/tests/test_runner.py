"""The runner over the local backend: a bash process, no Docker, no network."""

from __future__ import annotations

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
    from sightread_sandbox.shell import ShellSession

    killed = []

    async def kill_inside() -> None:
        killed.append(True)

    shell = ShellSession(["bash", "--norc", "--noprofile"], max_bytes=1000, on_timeout=kill_inside)
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
        json={"session": "s", "command": f"kill -0 {pid} 2>/dev/null && echo alive || echo dead"},
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

    from sightread_sandbox.shell import ShellSession

    killed = []

    async def kill_inside() -> None:
        killed.append(True)

    shell = ShellSession(["bash", "--norc", "--noprofile"], max_bytes=1000, on_timeout=kill_inside)
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
