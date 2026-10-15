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
