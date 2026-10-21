"""The format check: the program rules, the check agent's loop and its tools, and a Files
upload that opens only checked (docs/parsing.md § Format check). Every upstream call is
stubbed (docs/testing.md § Cost safety).
"""

from __future__ import annotations

import json
import uuid

import httpx
import pytest
import respx
from httpx import AsyncClient
from sqlalchemy import select

from sightread.agents import CHECK
from sightread.agents import check as check_agent
from sightread.auth.crypto import encrypt_kano_proxy_key
from sightread.config import Settings
from sightread.db.models import Document, Job, Result, UsageLog
from sightread.jobs.check import CheckFailed, run_check
from sightread.parsing import joins
from sightread.routes.library import document_payload
from sightread.upstream.client import Connection
from sightread.workspace.layout import user_root
from tests.conftest import CSRF_HEADERS, KANO_BASE, KANO_CHAT_URL, TEST_SECRET_KEY
from tests.test_chat import MESSAGES_URL, assistant_turn
from tests.test_library import MODEL
from tests.test_parse_end_to_end import KANO_KEY, _drain_queue
from tests.test_retry_failed_pages import Upstream, _answer, _retry, _row, _upload

# Three pages a vision model might send back for a paper: a sentence the first page break
# cuts before a capital, a bold run left open, and a word the second break hyphenates.
PAGES = {
    1: "## Introduction\n\nWe evaluate on the",
    2: "Atari benchmark. The **key idea is simple.\n\nOur method is experi-",
    3: "mental and works well.",
}
TRANSCRIPTION = (
    "<!-- page: 1 -->\n## Introduction\n\nWe evaluate on the\n\n"
    "<!-- page: 2 -->\nAtari benchmark. The **key idea is simple.\n\nOur method is experi-\n\n"
    "<!-- page: 3 -->\nmental and works well."
)
# The agent's join and its fix, over the rules' own join of the hyphenated word.
READING = (
    "<!-- page: 1 -->\n## Introduction\n\nWe evaluate on the Atari benchmark. "
    "The **key idea** is simple.\n\n"
    "<!-- page: 2 -->\nOur method is experi-mental and works well.\n\n"
    "<!-- page: 3 -->"
)


def _pages(markdown: str) -> list[joins.Page]:
    lead, pages = joins.split_pages(markdown)
    assert lead == ""
    return pages


def _turn(blocks: list[dict], stop_reason: str = "tool_use") -> httpx.Response:
    return httpx.Response(
        200,
        content=assistant_turn(blocks, stop_reason, model="claude-opus-5-5"),
        headers={"content-type": "text/event-stream"},
    )


def _tool(call_id: str, name: str, **tool_input) -> dict:
    return {"type": "tool_use", "id": call_id, "name": name, "input": tool_input}


def _done() -> httpx.Response:
    return _turn([{"type": "text", "text": "Done."}], "end_turn")


def _connection() -> Connection:
    return Connection(
        ciphertext=encrypt_kano_proxy_key(TEST_SECRET_KEY, KANO_KEY),
        secret_key=TEST_SECRET_KEY,
        base_url=f"{KANO_BASE}/anthropic/v1",
        snapshot=KANO_BASE,
        builtin=True,
        report_cost=True,
    )


def _settings(**overrides) -> Settings:
    return Settings(secret_key=TEST_SECRET_KEY, **overrides)


async def _check(markdown: str, connection: Connection | None = None, **overrides):
    billed: list = []

    async def bill(completed) -> None:
        billed.append(completed)

    outcome = await run_check(
        connection,
        _settings(**overrides),
        model="claude-code/claude-opus-5-5",
        effort="medium",
        system="the check's system prompt",
        markdown=markdown,
        on_usage=bill,
    )
    return outcome, billed


# --- the program rules ----------------------------------------------------------------------


def test_pages_render_back_as_they_were_assembled() -> None:
    lead, pages = joins.split_pages(TRANSCRIPTION)
    assert [page.number for page in pages] == [1, 2, 3]
    assert joins.render(lead, pages) == TRANSCRIPTION


def test_the_rules_join_a_cut_paragraph_and_leave_a_finished_one() -> None:
    pages = _pages(
        "<!-- page: 1 -->\nThe agent learns a policy that\n\n"
        "<!-- page: 2 -->\nmaximizes the return. It ends here.\n\n"
        "<!-- page: 3 -->\nthe next page starts lowercase after a sentence's end."
    )
    assert joins.sure_joins(pages) == 1
    # Moved up after a space; the page it left keeps its marker and the rest of its body.
    assert pages[0].body == "The agent learns a policy that maximizes the return. It ends here."
    assert pages[1].body == ""
    assert pages[2].body.startswith("the next page")
    assert joins.find_spots(pages) == []


def test_a_hyphen_after_a_letter_joins_without_a_space_and_stays() -> None:
    pages = _pages("<!-- page: 4 -->\nOur method is experi-\n\n<!-- page: 5 -->\nmental and works.")
    assert joins.sure_joins(pages) == 1
    assert pages[0].body == "Our method is experi-mental and works."


def test_the_rules_leave_spots_for_what_they_cannot_settle() -> None:
    pages = _pages(
        "<!-- page: 1 -->\nWe evaluate on the\n\n"
        "<!-- page: 2 -->\nAtari benchmark. The **key idea is simple.\n\n"
        "- an item cut at the break\n\n"
        "<!-- page: 3 -->\nwhich continues here.\n\nIt costs $5 to run.\n\n"
        "**3.2 Results**\n\n| a | b |\n| --- | --- |\n| 1 | 2 |\n\n"
        "<!-- page: 4 -->\n| a | b |\n| --- | --- |\n| 3 | 4 |\n\n"
        "```\nunbalanced ** and $ in code\n```\n\n$$\nx = y\n$$"
    )
    assert joins.sure_joins(pages) == 0
    spots = joins.find_spots(pages)
    assert [(spot.kind, spot.page) for spot in spots] == [
        ("join", 2),
        ("emphasis", 2),
        ("join", 3),
        ("math", 3),
        ("heading", 3),
        ("join", 4),
    ]
    # A join shows both sides of the break.
    assert (spots[0].excerpt, spots[0].after) == (
        "We evaluate on the",
        "Atari benchmark. The **key idea is simple.",
    )


def test_a_blank_page_is_passed_over_and_never_joined() -> None:
    # `assemble` strips every page, but a page left with only whitespace must not break the
    # check: the break across it continues the page before it.
    pages = [
        joins.Page(1, "The model is trained"),
        joins.Page(2, "  "),
        joins.Page(3, "with a large batch."),
    ]
    assert joins.sure_joins(pages) == 1
    assert pages[0].body == "The model is trained with a large batch."
    assert joins.find_spots(pages) == []
    assert joins.join_pages(pages, 2) == "page 2 has nothing to join"


def test_blocks_are_read_as_the_viewer_reads_them() -> None:
    # apps/web/app/lib/markdown.ts: a paragraph ends where a line starts another block,
    # blank line or not.
    body = (
        "Example:\n```python\nprint(1)\n\nprint(2)\n```\nand after the listing.\n\n"
        "The loss is\n$$L = x$$\nwhere\n$$\nx = 1\n$$\n\n"
        "An introductory sentence.\n## Results\nWe use:\n- a\n- b\n  wrapped item\n"
        "continued lazily\n\n"
        "Table 1: Scores\n| a | b |\n| --- | --- |\n| 1 | 2 |\n\n"
        "Region | Revenue\n--- | ---\nAPAC | 1204\n\n"
        "Its norm |x| = 3 holds.\n\n"
        "See below\n![fig1](sightread://p1/0,0,10,10)\nFigure 1: The model\n\n"
        "Above the rule\n---\n\\[\nx = 1\n\\]\n\n"
        "Contents\n\n1 Introduction . . . . 1\n2 Method . . . . 3"
    )
    found = joins.blocks(body)
    assert [kind for _, _, kind in found] == [
        *("paragraph", "code", "paragraph"),
        *("paragraph", "math", "paragraph", "math"),
        *("paragraph", "heading", "paragraph", "list", "paragraph"),
        *("paragraph", "table"),
        *("table",),
        *("paragraph",),
        *("paragraph", "figure"),
        *("paragraph", "rule", "math"),
        *("paragraph", "contents"),
    ]
    start, end, _ = found[10]
    assert body[start:end] == "- a\n- b\n  wrapped item"


@pytest.mark.parametrize(
    "last",
    [
        "Example:\n```python\nprint(1)\n```",
        "The loss is\n$$L = x$$",
        "- an item\n  ```\n  code\n  ```",
        "An introductory sentence.\n## Results",
        "Above the rule\n---",
        "Table 1: Scores\n| a | b |\n| --- | --- |\n| 1 | 2 |",
        "Region | Revenue\n--- | ---\nAPAC | 1204",
        "See below\n![fig1](sightread://p1/0,0,10,10)\nFigure 1: The model",
        "Contents\n\n1 Introduction . . . . 1",
    ],
)
def test_no_join_reaches_past_a_paragraph_into_another_block(last: str) -> None:
    # Written straight under prose, a listing, a formula, a heading, a rule, a table (with
    # or without the outer pipes), a figure or a contents run still ends its page: the next
    # page's lowercase start must never land on it.
    pages = [joins.Page(1, last), joins.Page(2, "next paragraph starts here.")]
    assert joins.sure_joins(pages) == 0
    assert pages[0].body == last
    assert joins.find_spots(pages) == []
    assert joins.join_pages(pages, 2) == (
        "only a paragraph continuing a paragraph or a list item, or a table continuing "
        "a table, joins across a page break"
    )
    assert pages[0].body == last


@pytest.mark.parametrize("last", ["The **conclusion.**", "The *conclusion.*", "The __end.__"])
def test_a_sentence_ends_inside_closing_emphasis_too(last: str) -> None:
    pages = [joins.Page(1, last), joins.Page(2, "iPhone users need a separate setup.")]
    assert joins.sure_joins(pages) == 0
    assert joins.find_spots(pages) == []


def test_prose_written_straight_after_a_listing_still_joins() -> None:
    pages = [joins.Page(1, "```\ncode\n```\nthe text goes"), joins.Page(2, "on here.")]
    assert joins.sure_joins(pages) == 1
    assert pages[0].body == "```\ncode\n```\nthe text goes on here."


def test_two_tables_join_as_one_with_the_second_header_kept_as_a_row() -> None:
    pages = _pages(
        "<!-- page: 1 -->\n| a | b |\n| --- | --- |\n| 1 | 2 |\n\n"
        "<!-- page: 2 -->\n| a | b |\n| --- | --- |\n| 3 | 4 |\n\nAfter the table."
    )
    assert joins.join_pages(pages, 2) is None
    assert pages[0].body == "| a | b |\n| --- | --- |\n| 1 | 2 |\n| a | b |\n| 3 | 4 |"
    assert pages[1].body == "After the table."


@pytest.mark.parametrize(
    ("last", "first"),
    [
        ("## A heading", "A paragraph."),
        ("A paragraph that", "| a | b |\n| --- | --- |"),
        ("- an item", "- another item"),
        ("| a | b |\n| --- | --- |", "and a paragraph."),
    ],
)
def test_a_join_mends_only_a_cut_paragraph_list_item_or_table(last: str, first: str) -> None:
    pages = [joins.Page(1, last), joins.Page(2, first)]
    assert joins.join_pages(pages, 2) == (
        "only a paragraph continuing a paragraph or a list item, or a table continuing "
        "a table, joins across a page break"
    )
    assert [page.body for page in pages] == [last, first]
    assert joins.join_pages(pages, 9) == "there is no page 9"
    assert joins.join_pages(pages, 1) == "page 1 has nothing to join"


def test_a_replacement_may_change_markup_and_never_the_text() -> None:
    pages = _pages(
        "<!-- page: 1 -->\nThe **key idea is simple. It costs $x to run.\n\n**3.2 Results**\n\n"
        "<!-- page: 2 -->\nThe end. The end."
    )
    assert joins.replace_once(pages, "The **key idea is", "The **key idea** is") is None
    assert joins.replace_once(pages, "$x to", "$x$ to") is None
    assert joins.replace_once(pages, "**3.2 Results**", "## 3.2 Results") is None
    assert pages[0].body == ("The **key idea** is simple. It costs $x$ to run.\n\n## 3.2 Results")
    # A changed word, a passage that is not unique, and a page marker are refused, and
    # refusing changes nothing.
    assert joins.replace_once(pages, "simple", "easy") == (
        "the edit changes the text; only markup may change"
    )
    assert joins.replace_once(pages, "The end.", "**The end.**") == (
        "old_str occurs 2 times; it must occur exactly once"
    )
    assert joins.replace_once(pages, "Results", "Results\n<!-- page: 9 -->") == (
        "an edit may not hold a page marker"
    )
    assert joins.replace_once(pages, "", "x") == "old_str is empty"
    assert pages[1].body == "The end. The end."


# --- the agent's loop -----------------------------------------------------------------------


async def test_a_document_with_no_spot_costs_nothing() -> None:
    # Joined by the rules alone; no connection is needed, and none is made.
    outcome, billed = await _check(
        "<!-- page: 1 -->\nWe study agents that\n\n<!-- page: 2 -->\nlearn. The end."
    )
    assert (
        outcome.markdown
        == "<!-- page: 1 -->\nWe study agents that learn. The end.\n\n<!-- page: 2 -->"
    )
    assert (outcome.joins, outcome.edits, billed) == (1, 0, [])


@respx.mock
async def test_the_agent_joins_and_fixes_through_its_tools() -> None:
    route = respx.post(MESSAGES_URL).mock(
        side_effect=[
            _turn(
                [
                    _tool("t1", "join_pages", page=2),
                    _tool(
                        "t2",
                        "str_replace",
                        old_str="The **key idea is simple.",
                        new_str="The **key idea** is simple.",
                    ),
                    _tool("t3", "str_replace", old_str="Atari", new_str="ATARI"),
                    _tool("t4", "read_page", page=2),
                ]
            ),
            _done(),
        ]
    )
    outcome, billed = await _check(TRANSCRIPTION, _connection())

    assert outcome.markdown == READING
    # The rules' join of the hyphenated word, and the agent's.
    assert (outcome.joins, outcome.edits) == (2, 1)
    assert len(billed) == 2

    first = json.loads(route.calls[0].request.content)
    assert first["model"] == "claude-code/claude-opus-5-5"
    assert first["system"] == "the check's system prompt"
    assert first["output_config"] == {"effort": "medium"}
    assert [tool["name"] for tool in first["tools"]] == ["read_page", "join_pages", "str_replace"]
    spots = first["messages"][0]["content"]
    assert spots.startswith("2 places need a decision")
    assert '<place n="1" kind="join" page="2">' in spots
    assert '<place n="2" kind="emphasis" page="2">' in spots

    # The answer replayed as it came, each call answered in order; the edit that changed
    # a word was refused and said why, and read_page saw the page as it now stands.
    second = json.loads(route.calls[1].request.content)
    assert [block["id"] for block in second["messages"][1]["content"]] == ["t1", "t2", "t3", "t4"]
    results = second["messages"][2]["content"]
    assert [result.get("is_error", False) for result in results] == [False, False, True, False]
    assert results[2]["content"] == "the edit changes the text; only markup may change"
    assert results[3]["content"] == "Our method is experi-mental and works well."


@pytest.mark.parametrize(
    ("answer", "reason"),
    [
        (lambda: _turn([{"type": "text", "text": "I can't."}], "refusal"), "blocked"),
        (lambda: _turn([{"type": "text", "text": "Half an"}], "max_tokens"), "cut off"),
        (lambda: httpx.Response(402), "payment"),
        (lambda: httpx.Response(401), "the upstream rejected the stored key"),
        (lambda: httpx.Response(400), "upstream call failed"),
    ],
)
@respx.mock
async def test_a_check_that_cannot_finish_fails(answer, reason) -> None:
    respx.post(MESSAGES_URL).mock(side_effect=lambda request: answer())
    with pytest.raises(CheckFailed) as failed:
        await _check(TRANSCRIPTION, _connection())
    assert str(failed.value) == reason


@respx.mock
async def test_an_agent_still_working_at_the_cap_fails() -> None:
    route = respx.post(MESSAGES_URL).mock(
        side_effect=lambda request: _turn([_tool("t1", "read_page", page=1)])
    )
    with pytest.raises(CheckFailed) as failed:
        await _check(TRANSCRIPTION, _connection(), check_max_calls=3)
    assert str(failed.value) == "not finished"
    assert route.call_count == 3


async def test_spots_without_kano_proxy_fail() -> None:
    with pytest.raises(CheckFailed) as failed:
        await _check(TRANSCRIPTION, None)
    assert str(failed.value) == "Kano Proxy is not connected"


@respx.mock
async def test_the_spots_past_the_cap_stay_as_they_are() -> None:
    route = respx.post(MESSAGES_URL).mock(side_effect=lambda request: _done())
    await _check(TRANSCRIPTION, _connection(), check_spots_max=1)
    spots = json.loads(route.calls[0].request.content)["messages"][0]["content"]
    assert spots.startswith("1 places need a decision")
    assert 'kind="emphasis"' not in spots


# --- a Files upload -------------------------------------------------------------------------


@pytest.fixture
async def library(make_client, tmp_path) -> AsyncClient:
    from tests.test_library import _ready_to_parse

    client = make_client(workspaces_dir=str(tmp_path / "ws"))
    await _ready_to_parse(client)
    client.user_id = (await client.get("/api/me")).json()["user"]["id"]
    return client


def _vision() -> Upstream:
    return Upstream(
        {(MODEL, page): (lambda text=text: _answer(text)) for page, text in PAGES.items()}
    )


async def _job(sessionmaker, job_id: str) -> Job:
    async with sessionmaker() as db:
        return await db.get(Job, uuid.UUID(job_id))


@respx.mock
async def test_a_files_upload_opens_checked(library, sessionmaker, documents) -> None:
    respx.post(KANO_CHAT_URL).mock(side_effect=_vision())
    agent = respx.post(MESSAGES_URL).mock(
        side_effect=[
            _turn(
                [
                    _tool("t1", "join_pages", page=2),
                    _tool(
                        "t2",
                        "str_replace",
                        old_str="The **key idea is simple.",
                        new_str="The **key idea** is simple.",
                    ),
                ]
            ),
            _done(),
        ]
    )
    uploaded = await _upload(library, documents["mixed_pdf"])
    job = await _job(sessionmaker, uploaded["job_id"])
    # Snapshotted at the upload: no model of its own is the parse's, and the default prompt.
    assert (job.check_model, job.check_effort, job.check_prompt) == (
        MODEL,
        "medium",
        CHECK.default_prompt_template,
    )
    await _drain_queue(library, sessionmaker)

    assert (await _row(library, uploaded["id"]))["status"] == "succeeded"
    async with sessionmaker() as db:
        result = await db.get(Result, uuid.UUID(uploaded["job_id"]))
        usage = (
            (await db.execute(select(UsageLog).where(UsageLog.agent == CHECK.name))).scalars().all()
        )
    # The transcription is kept exactly; the reading copy is what the document opens on.
    assert (result.markdown, result.reading_markdown) == (TRANSCRIPTION, READING)
    assert result.meta["check"] == {"model": MODEL, "joins": 2, "edits": 1}
    assert len(agent.calls) == 2
    assert [(row.job_id, row.model) for row in usage] == [(job.id, MODEL)] * 2

    content = await library.get(f"/api/library/documents/{uploaded['id']}/content")
    assert content.status_code == 200, content.text
    assert content.json()["markdown"] == READING
    root = user_root(library.app.state.settings.workspaces_dir, library.user_id)
    baseline = root / ".sightread" / "results" / uploaded["job_id"] / "result.md"
    assert baseline.read_text() == TRANSCRIPTION
    # The API and History read the transcription.
    api_result = await library.get(f"/api/library/documents/{uploaded['id']}/result")
    assert api_result.json()["markdown"] == TRANSCRIPTION


@respx.mock
async def test_a_failed_check_fails_the_job_and_its_retry_checks_again(
    library, sessionmaker, documents
) -> None:
    vision = _vision()
    respx.post(KANO_CHAT_URL).mock(side_effect=vision)
    agent = respx.post(MESSAGES_URL).mock(
        side_effect=[_turn([{"type": "text", "text": "No."}], "refusal"), _done()]
    )
    uploaded = await _upload(library, documents["mixed_pdf"])
    await _drain_queue(library, sessionmaker)

    # Every page converted, and still nothing opens: the check failed.
    row = await _row(library, uploaded["id"])
    assert (row["status"], row["failed_pages"], row["checking"]) == ("failed", 0, False)
    assert row["error"] == "the format check failed (blocked)"
    assert (
        await library.get(f"/api/library/documents/{uploaded['id']}/content")
    ).status_code == 409
    assert len(vision.calls) == 3

    # The retry sends no page to a model: it assembles the kept pages and checks again.
    retried = await _retry(library, uploaded["id"])
    assert retried.status_code == 202, retried.text
    await _drain_queue(library, sessionmaker)
    assert len(vision.calls) == 3
    assert len(agent.calls) == 2
    assert (await _row(library, uploaded["id"]))["status"] == "succeeded"
    async with sessionmaker() as db:
        result = await db.get(Result, uuid.UUID(uploaded["job_id"]))
    # The agent ended its turn at once: the rules' own join only.
    assert result.reading_markdown == (
        TRANSCRIPTION.replace("experi-\n\n<!-- page: 3 -->\nmental", "experi-mental")
        + "\n\n<!-- page: 3 -->"
    )


@respx.mock
async def test_a_retry_takes_the_check_settings_of_its_moment(
    library, sessionmaker, documents
) -> None:
    respx.post(KANO_CHAT_URL).mock(side_effect=_vision())
    respx.post(MESSAGES_URL).mock(side_effect=lambda request: httpx.Response(402))
    uploaded = await _upload(library, documents["mixed_pdf"])
    await _drain_queue(library, sessionmaker)
    assert (await _row(library, uploaded["id"]))["error"] == "the format check failed (payment)"

    # Switched off since: the retry runs unchecked, and the document opens on the
    # transcription.
    off = await library.put(
        "/api/agents/check/settings", json={"auto_check": False}, headers=CSRF_HEADERS
    )
    assert off.status_code == 200, off.text
    assert (await _retry(library, uploaded["id"])).status_code == 202
    assert (await _job(sessionmaker, uploaded["job_id"])).check_model is None
    await _drain_queue(library, sessionmaker)
    async with sessionmaker() as db:
        result = await db.get(Result, uuid.UUID(uploaded["job_id"]))
    assert (result.reading_markdown, "check" in result.meta) == (None, False)
    content = await library.get(f"/api/library/documents/{uploaded['id']}/content")
    assert content.json()["markdown"] == TRANSCRIPTION


async def test_the_check_takes_its_own_settings(library, sessionmaker, documents) -> None:
    saved = await library.put(
        "/api/agents/check/settings",
        json={"model": "claude-code/claude-haiku-5-5", "effort": "low", "instructions": "Use $."},
        headers=CSRF_HEADERS,
    )
    assert saved.status_code == 200, saved.text
    assert saved.json()["auto_check"] is True
    uploaded = await _upload(library, documents["mixed_pdf"])
    job = await _job(sessionmaker, uploaded["job_id"])
    assert (job.check_model, job.check_effort) == ("claude-code/claude-haiku-5-5", "low")
    assert job.check_prompt == check_agent.system_prompt(CHECK.default_prompt_template, "Use $.")
    assert job.check_prompt.endswith("# The reader's instructions\n\nUse $.\n")


async def test_no_check_when_switched_off_or_through_the_api(
    library, sessionmaker, documents
) -> None:
    off = await library.put(
        "/api/agents/check/settings", json={"auto_check": False}, headers=CSRF_HEADERS
    )
    assert off.status_code == 200, off.text
    uploaded = await _upload(library, documents["mixed_pdf"])
    assert (await _job(sessionmaker, uploaded["job_id"])).check_model is None

    # An API parse is the transcription its caller asked for: never checked.
    on = await library.put(
        "/api/agents/check/settings", json={"auto_check": True}, headers=CSRF_HEADERS
    )
    assert on.status_code == 200, on.text
    created = await library.post("/api/keys", json={"name": "api"}, headers=CSRF_HEADERS)
    accepted = await library.post(
        "/v1/parse",
        files={"file": ("doc.pdf", documents["text_pdf"].read_bytes(), "application/pdf")},
        headers={"Authorization": f"Bearer {created.json()['key']}"},
    )
    assert accepted.status_code == 202, accepted.text
    assert (await _job(sessionmaker, accepted.json()["job_id"])).check_model is None


def test_checking_is_a_running_parse_past_its_last_page_with_a_check() -> None:
    def payload(**fields) -> dict:
        state = {"status": "running", "pages_done": 3, "check_model": MODEL, **fields}
        job = Job(id=uuid.uuid4(), kind="pdf", pages_spec=None, page_count=3, **state)
        return document_payload(Document(id=1, name="a.pdf", job_id=job.id), job)

    assert payload()["checking"] is True
    assert payload(pages_done=2)["checking"] is False
    assert payload(check_model=None)["checking"] is False
    assert payload(status="succeeded")["checking"] is False


# --- the settings ---------------------------------------------------------------------------


async def test_each_agent_takes_only_its_own_settings(library) -> None:
    agents = {agent["name"]: agent for agent in (await library.get("/api/agents")).json()["agents"]}
    assert agents["check"]["settings"]["auto_check"] is True
    assert agents["check"]["default_prompt"] == CHECK.default_prompt_template

    async def put(agent: str, **body) -> httpx.Response:
        return await library.put(f"/api/agents/{agent}/settings", json=body, headers=CSRF_HEADERS)

    assert (await put("check", instructions="Formulas in $")).status_code == 200
    assert (await put("chat", instructions="Answer briefly")).status_code == 200
    for agent, body in [
        ("parse", {"auto_check": False}),
        ("chat", {"auto_check": False}),
        ("parse", {"instructions": "x"}),
        ("check", {"fallback_model": "m"}),
        ("check", {"answer_language": "en"}),
        ("check", {"memory_auto": False}),
        ("check", {"auto_check": None}),
    ]:
        refused = await put(agent, **body)
        assert refused.status_code == 400, (agent, body)
    # The check speaks the Messages API through Kano Proxy only.
    refused = await put("check", connection_id=1)
    assert refused.status_code == 400
    assert refused.json()["error"]["message"] == "The check agent runs on Kano Proxy only"
