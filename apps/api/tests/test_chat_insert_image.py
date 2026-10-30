"""The chat agent's `insert_image` tool (docs/chat.md § Tools; docs/workspace.md § Inserted
images): an image file of the workspace — a chart the shell saved, a picture the reader
keeps in Files — kept as one of a document's inserted images the way the editor's upload
keeps one, and the line that shows it answered for the agent to write. No test reaches the
network (docs/testing.md § Cost safety)."""

from __future__ import annotations

import io
import json
import os
import re
from pathlib import Path

import httpx
import respx
from PIL import Image

from sightread.agents import chat as chat_agent
from sightread.config import Settings
from sightread.jobs import chat_runner
from sightread.workspace.documents import measure_storage
from sightread.workspace.layout import images_dir, user_root
from tests.conftest import PARSE_MODEL, TEST_MODEL_PRICES
from tests.test_chat import _run_queued_turn
from tests.test_chat_convert import _keep, _papers
from tests.test_chat_extensions import (
    chatting_client,
    new_conversation,
    one_call,
    send,
    stub_messages,
)
from tests.test_chat_web import tool_results, turn_credits
from tests.test_document_images import _image

DOCUMENT = "/workspace/files/Papers/paper.pdf"
DONE = ([{"type": "text", "text": "Done."}], "end_turn")
LINE = re.compile(r"!\[\]\(figures/(img-[0-9a-f]{16}\.(?:png|jpg))\)")
NO_DOCUMENT = "give the doc.md or notes.md of one of the user's documents."


def _noise(size: tuple[int, int] = (64, 64)) -> bytes:
    """A PNG that does not compress away: a few kilobytes, whatever the quota leaves."""
    buffer = io.BytesIO()
    Image.frombytes("RGB", size, os.urandom(size[0] * size[1] * 3)).save(buffer, format="PNG")
    return buffer.getvalue()


async def _client(make_client, tmp_path, **overrides) -> httpx.AsyncClient:
    return await chatting_client(
        make_client(workspaces_dir=str(tmp_path / "workspaces"), **overrides)
    )


async def _markdown(client: httpx.AsyncClient) -> str:
    """The seeded document's text; reading it writes its directory, as a turn finds it."""
    content = await client.get(f"/api/library/documents/{client.document_id}/content")
    assert content.status_code == 200, content.text
    return content.json()["markdown"]


async def _workspace(client: httpx.AsyncClient) -> Path:
    await _markdown(client)
    return user_root(client.app.state.settings.workspaces_dir, client.user_id)


async def _turn(client: httpx.AsyncClient, conversation_id: int, *calls: dict) -> list[dict]:
    """One turn of the given `insert_image` calls, one a round, then an answer."""
    stub_messages(
        [*(one_call(f"i{i}", "insert_image", call) for i, call in enumerate(calls)), DONE]
    )
    await send(client, conversation_id, text="Put the chart in my notes")
    await _run_queued_turn(client)
    return await tool_results(client, conversation_id)


def test_the_tool_is_declared_last_on_every_deployment() -> None:
    """docs/chat.md § Tools: after `convert_file` where there is one, with or without a
    shell or the web; not strict — two paths the tool checks itself (§ Upstream); a write of
    the turn, never a subagent's; its line in the prompt goes with it."""
    bare = Settings(model_prices_json=TEST_MODEL_PRICES)
    fullest = Settings(parse_model=PARSE_MODEL, model_prices_json=TEST_MODEL_PRICES)
    for shell in (True, False):
        for settings in (bare, fullest):
            assert chat_runner.turn_tools(shell, settings)[-1] is chat_agent.INSERT_IMAGE_TOOL
    tool = chat_agent.INSERT_IMAGE_TOOL
    assert "strict" not in tool
    assert tool["input_schema"]["required"] == ["image", "document"]
    assert tool["input_schema"]["additionalProperties"] is False
    assert "insert_image" in chat_runner.MUTATING_TOOLS
    assert "insert_image" not in chat_runner.UNDECLARABLE_TOOLS
    assert "insert_image" not in chat_agent.SUBAGENT_TOOL_NAMES
    rendered = chat_agent.render_system_prompt(
        chat_agent.DEFAULT_SYSTEM_PROMPT, None, tools=["view_page", "insert_image"]
    )
    assert "- insert_image:" in rendered and "/workspace/.cache/" in rendered
    without = chat_agent.render_system_prompt(
        chat_agent.DEFAULT_SYSTEM_PROMPT, None, tools=["view_page"]
    )
    assert "- insert_image:" not in without


@respx.mock
async def test_a_chart_and_a_kept_picture_become_images_of_the_document(
    make_client, tmp_path
) -> None:
    """docs/chat.md § Tools: a PNG the shell saved under `.cache` and a JPEG in Files are
    stored under their hash and copied into the document's `figures/`, as the upload keeps
    them; each call answers the line to write. The tool writes no text, leaves the agent's
    file where it was, and costs nothing beyond the turn's tokens."""
    client = await _client(make_client, tmp_path)
    settings = client.app.state.settings
    root = await _workspace(client)
    before = await _markdown(client)
    chart = _image("PNG")
    (root / ".cache").mkdir(exist_ok=True)
    (root / ".cache" / "loss.png").write_bytes(chart)
    await _keep(client, "photo.jpg", _image("JPEG"), "image/jpeg", await _papers(client))
    conversation_id = await new_conversation(client)
    first, second = await _turn(
        client,
        conversation_id,
        {"image": "/workspace/.cache/loss.png", "document": f"{DOCUMENT}/notes.md"},
        # A picture of the reader's, into the document named by its directory.
        {"image": "/workspace/files/Papers/photo.jpg", "document": DOCUMENT},
    )
    names = []
    for result, suffix in ((first, ".png"), (second, ".jpg")):
        assert result["is_error"] is False, result["output"]
        match = LINE.fullmatch(result["output"].rsplit("\n", 1)[-1])
        assert match and match.group(1).endswith(suffix), result["output"]
        names.append(match.group(1))
    assert first["output"].startswith(
        f"Kept /workspace/.cache/loss.png as an image of the document at {DOCUMENT}."
    )
    for name in names:
        kept = images_dir(root) / str(client.document_id) / name
        assert (root / "files" / "Papers" / "paper.pdf" / "figures" / name).read_bytes() == (
            kept.read_bytes()
        )
        served = await client.get(f"/api/library/documents/{client.document_id}/images/{name}")
        assert served.status_code == 200 and served.content == kept.read_bytes()
    # The agent's file stays; nothing staged is left behind; no text was written.
    assert (root / ".cache" / "loss.png").read_bytes() == chart
    assert (root / "files" / "Papers" / "photo.jpg").is_file()
    assert list(Path(settings.upload_dir).iterdir()) == []
    assert await _markdown(client) == before
    assert await turn_credits(client, conversation_id) == ("succeeded", 1)


@respx.mock
async def test_insert_image_refuses_what_it_cannot_keep(make_client, tmp_path) -> None:
    """docs/chat.md § Tools: a path outside /workspace, a missing file, a link, a file that
    is no image, and a document that is not one of the reader's — a folder, or a directory
    whose `.meta.json` the shell wrote to name a real document — each answer an error, and
    nothing is stored."""
    client = await _client(make_client, tmp_path)
    settings = client.app.state.settings
    root = await _workspace(client)
    cache = root / ".cache"
    (cache / "forged").mkdir(parents=True)
    (cache / "loss.png").write_bytes(_image("PNG"))
    (cache / "notes.txt").write_text("not a picture")
    (cache / "link.png").symlink_to(cache / "loss.png")
    (cache / "forged" / ".meta.json").write_text(json.dumps({"document_id": client.document_id}))
    chart = "/workspace/.cache/loss.png"
    conversation_id = await new_conversation(client)
    results = await _turn(
        client,
        conversation_id,
        # The VM's /tmp is the VM's alone: never a path the worker can read.
        {"image": "/tmp/loss.png", "document": DOCUMENT},  # noqa: S108
        {"image": chart, "document": ""},
        {"image": "/workspace/.cache/none.png", "document": DOCUMENT},
        {"image": "/workspace/.cache/link.png", "document": DOCUMENT},
        {"image": "/workspace/.cache/notes.txt", "document": DOCUMENT},
        {"image": chart, "document": "/workspace/files/Papers"},
        {"image": chart, "document": "/workspace/.cache/forged/doc.md"},
    )
    assert [result["is_error"] for result in results] == [True] * 7
    assert [result["output"] for result in results] == [
        "image: paths must be under /workspace",
        "document: a `path` is required",
        "/workspace/.cache/none.png does not exist",
        "/workspace/.cache/link.png is not a plain file",
        "The image was not kept: The file is not a PNG, JPEG, WebP, GIF or HEIC image",
        f"No document at /workspace/files/Papers: {NO_DOCUMENT}",
        f"No document at /workspace/.cache/forged: {NO_DOCUMENT}",
    ]
    assert not (images_dir(root) / str(client.document_id)).exists()
    assert not (root / "files" / "Papers" / "paper.pdf" / "figures").exists()
    assert list(Path(settings.upload_dir).iterdir()) == []


@respx.mock
async def test_an_image_past_its_cap_or_the_quota_is_refused(make_client, tmp_path) -> None:
    """docs/chat.md § Tools: the file is read at most `DOCUMENT_IMAGE_MAX_BYTES`, and the
    quota is checked on each image, as the upload checks it: the first kept image fills the
    room the plan left, and the next is refused as storage full."""
    client = await _client(make_client, tmp_path, document_image_max_bytes=64 * 1024)
    root = await _workspace(client)
    cache = root / ".cache"
    cache.mkdir(exist_ok=True)
    (cache / "big.png").write_bytes(_noise((256, 256)))
    (cache / "first.png").write_bytes(_noise())
    (cache / "second.png").write_bytes(_noise())
    conversation_id = await new_conversation(client)
    async with client.app.state.sessionmaker() as db:
        used = (await measure_storage(db, client.app.state.settings, client.user_id, root)).total
    # The same workspace and user, on a plan with room for a few bytes more.
    tight = make_client(
        workspaces_dir=str(tmp_path / "workspaces"),
        document_image_max_bytes=64 * 1024,
        free_storage_bytes=used + 50,
    )
    stub_messages(
        [
            one_call(
                "i0", "insert_image", {"image": "/workspace/.cache/big.png", "document": DOCUMENT}
            ),
            one_call(
                "i1", "insert_image", {"image": "/workspace/.cache/first.png", "document": DOCUMENT}
            ),
            one_call(
                "i2",
                "insert_image",
                {"image": "/workspace/.cache/second.png", "document": DOCUMENT},
            ),
            DONE,
        ]
    )
    await send(client, conversation_id, text="Put the charts in")
    await _run_queued_turn(tight)
    big, first, second = await tool_results(client, conversation_id)
    assert big["is_error"] is True
    assert big["output"] == (
        "/workspace/.cache/big.png is larger than an inserted image may be (65,536 bytes)"
    )
    assert first["is_error"] is False, first["output"]
    assert second["is_error"] is True
    assert second["output"] == "The user's storage is full; the image was not kept."
    kept = sorted(entry.name for entry in (images_dir(root) / str(client.document_id)).iterdir())
    assert kept == [LINE.fullmatch(first["output"].rsplit("\n", 1)[-1]).group(1)]
