"""The chat agent's skills, subagents, replies and the reader's own prompt (docs/chat.md
§ System prompt, § Skills, § Subagents, § What a user message carries; docs/api.md): the
routes, and the turn running them against a stubbed Messages stream. No test reaches the
network (docs/testing.md § Cost safety)."""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest
import respx
from httpx import AsyncClient
from sqlalchemy import select

from sightread.agents import chat as chat_agent
from sightread.agents import skills as skill_defs
from sightread.agents import subagents as subagent_defs
from sightread.db.models import Message, Skill, UsageLog
from sightread.jobs import chat_runner, chat_subagent, chat_tools
from sightread.jobs.queue import claim_next_job
from sightread.jobs.runner import run_job
from tests.conftest import CSRF_HEADERS, KANO_CHAT_URL
from tests.test_chat import MESSAGES_URL, _run_queued_turn, _seed_document, sse

DOC_PATH = "/workspace/files/Papers/paper.pdf"
SUBAGENT_PREAMBLE_START = "You are a subagent"


def streamed(blocks: list[dict], stop_reason: str, *, model: str = "claude-opus-5-5") -> bytes:
    """A Messages stream of text, thinking and tool_use blocks."""
    events: list[dict] = [
        {"type": "message_start", "message": {"model": model, "usage": {"input_tokens": 100}}}
    ]
    for index, block in enumerate(blocks):
        if block["type"] == "thinking":
            start = {"type": "thinking", "thinking": ""}
            deltas = [
                {"type": "thinking_delta", "thinking": block["thinking"]},
                {"type": "signature_delta", "signature": "sig"},
            ]
        elif block["type"] == "text":
            start = {"type": "text", "text": ""}
            deltas = [{"type": "text_delta", "text": block["text"]}]
        else:
            start = {"type": "tool_use", "id": block["id"], "name": block["name"], "input": {}}
            deltas = [{"type": "input_json_delta", "partial_json": json.dumps(block["input"])}]
        events.append({"type": "content_block_start", "index": index, "content_block": start})
        events += [{"type": "content_block_delta", "index": index, "delta": d} for d in deltas]
        events.append({"type": "content_block_stop", "index": index})
    events += [
        {
            "type": "message_delta",
            "delta": {"stop_reason": stop_reason},
            "usage": {"output_tokens": 5},
        },
        {"type": "message_stop"},
    ]
    return sse(*events)


def stub_messages(main: list, sub: list | None = None) -> tuple[list[dict], list[dict]]:
    """Answer the turn's calls from `main` and a subagent's from `sub`, each `(blocks,
    stop_reason)` in order, on the model each request asked for; the requests are kept."""
    main_answers, sub_answers = iter(main), iter(sub or [])
    main_requests: list[dict] = []
    sub_requests: list[dict] = []

    def answer(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if body["system"][0]["text"].startswith(SUBAGENT_PREAMBLE_START):
            sub_requests.append(body)
            blocks, stop = next(sub_answers)
        else:
            main_requests.append(body)
            blocks, stop = next(main_answers)
        return httpx.Response(200, content=streamed(blocks, stop, model=body["model"]))

    respx.post(MESSAGES_URL).mock(side_effect=answer)
    return main_requests, sub_requests


def one_call(call_id: str, name: str, tool_input: dict) -> tuple[list[dict], str]:
    """A round of one tool call. The tests make one call a round: SQLite takes the events'
    sequence numbers without the row lock PostgreSQL holds (chat_events.append), so calls
    finishing together would collide on the fallback database only."""
    return [{"type": "tool_use", "id": call_id, "name": name, "input": tool_input}], "tool_use"


async def chatting_client(client: AsyncClient) -> AsyncClient:
    """Signed in, Kano Proxy connected, a model chosen for the chat agent, one document."""
    await client.post("/api/auth/dev-login", headers=CSRF_HEADERS)
    user_id = (await client.get("/api/me")).json()["user"]["id"]
    chosen = await client.put(
        "/api/agents/chat/settings",
        json={"model": "claude-code/claude-opus-5-5"},
        headers=CSRF_HEADERS,
    )
    assert chosen.status_code == 200, chosen.text
    client.document_id = await _seed_document(client, user_id)
    client.user_id = user_id
    return client


@pytest.fixture
async def chat_client(make_client, tmp_path) -> AsyncClient:
    return await chatting_client(make_client(workspaces_dir=str(tmp_path / "workspaces")))


async def new_conversation(client: AsyncClient) -> int:
    created = await client.post(
        f"/api/library/documents/{client.document_id}/conversations", headers=CSRF_HEADERS
    )
    assert created.status_code == 201, created.text
    return created.json()["id"]


async def send(client: AsyncClient, conversation_id: int, **body) -> httpx.Response:
    return await client.post(
        f"/api/conversations/{conversation_id}/messages", json=body, headers=CSRF_HEADERS
    )


async def replayed_events(client: AsyncClient, conversation_id: int) -> list[tuple[str, dict]]:
    async with client.stream(
        "GET", f"/api/conversations/{conversation_id}/events?after=0"
    ) as stream:
        body = (await stream.aread()).decode()
    events: list[tuple[str, dict]] = []
    name = None
    for line in body.splitlines():
        if line.startswith("event: "):
            name = line[len("event: ") :]
        elif line.startswith("data: ") and name is not None:
            events.append((name, json.loads(line[len("data: ") :])))
            name = None
    return events


def fake_pages(monkeypatch) -> list[int]:
    """`view_page` without a source: the page as a line of text, each call noted."""
    seen: list[int] = []

    async def view_page(paths, tool_input, **kwargs):
        seen.append(tool_input["page"])
        return chat_tools.ToolOutcome(f"Page {tool_input['page']} shows Greedy 3.45.")

    monkeypatch.setattr(chat_tools, "view_page", view_page)
    return seen


# --- skills -----------------------------------------------------------------------------


async def test_the_skills_are_the_builtins_then_the_readers_own(signed_in) -> None:
    listed = (await signed_in.get("/api/skills")).json()["skills"]
    assert [skill["name"] for skill in listed] == ["check-figures", "fix-math", "translate"]
    check = listed[0]
    assert check["builtin"] is True and check["modified"] is False
    assert check["user_invocable"] is True and check["model_invocable"] is False
    assert check["default"] == {
        field: getattr(skill_defs.CHECK_FIGURES, field) for field in skill_defs.SKILL_FIELDS
    }

    created = await signed_in.post(
        "/api/skills",
        json={
            "name": "zh-summary",
            "description": "Add a Chinese summary after each section",
            "argument_hint": "[範圍]",
            "body": "Summarize $ARGUMENTS in Chinese.",
        },
        headers=CSRF_HEADERS,
    )
    assert created.status_code == 201, created.text
    assert created.json() == {
        "name": "zh-summary",
        "description": "Add a Chinese summary after each section",
        "argument_hint": "[範圍]",
        "body": "Summarize $ARGUMENTS in Chinese.",
        "user_invocable": True,
        "model_invocable": True,
        "builtin": False,
        "modified": False,
        "default": None,
    }
    # A name taken, or a built-in's, is never free (docs/chat.md § Skills).
    for taken in ("zh-summary", "fix-math"):
        again = await signed_in.post(
            "/api/skills", json={"name": taken, "body": "x"}, headers=CSRF_HEADERS
        )
        assert again.status_code == 409, taken
    for bad in ("Has-Caps", "-dash-first", "x" * 65, "two words"):
        refused = await signed_in.post(
            "/api/skills", json={"name": bad, "body": "x"}, headers=CSRF_HEADERS
        )
        assert refused.status_code == 400, bad
    empty = await signed_in.post(
        "/api/skills", json={"name": "empty", "body": "   "}, headers=CSRF_HEADERS
    )
    assert empty.status_code == 400
    listed = (await signed_in.get("/api/skills")).json()["skills"]
    assert [skill["name"] for skill in listed] == [
        "check-figures",
        "fix-math",
        "translate",
        "zh-summary",
    ]


async def test_a_builtin_skill_is_edited_and_restored_never_renamed_or_deleted(
    signed_in,
) -> None:
    edited = await signed_in.put(
        "/api/skills/fix-math",
        json={"model_invocable": False, "body": "Only page 1."},
        headers=CSRF_HEADERS,
    )
    assert edited.status_code == 200, edited.text
    assert edited.json()["modified"] is True
    assert edited.json()["model_invocable"] is False and edited.json()["body"] == "Only page 1."
    assert edited.json()["description"] == skill_defs.FIX_MATH.description
    assert edited.json()["default"]["body"] == skill_defs.FIX_MATH.body
    renamed = await signed_in.put(
        "/api/skills/fix-math", json={"name": "fix-maths"}, headers=CSRF_HEADERS
    )
    assert renamed.status_code == 400

    # An edit back to the shipped values is no edit: nothing is kept, so a later default
    # reaches it.
    back = await signed_in.put(
        "/api/skills/fix-math",
        json={"model_invocable": True, "body": skill_defs.FIX_MATH.body},
        headers=CSRF_HEADERS,
    )
    assert back.json()["modified"] is False
    async with signed_in.app.state.sessionmaker() as db:
        assert (await db.execute(select(Skill))).scalars().all() == []

    # Restore default deletes the edit; the built-in stays, and stays first.
    await signed_in.put(
        "/api/skills/check-figures", json={"description": "Captions only"}, headers=CSRF_HEADERS
    )
    restored = await signed_in.delete("/api/skills/check-figures", headers=CSRF_HEADERS)
    assert restored.status_code == 204
    check = (await signed_in.get("/api/skills")).json()["skills"][0]
    assert check["name"] == "check-figures" and check["modified"] is False
    assert check["description"] == skill_defs.CHECK_FIGURES.description
    again = await signed_in.delete("/api/skills/check-figures", headers=CSRF_HEADERS)
    assert again.status_code == 204
    assert (await signed_in.delete("/api/skills/nobody", headers=CSRF_HEADERS)).status_code == 404
    missing = await signed_in.put("/api/skills/nobody", json={"body": "x"}, headers=CSRF_HEADERS)
    assert missing.status_code == 404


async def test_a_readers_own_skill_is_renamed_and_deleted(signed_in) -> None:
    for name in ("draft", "other"):
        created = await signed_in.post(
            "/api/skills", json={"name": name, "body": f"{name} body"}, headers=CSRF_HEADERS
        )
        assert created.status_code == 201
    renamed = await signed_in.put(
        "/api/skills/draft", json={"name": "final", "user_invocable": False}, headers=CSRF_HEADERS
    )
    assert renamed.status_code == 200, renamed.text
    assert renamed.json()["name"] == "final" and renamed.json()["user_invocable"] is False
    assert renamed.json()["body"] == "draft body"
    for taken in ("other", "check-figures"):
        clash = await signed_in.put("/api/skills/final", json={"name": taken}, headers=CSRF_HEADERS)
        assert clash.status_code == 409, taken
    assert (await signed_in.delete("/api/skills/final", headers=CSRF_HEADERS)).status_code == 204
    assert (await signed_in.delete("/api/skills/final", headers=CSRF_HEADERS)).status_code == 404
    names = [skill["name"] for skill in (await signed_in.get("/api/skills")).json()["skills"]]
    assert names == ["check-figures", "fix-math", "translate", "other"]


def test_a_readers_own_skill_under_a_later_builtins_name_reads_as_their_edit() -> None:
    mine = skill_defs.SkillDefinition(
        name="translate", description="Mine", argument_hint="", body="My own steps."
    )
    resolved = {skill.name: skill for skill in skill_defs.resolve_skills([mine])}
    translate = resolved["translate"]
    assert translate.builtin and translate.modified
    assert translate.body == "My own steps." and translate.default is skill_defs.TRANSLATE


def test_translate_takes_a_language_and_the_readers_instructions() -> None:
    rendered = skill_defs.render_skill(skill_defs.TRANSLATE.body, " zh-TW 術語保留英文 ")
    assert "Arguments: zh-TW 術語保留英文\n" in rendered
    assert "$ARGUMENTS" not in rendered
    # The translators are the subagent the skill names, and they write into the document
    # themselves; the agent proofreads after them, and no checker subagent is started.
    assert "`translator`" in rendered and "translation-checker" not in rendered
    assert "Proofread once every translator has reported" in rendered
    # Without a language, the translation language the turn names, else the locale; the
    # reader's term memories go to every translator (docs/chat.md § Translation).
    assert "translation language named in the message's context" in rendered
    assert "into its reader locale" in rendered
    assert "Pass the user's term memories (the Terms of your memory section)" in rendered
    assert subagent_defs.BUILTIN_SUBAGENTS == (subagent_defs.TRANSLATOR,)
    assert subagent_defs.TRANSLATOR.writes is True
    assert subagent_defs.TRANSLATOR.tools == ("str_replace_based_edit_tool", "bash")


async def test_skills_and_subagents_are_capped(make_client) -> None:
    client = make_client(chat_skills_max=1, chat_subagents_max=1, system_prompt_max_chars=2000)
    await client.post("/api/auth/dev-login", headers=CSRF_HEADERS)
    # An edit of a built-in is not one of the reader's own: it does not count.
    await client.put("/api/skills/fix-math", json={"description": "Mine"}, headers=CSRF_HEADERS)
    await client.put(
        "/api/subagents/translator", json={"description": "Mine"}, headers=CSRF_HEADERS
    )
    for route, field in (("/api/skills", "body"), ("/api/subagents", "prompt")):
        first = await client.post(route, json={"name": "one", field: "x"}, headers=CSRF_HEADERS)
        assert first.status_code == 201, first.text
        second = await client.post(route, json={"name": "two", field: "x"}, headers=CSRF_HEADERS)
        assert second.status_code == 400
        too_long = await client.put(f"{route}/one", json={field: "x" * 2001}, headers=CSRF_HEADERS)
        assert too_long.status_code == 400
        described = await client.put(
            f"{route}/one", json={"description": "x" * 1025}, headers=CSRF_HEADERS
        )
        assert described.status_code == 400


# --- subagents --------------------------------------------------------------------------


async def test_subagents_follow_the_skills_contract(signed_in) -> None:
    listed = (await signed_in.get("/api/subagents")).json()["subagents"]
    assert [subagent["name"] for subagent in listed] == ["translator"]
    assert listed[0]["tools"] == ["str_replace_based_edit_tool", "bash"]
    assert listed[0]["writes"] is True
    assert listed[0]["model"] is None and listed[0]["builtin"] is True

    created = await signed_in.post(
        "/api/subagents",
        json={
            "name": "tables",
            "description": "Checks tables",
            "model": "claude-code/claude-haiku-5-5",
            "tools": ["str_replace_based_edit_tool", "view_page", "view_page"],
            "prompt": "Check every table.",
        },
        headers=CSRF_HEADERS,
    )
    assert created.status_code == 201, created.text
    # The tools in their fixed order, each once.
    assert created.json()["tools"] == ["view_page", "str_replace_based_edit_tool"]
    assert created.json()["model"] == "claude-code/claude-haiku-5-5"
    assert created.json()["default"] is None
    defaulted = await signed_in.post(
        "/api/subagents", json={"name": "all-tools", "prompt": "Read."}, headers=CSRF_HEADERS
    )
    assert defaulted.json()["tools"] == ["view_page", "view_figure", "str_replace_based_edit_tool"]
    assert defaulted.json()["writes"] is False
    # The shell only for one that may change files: given with `writes`, refused without.
    shell = await signed_in.put(
        "/api/subagents/all-tools",
        json={"writes": True, "tools": ["bash", "str_replace_based_edit_tool"]},
        headers=CSRF_HEADERS,
    )
    assert shell.json()["writes"] is True
    assert shell.json()["tools"] == ["str_replace_based_edit_tool", "bash"]
    unshelled = await signed_in.put(
        "/api/subagents/all-tools", json={"writes": False}, headers=CSRF_HEADERS
    )
    assert unshelled.status_code == 400
    back = await signed_in.put(
        "/api/subagents/all-tools",
        json={
            "writes": False,
            "tools": ["view_page", "view_figure", "str_replace_based_edit_tool"],
        },
        headers=CSRF_HEADERS,
    )
    assert back.json()["writes"] is False
    for body in (
        {"name": "shell", "prompt": "x", "tools": ["bash"]},
        {"name": "nesting", "prompt": "x", "tools": ["subagent"]},
        {"name": "none", "prompt": "x", "tools": []},
        {"name": "long", "prompt": "x", "model": "m" * 256},
    ):
        refused = await signed_in.post("/api/subagents", json=body, headers=CSRF_HEADERS)
        assert refused.status_code == 400, body
    taken = await signed_in.post(
        "/api/subagents", json={"name": "translator", "prompt": "x"}, headers=CSRF_HEADERS
    )
    assert taken.status_code == 409

    # A built-in: edited under its name, `model: null` kept as a value, restored by delete.
    # Any model of the catalog, Claude or not.
    edited = await signed_in.put(
        "/api/subagents/translator", json={"model": "codex/gpt-5.5"}, headers=CSRF_HEADERS
    )
    assert edited.json()["model"] == "codex/gpt-5.5"
    assert edited.json()["modified"] is True
    cleared = await signed_in.put(
        "/api/subagents/translator", json={"model": None}, headers=CSRF_HEADERS
    )
    assert cleared.json()["model"] is None and cleared.json()["modified"] is False
    # The shipped tools named in another order are still the shipped tools.
    same = await signed_in.put(
        "/api/subagents/translator",
        json={"tools": ["bash", "str_replace_based_edit_tool"]},
        headers=CSRF_HEADERS,
    )
    assert same.json()["modified"] is False
    renamed = await signed_in.put(
        "/api/subagents/translator", json={"name": "translators"}, headers=CSRF_HEADERS
    )
    assert renamed.status_code == 400
    assert (
        await signed_in.delete("/api/subagents/translator", headers=CSRF_HEADERS)
    ).status_code == 204
    names = [entry["name"] for entry in (await signed_in.get("/api/subagents")).json()["subagents"]]
    assert names == ["translator", "all-tools", "tables"]

    moved = await signed_in.put(
        "/api/subagents/tables", json={"name": "grids", "model": None}, headers=CSRF_HEADERS
    )
    assert moved.json()["name"] == "grids" and moved.json()["model"] is None
    assert (await signed_in.delete("/api/subagents/grids", headers=CSRF_HEADERS)).status_code == 204
    assert (await signed_in.delete("/api/subagents/grids", headers=CSRF_HEADERS)).status_code == 404


# --- the agent's own prompt -------------------------------------------------------------


async def test_the_agent_prompt_replaces_the_default_and_excludes_a_preset(signed_in) -> None:
    agents = (await signed_in.get("/api/agents")).json()["agents"]
    chat = next(agent for agent in agents if agent["name"] == "chat")
    parse = next(agent for agent in agents if agent["name"] == "parse")
    assert chat["prompt_tokens"] == [
        "{document_path}",
        "{page_count}",
        "{bbox_format}",
        "{parse_model}",
        "{network}",
    ]
    assert parse["prompt_tokens"] == ["{page}", "{bbox_format}", "{bbox_instructions}"]
    assert chat["settings"]["prompt"] is None

    preset = (
        await signed_in.post(
            "/api/prompts",
            json={"agent": "chat", "name": "Terse", "text": "Be terse."},
            headers=CSRF_HEADERS,
        )
    ).json()
    selected = await signed_in.put(
        "/api/agents/chat/settings", json={"prompt_preset_id": preset["id"]}, headers=CSRF_HEADERS
    )
    assert selected.json()["prompt_preset_id"] == preset["id"]

    # Saving a prompt clears the preset, and selecting a preset clears the prompt: one of
    # the two is ever in force (docs/chat.md § System prompt).
    own = await signed_in.put(
        "/api/agents/chat/settings",
        json={"prompt": "  My agent for {document_path}.  "},
        headers=CSRF_HEADERS,
    )
    assert own.status_code == 200, own.text
    assert own.json()["prompt"] == "My agent for {document_path}."
    assert own.json()["prompt_preset_id"] is None
    back = await signed_in.put(
        "/api/agents/chat/settings", json={"prompt_preset_id": preset["id"]}, headers=CSRF_HEADERS
    )
    assert back.json()["prompt"] is None and back.json()["prompt_preset_id"] == preset["id"]
    both = await signed_in.put(
        "/api/agents/chat/settings",
        json={"prompt": "Mine", "prompt_preset_id": preset["id"]},
        headers=CSRF_HEADERS,
    )
    assert both.status_code == 400
    together = await signed_in.put(
        "/api/agents/chat/settings",
        json={"prompt": "Mine", "prompt_preset_id": None},
        headers=CSRF_HEADERS,
    )
    assert together.json()["prompt"] == "Mine" and together.json()["prompt_preset_id"] is None
    # The default, or nothing, is stored as no prompt, so a later default reaches it.
    for text in (chat["default_prompt"], "   "):
        reset = await signed_in.put(
            "/api/agents/chat/settings", json={"prompt": text}, headers=CSRF_HEADERS
        )
        assert reset.status_code == 200, reset.text
        assert reset.json()["prompt"] is None
    cap = signed_in.app.state.settings.system_prompt_max_chars
    too_long = await signed_in.put(
        "/api/agents/chat/settings", json={"prompt": "x" * (cap + 1)}, headers=CSRF_HEADERS
    )
    assert too_long.status_code == 400


@respx.mock
async def test_a_parse_runs_on_the_readers_own_prompt(make_client, sessionmaker, documents) -> None:
    upstream = respx.post(KANO_CHAT_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": "# Page one"}}],
                "usage": {"prompt_tokens": 8, "completion_tokens": 9},
            },
        )
    )
    client = make_client()
    await client.post("/api/auth/dev-login", headers=CSRF_HEADERS)
    saved = await client.put(
        "/api/agents/parse/settings",
        json={"prompt": "Transcribe page {page} as plain text."},
        headers=CSRF_HEADERS,
    )
    assert saved.json()["prompt"] == "Transcribe page {page} as plain text."
    key = await client.post("/api/keys", json={"name": "test"}, headers=CSRF_HEADERS)
    client.headers["Authorization"] = f"Bearer {key.json()['key']}"
    accepted = await client.post(
        "/v1/parse", files={"file": ("tiny.png", documents["png"].read_bytes(), "image/png")}
    )
    assert accepted.status_code == 202, accepted.text

    settings = client.app.state.settings
    async with sessionmaker() as db:
        claimed = await claim_next_job(db, settings.max_jobs_per_user)
    await run_job(sessionmaker, settings, claimed, asyncio.Semaphore(2))
    sent = json.dumps(json.loads(upstream.calls[0].request.content))
    # The reader's prompt replaced the template whole, its tokens substituted.
    assert "Transcribe page 1 as plain text." in sent


# --- the turn ---------------------------------------------------------------------------


@respx.mock
async def test_a_turn_runs_on_the_readers_prompt_with_the_catalogue_before_memory(
    chat_client,
) -> None:
    client = chat_client
    await client.put(
        "/api/agents/chat/settings",
        json={"prompt": "Custom agent for {document_path}, {page_count} pages."},
        headers=CSRF_HEADERS,
    )
    for body in (
        {"name": "zh-summary", "description": "Add a Chinese summary", "body": "Summarize."},
        {
            "name": "mine-only",
            "description": "Composer only",
            "body": "x",
            "model_invocable": False,
        },
    ):
        assert (
            await client.post("/api/skills", json=body, headers=CSRF_HEADERS)
        ).status_code == 201
    main, _ = stub_messages([([{"type": "text", "text": "Hi."}], "end_turn")])
    conversation_id = await new_conversation(client)
    assert (await send(client, conversation_id, text="hello")).status_code == 202
    await _run_queued_turn(client)

    system = main[0]["system"]
    assert len(system) == 3
    assert system[0]["text"] == f"Custom agent for {DOC_PATH}, 1 pages."
    assert "cache_control" not in system[0] and "cache_control" not in system[1]
    # The memory block is last, rules and all, though the reader replaced the prompt.
    assert system[2]["text"].startswith("# Memory\n")
    assert system[2]["cache_control"] == {"type": "ephemeral", "ttl": "1h"}
    catalogue = system[1]["text"]
    assert "- fix-math: " in catalogue and "- zh-summary: Add a Chinese summary" in catalogue
    # A skill the agent may not load is not listed: those are the reader's commands only.
    assert "check-figures" not in catalogue and "mine-only" not in catalogue
    assert "- translate: " not in catalogue
    assert "- translator (changes files itself): " in catalogue
    assert "- verifier" not in catalogue and "- reader" not in catalogue
    assert "- translation-checker" not in catalogue


@respx.mock
async def test_the_agent_loads_the_skills_it_may_and_no_other(chat_client) -> None:
    client = chat_client
    main, _ = stub_messages(
        [
            one_call("s1", "skill", {"name": "fix-math", "arguments": "pages 2-3"}),
            one_call("s2", "skill", {"name": "check-figures", "arguments": ""}),
            one_call("s3", "subagent", {"name": "nobody", "task": "Anything."}),
            ([{"type": "text", "text": "Done."}], "end_turn"),
        ]
    )
    conversation_id = await new_conversation(client)
    await send(client, conversation_id, text="Fix the math")
    await _run_queued_turn(client)

    loaded, user_only, missing = (
        main[index]["messages"][2 * index]["content"][0] for index in (1, 2, 3)
    )
    assert loaded["tool_use_id"] == "s1" and "is_error" not in loaded
    assert loaded["content"].startswith("Skill fix-math")
    assert "Otherwise the pages given here: pages 2-3 (the whole document" in loaded["content"]
    # The reader's command only, and a subagent that is not there: errors to correct.
    assert user_only["is_error"] is True and missing["is_error"] is True


@respx.mock
async def test_a_command_runs_a_skill_the_reader_can_run(chat_client) -> None:
    client = chat_client
    main, _ = stub_messages([([{"type": "text", "text": "Checked."}], "end_turn")])
    await client.post(
        "/api/skills",
        json={"name": "agent-only", "body": "x", "user_invocable": False},
        headers=CSRF_HEADERS,
    )
    conversation_id = await new_conversation(client)
    for command in ("nobody", "agent-only"):
        refused = await send(client, conversation_id, command=command)
        assert refused.status_code == 400, command
    sent = await send(client, conversation_id, command="check-figures", text=" 1-3 ")
    assert sent.status_code == 202, sent.text
    await _run_queued_turn(client)

    content = main[0]["messages"][0]["content"]
    assert content[0] == {"type": "text", "text": "/check-figures 1-3"}
    assert content[1]["text"].startswith("The user ran the skill /check-figures.")
    assert "Otherwise the pages given here: 1-3 (the whole document" in content[1]["text"]
    # Sent with neither a selection nor sections, nothing narrows the pages it names.
    assert "Run it on" not in content[1]["text"]
    assert set(content[1]) == {"type", "text"}
    thread = (await client.get(f"/api/conversations/{conversation_id}")).json()
    # The thread shows the command, not the instructions the model read.
    assert thread["messages"][0]["content"] == [
        {"type": "text", "text": "/check-figures 1-3"},
        {"type": "text", "context": "skill", "skill": "check-figures"},
    ]
    assert thread["conversation"]["title"] == "/check-figures 1-3"


@respx.mock
async def test_a_command_keeps_to_the_selection_and_sections_it_is_sent_with(chat_client) -> None:
    client = chat_client
    main, _ = stub_messages([([{"type": "text", "text": "Done."}], "end_turn")] * 3)
    conversation_id = await new_conversation(client)
    for extra in (
        {"selection": "Greedy is 3.45."},
        {"sections": [{"heading": "3.2 Attention", "text": "## 3.2 Attention\n\nBody."}]},
        {
            "text": "zh-TW keep terms",
            "selection": "Greedy is 3.45.",
            "sections": [
                {"heading": "3.2 Attention", "text": "## 3.2 Attention\n\nBody."},
                {"heading": "4 Results", "text": None},
            ],
        },
    ):
        sent = await send(client, conversation_id, command="translate", **extra)
        assert sent.status_code == 202, sent.text
        await _run_queued_turn(client)

    lines = []
    for index in range(3):
        skill = main[index]["messages"][2 * index]["content"][-2]["text"]
        assert skill.startswith("The user ran the skill /translate.")
        lines.append(skill.splitlines()[-1])
    assert lines == [
        "Run it on the selection above only: the rest of the document is outside its range.",
        'Run it on the section "3.2 Attention" only: '
        "the rest of the document is outside its range.",
        'Run it on the selection above, the section "3.2 Attention" and the section "4 Results" '
        "only: the rest of the document is outside its range.",
    ]
    # The range names where; the arguments still say how.
    assert "Arguments: zh-TW keep terms" in main[2]["messages"][4]["content"][-2]["text"]


@respx.mock
async def test_a_reply_carries_the_passage_it_answers(chat_client) -> None:
    client = chat_client
    main, _ = stub_messages([([{"type": "text", "text": "Because."}], "end_turn")])
    conversation_id = await new_conversation(client)
    too_long = await send(client, conversation_id, text="Why?", quote="x" * 16_001)
    assert too_long.status_code == 400
    nothing = await send(client, conversation_id, text="   ", quote="Greedy is 3.45.")
    assert nothing.status_code == 400
    sent = await send(client, conversation_id, text="Why this value?", quote=" Greedy is 3.45. ")
    assert sent.status_code == 202, sent.text
    await _run_queued_turn(client)

    content = main[0]["messages"][0]["content"]
    assert content[0] == {"type": "text", "text": "Why this value?"}
    assert content[1] == {
        "type": "text",
        "text": "The user is replying to this passage of your earlier answer:\n\nGreedy is 3.45.",
    }
    shown = (await client.get(f"/api/conversations/{conversation_id}")).json()["messages"][0]
    assert shown["content"] == [
        {"type": "text", "text": "Why this value?"},
        {"type": "text", "text": "Greedy is 3.45.", "context": "quote"},
    ]


@respx.mock
async def test_a_subagent_works_alone_and_hands_back_its_report(chat_client, monkeypatch) -> None:
    client = chat_client
    pages = fake_pages(monkeypatch)
    doc = f"{DOC_PATH}/doc.md"
    report = "Page 1: doc.md says 3450, the page shows 3.45."
    write = {"command": "str_replace", "path": doc, "old_str": "3450", "new_str": "3.45"}
    created = await client.post(
        "/api/subagents",
        json={"name": "checker", "description": "Checks pages", "prompt": "Check each page."},
        headers=CSRF_HEADERS,
    )
    assert created.status_code == 201, created.text
    main, sub = stub_messages(
        [
            (
                [
                    {"type": "text", "text": "Starting a check."},
                    {
                        "type": "tool_use",
                        "id": "call",
                        "name": "subagent",
                        "input": {"name": "checker", "task": "Check page 1 against doc.md."},
                    },
                ],
                "tool_use",
            ),
            ([{"type": "text", "text": "The checker found one mismatch."}], "end_turn"),
        ],
        [
            (
                [
                    {"type": "thinking", "thinking": "Open the page first."},
                    {"type": "tool_use", "id": "v1", "name": "view_page", "input": {"page": 1}},
                ],
                "tool_use",
            ),
            one_call("v2", "str_replace_based_edit_tool", {"command": "view", "path": doc}),
            one_call("w1", "str_replace_based_edit_tool", write),
            ([{"type": "text", "text": report}], "end_turn"),
        ],
    )
    conversation_id = await new_conversation(client)
    await send(client, conversation_id, text="Check page 1")
    await _run_queued_turn(client)

    thread = (await client.get(f"/api/conversations/{conversation_id}")).json()
    assert thread["turns"][0]["status"] == "succeeded", thread["turns"]
    # Its own context: the preamble and its prompt, the task its only message, its tools.
    first = sub[0]
    assert first["model"] == "claude-code/claude-opus-5-5"  # none of its own: the conversation's
    assert first["tools"] == [
        chat_agent.VIEW_PAGE_TOOL,
        chat_agent.VIEW_FIGURE_TOOL,
        chat_agent.SUBAGENT_EDITOR_TOOL,
    ]
    # Every tool carries a schema: a conversion to a model outside the passthrough drops a
    # tool without one, and a subagent may run on such a model (docs/chat.md § Subagents).
    assert all("input_schema" in tool and "type" not in tool for tool in first["tools"])
    assert first["messages"] == [
        {"role": "user", "content": [{"type": "text", "text": "Check page 1 against doc.md."}]}
    ]
    assert DOC_PATH in first["system"][0]["text"]
    assert first["system"][0]["text"].endswith("# Your instructions\n\nCheck each page.")
    assert f"{DOC_PATH}/doc.md" in first["system"][0]["text"]
    assert subagent_defs.READ_ONLY in first["system"][0]["text"]
    assert first["system"][0]["cache_control"] == {"type": "ephemeral", "ttl": "1h"}
    assert first["output_config"] == main[0]["output_config"]
    # Its history lives one run, under one prompt: nothing for the API to unbind.
    assert first["thinking"] == {"type": "adaptive", "display": "updates"}
    # It reads and never writes: the editor answers `view` alone.
    page, view, refused = (sub[index]["messages"][2 * index]["content"][0] for index in (1, 2, 3))
    assert page["tool_use_id"] == "v1" and page["content"] == "Page 1 shows Greedy 3.45."
    assert pages == [1]
    assert view["tool_use_id"] == "v2" and "| Greedy | 3450 |" in view["content"]
    assert refused["is_error"] is True and "only read" in refused["content"]
    markdown = (await client.get(f"/api/library/documents/{client.document_id}/content")).json()
    assert "| Greedy | 3450 |" in markdown["markdown"]

    # What the agent sees is the report, as the call's result, without the record.
    assert main[1]["messages"][2]["content"] == [
        {"type": "tool_result", "tool_use_id": "call", "content": report}
    ]
    # None of its messages enter the history; the stored result keeps the record.
    assert [message["role"] for message in thread["messages"]] == [
        "user",
        "assistant",
        "user",
        "assistant",
    ]
    record = thread["messages"][2]["content"][0]["subagent"]
    assert record["name"] == "checker" and record["model"] == "claude-code/claude-opus-5-5"
    assert record["tool_calls"] == 3 and isinstance(record["elapsed_ms"], int)
    assert record["steps"] == [
        {"type": "note", "text": "Open the page first."},
        {"type": "tool", "id": "v1", "name": "view_page", "input": {"page": 1}, "is_error": False},
        {
            "type": "tool",
            "id": "v2",
            "name": "str_replace_based_edit_tool",
            "input": {"command": "view", "path": doc},
            "is_error": False,
        },
        {
            "type": "tool",
            "id": "w1",
            "name": "str_replace_based_edit_tool",
            "input": write,
            "is_error": True,
        },
    ]

    # Its events name the call that started it, and its report streams as no turn text.
    events = await replayed_events(client, conversation_id)
    nested = [name for name, data in events if data.get("parent") == "call"]
    assert set(nested) == {"thinking", "tool_use", "tool_result"}
    assert nested.count("tool_use") == 3 and nested.count("tool_result") == 3
    texts = "".join(data["text"] for name, data in events if name == "text_delta")
    assert report not in texts and "Starting a check." in texts
    done = next(data for name, data in events if name == "tool_result" and data["id"] == "call")
    assert done["subagent"] == record and "parent" not in done
    # Billed like every other call, on the turn.
    async with client.app.state.sessionmaker() as db:
        rows = (await db.execute(select(UsageLog).where(UsageLog.agent == "chat"))).scalars().all()
    assert len(rows) == 6 and all(row.turn_id is not None for row in rows)


@respx.mock
async def test_a_subagent_that_writes_edits_the_document_itself(chat_client) -> None:
    client = chat_client
    doc = f"{DOC_PATH}/doc.md"
    write = {"command": "str_replace", "path": doc, "old_str": "3450", "new_str": "3.45"}
    main, sub = stub_messages(
        [
            (
                [
                    {
                        "type": "tool_use",
                        "id": "call",
                        "name": "subagent",
                        "input": {"name": "translator", "task": "Pages 1-1 into zh-TW."},
                    }
                ],
                "tool_use",
            ),
            ([{"type": "text", "text": "Translated."}], "end_turn"),
        ],
        [
            one_call("w1", "str_replace_based_edit_tool", write),
            ([{"type": "text", "text": "1 block. Greedy → 貪婪"}], "end_turn"),
        ],
    )
    conversation_id = await new_conversation(client)
    await send(client, conversation_id, text="Translate page 1")
    await _run_queued_turn(client)

    thread = (await client.get(f"/api/conversations/{conversation_id}")).json()
    assert thread["turns"][0]["status"] == "succeeded", thread["turns"]
    # Told it may change files; no sandbox here, so no shell is offered.
    first = sub[0]
    assert first["tools"] == [chat_agent.SUBAGENT_EDITOR_TOOL]
    assert subagent_defs.WRITES in first["system"][0]["text"]
    assert "The shell is" not in first["system"][0]["text"]
    # Its write is a revision of the document, like the agent's own.
    written = sub[1]["messages"][2]["content"][0]
    assert written["tool_use_id"] == "w1" and "is_error" not in written
    markdown = (await client.get(f"/api/library/documents/{client.document_id}/content")).json()
    assert "| Greedy | 3.45 |" in markdown["markdown"]
    # Its `edit` names the call that started it: the panel draws the diff card in its card.
    events = await replayed_events(client, conversation_id)
    edit = next(data for name, data in events if name == "edit")
    assert edit["tool_use_id"] == "w1" and edit["parent"] == "call" and edit["commit"]
    record = thread["messages"][2]["content"][0]["subagent"]
    assert record["steps"] == [
        {
            "type": "tool",
            "id": "w1",
            "name": "str_replace_based_edit_tool",
            "input": write,
            "is_error": False,
        }
    ]


@respx.mock
async def test_a_subagent_runs_on_its_own_model_and_stops_at_its_call_cap(
    make_client, tmp_path, monkeypatch
) -> None:
    client = await chatting_client(
        make_client(workspaces_dir=str(tmp_path / "workspaces"), chat_subagent_max_tool_calls=1)
    )
    pages = fake_pages(monkeypatch)
    created = await client.post(
        "/api/subagents",
        json={
            "name": "quick",
            "description": "Fast reads",
            "model": "claude-code/claude-haiku-5-5",
            "tools": ["view_page"],
            "prompt": "Read fast.",
        },
        headers=CSRF_HEADERS,
    )
    assert created.status_code == 201, created.text
    main, sub = stub_messages(
        [
            (
                [
                    {
                        "type": "tool_use",
                        "id": "call",
                        "name": "subagent",
                        "input": {"name": "quick", "task": "Read pages 1 and 2."},
                    }
                ],
                "tool_use",
            ),
            ([{"type": "text", "text": "Noted."}], "end_turn"),
        ],
        [
            (
                [
                    {"type": "text", "text": "Page 1 looks fine so far."},
                    {"type": "tool_use", "id": "p1", "name": "view_page", "input": {"page": 1}},
                    {"type": "tool_use", "id": "p2", "name": "view_page", "input": {"page": 2}},
                ],
                "tool_use",
            )
        ],
    )
    conversation_id = await new_conversation(client)
    await send(client, conversation_id, text="Read")
    await _run_queued_turn(client)

    assert sub[0]["model"] == "claude-code/claude-haiku-5-5"
    assert [tool["name"] for tool in sub[0]["tools"]] == ["view_page"]
    # Past the cap nothing more runs: no read, no further call.
    assert len(sub) == 1 and pages == []
    result = main[1]["messages"][2]["content"][0]
    assert "is_error" not in result
    assert result["content"] == (
        "Page 1 looks fine so far.\n\n"
        "[The subagent made more than 1 tool calls and was stopped here.]"
    )
    thread = (await client.get(f"/api/conversations/{conversation_id}")).json()
    record = thread["messages"][2]["content"][0]["subagent"]
    assert record["model"] == "claude-code/claude-haiku-5-5" and record["tool_calls"] == 2
    assert [step["is_error"] for step in record["steps"]] == [True, True]
    async with client.app.state.sessionmaker() as db:
        models = sorted(
            (await db.execute(select(UsageLog.model).where(UsageLog.agent == "chat"))).scalars()
        )
    assert models == [
        "claude-code/claude-haiku-5-5",
        "claude-code/claude-opus-5-5",
        "claude-code/claude-opus-5-5",
    ]


@respx.mock
async def test_a_subagent_on_an_unpriced_model_never_calls_it(chat_client) -> None:
    """docs/billing.md § Models: a model nobody priced must never run for free — the
    subagent answers an error result before its first call, and the turn goes on."""
    created = await chat_client.post(
        "/api/subagents",
        json={
            "name": "quick",
            "description": "Fast reads",
            "model": "vendor/unpriced-model",
            "tools": ["view_page"],
            "prompt": "Read fast.",
        },
        headers=CSRF_HEADERS,
    )
    assert created.status_code == 201, created.text
    main, sub = stub_messages(
        [
            one_call("call", "subagent", {"name": "quick", "task": "Read page 1."}),
            ([{"type": "text", "text": "Noted."}], "end_turn"),
        ],
        [([{"type": "text", "text": "Never sent."}], "end_turn")],
    )
    conversation_id = await new_conversation(chat_client)
    await send(chat_client, conversation_id, text="Read")
    await _run_queued_turn(chat_client)

    assert (len(main), len(sub)) == (2, 0)
    result = main[1]["messages"][2]["content"][0]
    assert result["is_error"] and "vendor/unpriced-model is not priced" in result["content"]
    thread = (await chat_client.get(f"/api/conversations/{conversation_id}")).json()
    [turn] = thread["turns"]
    assert turn["status"] == "succeeded"
    async with chat_client.app.state.sessionmaker() as db:
        models = (
            await db.execute(select(UsageLog.model).where(UsageLog.agent == "chat"))
        ).scalars()
        assert set(models) == {"claude-code/claude-opus-5-5"}


@respx.mock
async def test_a_subagent_that_spends_the_balance_stops_and_the_turn_answers(
    make_client, tmp_path, monkeypatch
) -> None:
    """docs/billing.md § A chat turn: once a subagent's call spends the balance, its tool
    calls past the allowance are not run, it reports what it has, and the turn's last call
    goes with no tool left to it."""
    # Each stubbed call carries 105 tokens at US$1 per million: 3 credits for the first
    # at US$0.00005 a credit, 5 for two, 7 for three. Four granted: the turn's own call
    # leaves one, the subagent's call overdraws.
    client = await chatting_client(
        make_client(
            workspaces_dir=str(tmp_path / "workspaces"),
            free_credits_per_period=4,
            credit_usd="0.00005",
            chat_overdraft_max_tool_calls=0,
        )
    )
    pages = fake_pages(monkeypatch)
    created = await client.post(
        "/api/subagents",
        json={
            "name": "quick",
            "description": "Fast reads",
            "model": "claude-code/claude-haiku-5-5",
            "tools": ["view_page"],
            "prompt": "Read fast.",
        },
        headers=CSRF_HEADERS,
    )
    assert created.status_code == 201, created.text
    main, sub = stub_messages(
        [
            one_call("call", "subagent", {"name": "quick", "task": "Read page 1."}),
            ([{"type": "text", "text": "Here is what I have."}], "end_turn"),
        ],
        [
            one_call("p1", "view_page", {"page": 1}),
            ([{"type": "text", "text": "Never sent."}], "end_turn"),
        ],
    )
    conversation_id = await new_conversation(client)
    await send(client, conversation_id, text="Read")
    await _run_queued_turn(client)

    # The subagent made one call and ran none of its tools; the turn's second call is its
    # last, with no tool left to it.
    assert (len(main), len(sub), pages) == (2, 1, [])
    assert "tool_choice" not in main[0]
    assert main[1]["tool_choice"] == {"type": "none"}
    thread = (await client.get(f"/api/conversations/{conversation_id}")).json()
    [turn] = thread["turns"]
    assert (turn["status"], turn["credits"]) == ("succeeded", 7)
    result = thread["messages"][2]["content"][0]
    assert not result.get("is_error") and chat_subagent.CREDITS_SPENT in result["content"]
    billing = (await client.get("/api/billing")).json()
    assert (billing["period"]["spent"], billing["period"]["remaining"]) == (7, -3)


@respx.mock
async def test_a_write_waiting_when_a_subagent_spends_the_balance_is_not_started(
    make_client, tmp_path, monkeypatch
) -> None:
    """docs/billing.md § A chat turn: a tool of the round not yet started once the balance
    is spent and the allowance used answers an error result; the write already running
    finishes."""
    client = await chatting_client(
        make_client(
            workspaces_dir=str(tmp_path / "workspaces"),
            free_credits_per_period=4,
            credit_usd="0.00005",
            chat_overdraft_max_tool_calls=0,
        )
    )
    created = await client.post(
        "/api/subagents",
        json={
            "name": "quick",
            "description": "Fast reads",
            "model": "claude-code/claude-haiku-5-5",
            "tools": ["view_page"],
            "prompt": "Read fast.",
        },
        headers=CSRF_HEADERS,
    )
    assert created.status_code == 201, created.text
    writes: list[str] = []

    async def run_mutating(ctx, name, tool_input):
        # The first write of the round runs until the subagent beside it has spent the
        # balance, so the second is still waiting its turn when that happens.
        writes.append(tool_input["path"])
        for _ in range(200):
            if ctx.credits_exhausted:
                break
            await asyncio.sleep(0.05)
        return chat_tools.ToolOutcome("written")

    monkeypatch.setattr(chat_runner, "run_mutating", run_mutating)
    main, sub = stub_messages(
        [
            (
                [
                    {
                        "type": "tool_use",
                        "id": "call",
                        "name": "subagent",
                        "input": {"name": "quick", "task": "Read page 1."},
                    },
                    {
                        "type": "tool_use",
                        "id": "w1",
                        "name": "str_replace_based_edit_tool",
                        "input": {"command": "create", "path": "a.md", "file_text": "a"},
                    },
                    {
                        "type": "tool_use",
                        "id": "w2",
                        "name": "str_replace_based_edit_tool",
                        "input": {"command": "create", "path": "b.md", "file_text": "b"},
                    },
                ],
                "tool_use",
            ),
            ([{"type": "text", "text": "Wrote a.md."}], "end_turn"),
        ],
        [one_call("p1", "view_page", {"page": 1})],
    )
    conversation_id = await new_conversation(client)
    await send(client, conversation_id, text="Write")
    await _run_queued_turn(client)

    assert (len(main), len(sub), writes) == (2, 1, ["a.md"])
    assert main[1]["tool_choice"] == {"type": "none"}
    thread = (await client.get(f"/api/conversations/{conversation_id}")).json()
    [turn] = thread["turns"]
    assert turn["status"] == "succeeded"
    results = thread["messages"][2]["content"]
    assert chat_subagent.CREDITS_SPENT in results[0]["content"]
    assert [(block.get("is_error", False), block["content"]) for block in results[1:]] == [
        (False, "written"),
        (True, chat_runner.NOT_RUN_OUT_OF_CREDITS),
    ]
    billing = (await client.get("/api/billing")).json()
    assert (billing["period"]["spent"], billing["period"]["remaining"]) == (7, -3)


@respx.mock
async def test_a_stop_reaches_a_subagent_inside_its_run(chat_client, monkeypatch) -> None:
    client = chat_client
    pages = fake_pages(monkeypatch)
    stopped = False
    checks = chat_runner._cancelled

    async def cancelled(ctx) -> bool:
        return stopped or await checks(ctx)

    monkeypatch.setattr(chat_runner, "_cancelled", cancelled)
    created = await client.post(
        "/api/subagents",
        json={"name": "pages", "prompt": "Read.", "tools": ["view_page"]},
        headers=CSRF_HEADERS,
    )
    assert created.status_code == 201, created.text

    def answer(request: httpx.Request) -> httpx.Response:
        nonlocal stopped
        body = json.loads(request.content)
        if body["system"][0]["text"].startswith(SUBAGENT_PREAMBLE_START):
            # The reader presses Stop while the subagent's call streams.
            stopped = True
            blocks = [{"type": "tool_use", "id": "p1", "name": "view_page", "input": {"page": 1}}]
        else:
            blocks = [
                {
                    "type": "tool_use",
                    "id": "call",
                    "name": "subagent",
                    "input": {"name": "pages", "task": "Read it."},
                }
            ]
        return httpx.Response(200, content=streamed(blocks, "tool_use", model=body["model"]))

    respx.post(MESSAGES_URL).mock(side_effect=answer)
    conversation_id = await new_conversation(client)
    await send(client, conversation_id, text="Read it")
    await _run_queued_turn(client)

    thread = (await client.get(f"/api/conversations/{conversation_id}")).json()
    assert thread["turns"][0]["status"] == "cancelled"
    assert pages == []
    # Nothing of the round is stored; the call waits for the replay's repair.
    assert [message["role"] for message in thread["messages"]] == ["user", "assistant"]
    events = await replayed_events(client, conversation_id)
    assert events[-1][0] == "turn_error" and events[-1][1]["status"] == "cancelled"
    # Billed all the same: the turn's call and the subagent's.
    async with client.app.state.sessionmaker() as db:
        rows = (await db.execute(select(UsageLog).where(UsageLog.agent == "chat"))).scalars().all()
    assert len(rows) == 2


def test_a_skill_renders_its_arguments() -> None:
    assert skill_defs.render_skill("Check $ARGUMENTS now.", " 1-3 ") == "Check 1-3 now."
    assert skill_defs.render_skill("Check it.", "1-3") == "Check it.\n\nARGUMENTS: 1-3"
    assert skill_defs.render_skill("Check it.", "  ") == "Check it."


def test_the_subagent_records_bound_a_long_input() -> None:

    # A long field is cut, so the step still reads as the call it was.
    write = {"command": "str_replace", "path": "/workspace/doc.md", "new_str": "x" * 5000}
    assert chat_subagent._bounded_input(write) == {
        **write,
        "new_str": "x" * chat_subagent.FIELD_MAX_CHARS,
    }
    # Too long even so: cut whole.
    many = {str(index): "x" * 500 for index in range(50)}
    bounded = chat_subagent._bounded_input(many)
    assert isinstance(bounded, str) and len(bounded) == chat_subagent.INPUT_MAX_CHARS
    assert chat_subagent._bounded_input({"page": 1}) == {"page": 1}


def test_a_subagents_tools_are_the_subagent_tools_in_their_order() -> None:
    named = ["bash", "str_replace_based_edit_tool", "skill", "view_page", "subagent", "view_page"]
    assert subagent_defs.canonical_tools(named) == (
        "view_page",
        "str_replace_based_edit_tool",
        "bash",
    )


@respx.mock
async def test_thinking_is_replayed_only_under_the_prompt_it_was_made_under(chat_client) -> None:
    """docs/chat.md § History is append-only: a thinking block is stored with the prompt and
    tools it was made under and goes back as the API sent it while they hold; once the
    reader's instructions change the system prompt it is left out — the answers' text
    stays — and the blocks made under the new prompt are replayed."""

    def answer(note: str, said: str) -> tuple[list[dict], str]:
        return [{"type": "thinking", "thinking": note}, {"type": "text", "text": said}], "end_turn"

    main, _ = stub_messages(
        [
            answer("First.", "One."),
            answer("Second.", "Two."),
            answer("Third.", "Three."),
            ([{"type": "text", "text": "Four."}], "end_turn"),
        ]
    )
    conversation_id = await new_conversation(chat_client)

    async def turn(text: str) -> None:
        assert (await send(chat_client, conversation_id, text=text)).status_code == 202
        await _run_queued_turn(chat_client)

    def thinking(request: dict) -> list[str]:
        return [
            block["thinking"]
            for message in request["messages"]
            if message["role"] == "assistant"
            for block in message["content"]
            if block["type"] == "thinking"
        ]

    await turn("one")
    await turn("two")
    assert main[1]["messages"][1]["content"][0] == {
        "type": "thinking",
        "thinking": "First.",
        "signature": "sig",
    }
    changed = await chat_client.put(
        "/api/agents/chat/settings", json={"instructions": "Be brief."}, headers=CSRF_HEADERS
    )
    assert changed.status_code == 200, changed.text
    await turn("three")
    await turn("four")

    assert main[2]["system"] != main[1]["system"]
    assert thinking(main[2]) == []
    assert [
        message["content"] for message in main[2]["messages"] if message["role"] == "assistant"
    ] == [[{"type": "text", "text": "One."}], [{"type": "text", "text": "Two."}]]
    assert thinking(main[3]) == ["Third."]
    # Stored with the prompt each was made under, never sent with it.
    async with chat_client.app.state.sessionmaker() as db:
        stored = (
            await db.execute(
                select(Message.content).where(
                    Message.conversation_id == conversation_id, Message.role == "assistant"
                )
            )
        ).scalars()
        bindings = [
            block["binding"]
            for content in stored
            for block in content
            if block["type"] == "thinking"
        ]
    assert len(bindings) == 3 and bindings[0] == bindings[1] != bindings[2]
    assert not any(
        "binding" in block
        for request in main
        for message in request["messages"]
        for block in message["content"]
        if isinstance(block, dict)
    )
