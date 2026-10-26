"""The chat agent's web tools (docs/chat.md § Tools, docs/billing.md § A chat turn,
docs/workspace.md § Files): a web search charged per call, a paper search, a URL read as
text and a download filed as an upload is — every fetch under the guard, every upstream
stubbed (docs/testing.md). No test reaches the network: the resolver answers a
fixed public address and `respx` answers the connection to it."""

from __future__ import annotations

import socket

import httpx
import respx
from sqlalchemy import select

from sightread.agents import chat as chat_agent
from sightread.db.models import ChatToolCall, Document, Folder
from sightread.jobs import chat_runner, chat_web
from sightread.workspace import safe_io
from sightread.workspace.documents import measure_storage
from sightread.workspace.layout import user_root
from tests.conftest import CSRF_HEADERS
from tests.fixtures.documents import build_text_layer_pdf
from tests.test_chat import _run_queued_turn
from tests.test_chat_extensions import (
    chatting_client,
    new_conversation,
    one_call,
    replayed_events,
    send,
    stub_messages,
)

PUBLIC_IP = "1.2.3.4"
HOSTS = {
    "example.org": [PUBLIC_IP],
    "private.test": ["10.1.2.3"],
    "mixed.test": [PUBLIC_IP, "127.0.0.1"],
}
WEB_KEYS = {"brave_search_api_key": "brave-test", "openalex_api_key": "openalex-test"}


def fake_resolver(monkeypatch) -> None:
    def resolve(host: str) -> list[str]:
        if host not in HOSTS:
            raise socket.gaierror(f"no such host {host}")
        return HOSTS[host]

    monkeypatch.setattr(chat_web, "resolve_host", resolve)


async def web_client(make_client, tmp_path, **overrides) -> httpx.AsyncClient:
    return await chatting_client(
        make_client(workspaces_dir=str(tmp_path / "workspaces"), **WEB_KEYS, **overrides)
    )


def brave_results(*titles: str) -> dict:
    return {
        "web": {
            "results": [
                {
                    "title": f"<strong>{title}</strong>",
                    "url": f"https://example.org/{index}",
                    "description": f"About {title} &amp; more",
                    "age": "2 days ago",
                }
                for index, title in enumerate(titles, start=1)
            ]
        }
    }


async def tool_results(client, conversation_id: int) -> list[dict]:
    return [
        data
        for name, data in await replayed_events(client, conversation_id)
        if name == "tool_result"
    ]


async def recorded(client, tool: str) -> int:
    async with client.app.state.sessionmaker() as db:
        rows = (await db.execute(select(ChatToolCall).where(ChatToolCall.tool == tool))).scalars()
        return len(rows.all())


async def turn_credits(client, conversation_id: int) -> tuple[str, int | None]:
    thread = (await client.get(f"/api/conversations/{conversation_id}")).json()
    [turn] = thread["turns"]
    return turn["status"], turn["credits"]


# --- what is declared -------------------------------------------------------------------------


def test_the_web_tools_are_declared_only_where_the_deployment_runs_them() -> None:
    """docs/chat.md § Tools: the switch takes all four away, a search needs its key, and
    their lines leave the prompt with them; after `memory`, so the cached prefix holds."""
    names = lambda tools: [tool["name"] for tool in tools]  # noqa: E731
    assert names(chat_agent.web_tools(enabled=False, brave=True, openalex=True)) == []
    assert names(chat_agent.web_tools(enabled=True, brave=False, openalex=False)) == [
        "read_url",
        "download_file",
    ]
    assert names(chat_agent.web_tools(enabled=True, brave=True, openalex=False)) == [
        "web_search",
        "read_url",
        "download_file",
    ]
    assert names(chat_agent.web_tools(enabled=True, brave=True, openalex=True)) == list(
        chat_agent.WEB_TOOL_NAMES
    )
    assert chat_agent.TOOLS[-1] is chat_agent.MEMORY_TOOL
    assert not set(chat_agent.WEB_TOOL_NAMES) & set(chat_agent.SUBAGENT_TOOL_NAMES)
    for tool in chat_agent.WEB_TOOLS:
        assert tool["strict"] is True and tool["input_schema"]["additionalProperties"] is False
    rendered = chat_agent.render_system_prompt(
        chat_agent.DEFAULT_SYSTEM_PROMPT, None, tools=["view_page", "read_url", "download_file"]
    )
    assert "- read_url:" in rendered and "- download_file:" in rendered
    assert "- web_search:" not in rendered and "- search_papers:" not in rendered
    everything = chat_agent.render_system_prompt(chat_agent.DEFAULT_SYSTEM_PROMPT, None)
    assert "- web_search:" in everything and "- search_papers:" in everything


async def test_the_agents_route_and_the_rates_say_what_the_deployment_offers(
    make_client, tmp_path
) -> None:
    """docs/api.md `GET /api/agents`, docs/billing.md § Routes: the tools the caller's turn
    declares, and the price of a web search — null where there is none."""
    client = make_client(workspaces_dir=str(tmp_path / "ws"), **WEB_KEYS)
    await client.post("/api/auth/dev-login", headers=CSRF_HEADERS)
    chat = next(
        a for a in (await client.get("/api/agents")).json()["agents"] if a["name"] == "chat"
    )
    assert chat["tools"][-4:] == ["web_search", "search_papers", "read_url", "download_file"]
    assert (await client.get("/api/billing/catalog")).json()["rates"]["web_search_per_call"] == 3
    assert (await client.get("/api/billing")).json()["rates"]["web_search_per_call"] == 3

    bare = make_client(workspaces_dir=str(tmp_path / "ws2"))
    await bare.post("/api/auth/dev-login", headers=CSRF_HEADERS)
    chat = next(a for a in (await bare.get("/api/agents")).json()["agents"] if a["name"] == "chat")
    assert chat["tools"][-3:] == ["memory", "read_url", "download_file"]
    assert (await bare.get("/api/billing/catalog")).json()["rates"]["web_search_per_call"] is None

    off = make_client(workspaces_dir=str(tmp_path / "ws3"), chat_web_access=False, **WEB_KEYS)
    await off.post("/api/auth/dev-login", headers=CSRF_HEADERS)
    chat = next(a for a in (await off.get("/api/agents")).json()["agents"] if a["name"] == "chat")
    assert chat["tools"][-1] == "memory"
    assert (await off.get("/api/billing/catalog")).json()["rates"]["web_search_per_call"] is None


# --- web search -------------------------------------------------------------------------------


@respx.mock
async def test_a_web_search_answers_results_and_is_charged_per_call(make_client, tmp_path) -> None:
    """docs/billing.md § A chat turn: an answered search adds `WEB_SEARCH_CREDITS` to the
    turn's entry on top of its tokens (two stubbed calls round up to 1), recorded as a
    `chat_tool_calls` row; the model gets titles, URLs and plain snippets."""
    client = await web_client(make_client, tmp_path)
    brave = respx.get(host="api.search.brave.com", path="/res/v1/web/search").mock(
        return_value=httpx.Response(200, json=brave_results("Attention", "Transformers"))
    )
    main, _sub = stub_messages(
        [
            one_call("s1", "web_search", {"query": "attention is all you need"}),
            ([{"type": "text", "text": "Found it."}], "end_turn"),
        ]
    )
    conversation_id = await new_conversation(client)
    await send(client, conversation_id, text="Search")
    await _run_queued_turn(client)

    request = brave.calls.last.request
    assert request.headers["x-subscription-token"] == "brave-test"
    assert request.url.params["q"] == "attention is all you need"
    [result] = await tool_results(client, conversation_id)
    assert result["is_error"] is False
    assert "1. Attention" in result["output"] and "https://example.org/1" in result["output"]
    assert "About Attention & more (2 days ago)" in result["output"]
    assert "<strong>" not in result["output"]
    assert await recorded(client, "web_search") == 1
    assert await turn_credits(client, conversation_id) == ("succeeded", 1 + 3)
    billing = (await client.get("/api/billing")).json()
    assert billing["period"]["spent"] == 4
    assert len(main) == 2


@respx.mock
async def test_a_search_brave_did_not_answer_costs_nothing(make_client, tmp_path) -> None:
    client = await web_client(make_client, tmp_path)
    respx.get(host="api.search.brave.com", path="/res/v1/web/search").mock(
        return_value=httpx.Response(503, text="down")
    )
    stub_messages(
        [
            one_call("s1", "web_search", {"query": "anything"}),
            ([{"type": "text", "text": "No luck."}], "end_turn"),
        ]
    )
    conversation_id = await new_conversation(client)
    await send(client, conversation_id, text="Search")
    await _run_queued_turn(client)
    [result] = await tool_results(client, conversation_id)
    assert result["is_error"] is True and "HTTP 503" in result["output"]
    assert await recorded(client, "web_search") == 0
    assert await turn_credits(client, conversation_id) == ("succeeded", 1)


@respx.mock
async def test_web_searches_stop_at_the_turn_cap(make_client, tmp_path) -> None:
    """docs/chat.md § Tools: a search past `WEB_SEARCH_MAX_PER_TURN` answers an error
    result and is neither run nor charged."""
    client = await web_client(make_client, tmp_path, web_search_max_per_turn=1)
    brave = respx.get(host="api.search.brave.com", path="/res/v1/web/search").mock(
        return_value=httpx.Response(200, json=brave_results("One"))
    )
    stub_messages(
        [
            one_call("s1", "web_search", {"query": "first"}),
            one_call("s2", "web_search", {"query": "second"}),
            ([{"type": "text", "text": "Done."}], "end_turn"),
        ]
    )
    conversation_id = await new_conversation(client)
    await send(client, conversation_id, text="Search twice")
    await _run_queued_turn(client)
    first, second = await tool_results(client, conversation_id)
    assert first["is_error"] is False
    assert second["is_error"] is True and "made its 1 web searches" in second["output"]
    assert brave.call_count == 1
    assert await turn_credits(client, conversation_id) == ("succeeded", 1 + 3)


@respx.mock
async def test_web_searches_stop_at_the_deployments_day_cap(make_client, tmp_path) -> None:
    """docs/chat.md § Tools: the per-day cap counts every user's recorded searches of the
    UTC day, so a turn after it is reached is refused before Brave is called."""
    client = await web_client(make_client, tmp_path, web_search_max_per_day=1)
    brave = respx.get(host="api.search.brave.com", path="/res/v1/web/search").mock(
        return_value=httpx.Response(200, json=brave_results("One"))
    )
    for query, expected_error in (("first", False), ("second", True)):
        stub_messages(
            [
                one_call("s", "web_search", {"query": query}),
                ([{"type": "text", "text": "Done."}], "end_turn"),
            ]
        )
        conversation_id = await new_conversation(client)
        await send(client, conversation_id, text="Search")
        await _run_queued_turn(client)
        [result] = await tool_results(client, conversation_id)
        assert result["is_error"] is expected_error, result
    assert "Today's limit on web searches (this deployment's)" in result["output"]
    assert brave.call_count == 1
    assert await recorded(client, "web_search") == 1


@respx.mock
async def test_a_web_search_is_refused_once_the_credits_are_gone(make_client, tmp_path) -> None:
    """docs/billing.md § A chat turn: the search that overdraws the period is charged — it
    was answered — and the next one is refused rather than run on the overdraft; the turn
    still answers."""
    # 105 tokens a call at US$1 per million, a credit US$0.00005: 3 credits a call. Four
    # granted: the first call leaves one, the search takes three more.
    client = await web_client(
        make_client, tmp_path, free_credits_per_period=4, credit_usd="0.00005"
    )
    brave = respx.get(host="api.search.brave.com", path="/res/v1/web/search").mock(
        return_value=httpx.Response(200, json=brave_results("One"))
    )
    stub_messages(
        [
            one_call("s1", "web_search", {"query": "first"}),
            one_call("s2", "web_search", {"query": "second"}),
            ([{"type": "text", "text": "Here is what I have."}], "end_turn"),
        ]
    )
    conversation_id = await new_conversation(client)
    await send(client, conversation_id, text="Search")
    await _run_queued_turn(client)
    first, second = await tool_results(client, conversation_id)
    assert first["is_error"] is False
    assert second["is_error"] is True and second["output"] == chat_runner.NO_CREDITS_FOR_SEARCH
    assert brave.call_count == 1
    status, credits = await turn_credits(client, conversation_id)
    assert status == "succeeded" and credits >= 3 + 3
    assert (await client.get("/api/billing")).json()["period"]["remaining"] < 0


# --- paper search -----------------------------------------------------------------------------


@respx.mock
async def test_search_papers_lists_works_with_their_open_access_pdf(make_client, tmp_path) -> None:
    client = await web_client(make_client, tmp_path)
    openalex = respx.get(host="api.openalex.org", path="/works").mock(
        return_value=httpx.Response(
            200,
            json={
                "results": [
                    {
                        "id": "https://openalex.org/W1",
                        "doi": "https://doi.org/10.1000/xyz",
                        "title": "Attention Is All You Need",
                        "publication_year": 2017,
                        "authorships": [
                            {"author": {"display_name": name}}
                            for name in (
                                "Ashish Vaswani",
                                "Noam Shazeer",
                                "Niki Parmar",
                                "Jakob Uszkoreit",
                            )
                        ],
                        "primary_location": {
                            "source": {"display_name": "NeurIPS"},
                            "landing_page_url": "https://example.org/landing",
                        },
                        "open_access": {"oa_url": "https://example.org/oa.pdf"},
                        "cited_by_count": 100000,
                        "best_oa_location": {"pdf_url": "https://example.org/best.pdf"},
                    }
                ]
            },
        )
    )
    stub_messages(
        [
            one_call("p1", "search_papers", {"query": "transformer attention"}),
            ([{"type": "text", "text": "Found."}], "end_turn"),
        ]
    )
    conversation_id = await new_conversation(client)
    await send(client, conversation_id, text="Find the paper")
    await _run_queued_turn(client)
    request = openalex.calls.last.request
    # The key travels as a header: httpx logs every request's URL (docs/auth.md).
    assert request.headers["authorization"] == "Bearer openalex-test"
    assert "api_key" not in str(request.url)
    assert request.url.params["search"] == "transformer attention"
    [result] = await tool_results(client, conversation_id)
    assert result["is_error"] is False
    assert (
        "1. Attention Is All You Need (2017) — Ashish Vaswani, Noam Shazeer, Niki Parmar "
        "et al. — NeurIPS" in result["output"]
    )
    assert (
        "DOI: https://doi.org/10.1000/xyz · cited by 100000 · PDF: https://example.org/best.pdf"
        in result["output"]
    )
    assert await recorded(client, "search_papers") == 1
    # Free: the turn's tokens alone.
    assert await turn_credits(client, conversation_id) == ("succeeded", 1)


# --- reading a URL ----------------------------------------------------------------------------


@respx.mock
async def test_read_url_reads_a_page_as_text_through_the_checked_address(
    make_client, tmp_path, monkeypatch
) -> None:
    """docs/chat.md § Tools: the connection goes to the address the guard checked, with the
    name kept for TLS and the Host header; the page comes back as its title and text."""
    fake_resolver(monkeypatch)
    client = await web_client(make_client, tmp_path)
    page = respx.get(host=PUBLIC_IP, path="/paper").mock(
        return_value=httpx.Response(
            200,
            headers={"content-type": "text/html; charset=utf-8"},
            content=(
                b"<html><head><title>A Paper</title><script>alert(1)</script>"
                b"<style>p{}</style></head><body><h1>Abstract</h1><p>We study&nbsp;things."
                b"</p><script>tracker()</script><p>Second   paragraph.</p></body></html>"
            ),
        )
    )
    stub_messages(
        [
            one_call("r1", "read_url", {"url": "https://example.org/paper"}),
            ([{"type": "text", "text": "Read."}], "end_turn"),
        ]
    )
    conversation_id = await new_conversation(client)
    await send(client, conversation_id, text="Read it")
    await _run_queued_turn(client)
    request = page.calls.last.request
    assert request.url.host == PUBLIC_IP and request.headers["host"] == "example.org"
    assert request.extensions["sni_hostname"] == "example.org"
    [result] = await tool_results(client, conversation_id)
    assert result["is_error"] is False
    assert result["output"] == (
        "A Paper\nhttps://example.org/paper\n\nAbstract\n\nWe study\xa0things.\n\nSecond paragraph."
    )
    # Free, and not a recorded call: nothing to count.
    assert await turn_credits(client, conversation_id) == ("succeeded", 1)
    assert await recorded(client, "read_url") == 0


@respx.mock
async def test_read_url_reads_a_pdfs_text_layer(tmp_path, monkeypatch) -> None:
    fake_resolver(monkeypatch)
    pdf = build_text_layer_pdf(tmp_path / "paper.pdf").read_bytes()
    respx.get(host=PUBLIC_IP, path="/paper.pdf").mock(
        return_value=httpx.Response(200, headers={"content-type": "application/pdf"}, content=pdf)
    )
    outcome = await chat_web.read_url(
        "https://example.org/paper.pdf",
        max_bytes=10_000_000,
        max_chars=20_000,
        timeout=30,
        pdf_timeout=60,
    )
    assert outcome.is_error is False
    assert outcome.content.startswith("https://example.org/paper.pdf\n\n")
    assert len(outcome.content) > 100


@respx.mock
async def test_read_url_refuses_what_the_guard_refuses(monkeypatch) -> None:
    """docs/chat.md § Tools: no scheme but http(s), no private or local address — the
    literal, the name that resolves to one, a name that resolves to one among others, a
    redirect to one — and nothing is fetched for any of them."""
    fake_resolver(monkeypatch)
    private = respx.get(host="10.1.2.3").mock(return_value=httpx.Response(200, text="secret"))
    respx.get(host=PUBLIC_IP, path="/bounce").mock(
        return_value=httpx.Response(302, headers={"location": "http://10.1.2.3/secret"})
    )
    respx.get(host=PUBLIC_IP, path="/loop").mock(
        return_value=httpx.Response(302, headers={"location": "https://example.org/loop"})
    )
    respx.get(host=PUBLIC_IP, path="/binary").mock(
        return_value=httpx.Response(200, headers={"content-type": "application/zip"}, content=b"PK")
    )

    async def read(url: str) -> chat_web.ToolOutcome:
        return await chat_web.read_url(
            url, max_bytes=1000, max_chars=1000, timeout=5, pdf_timeout=5
        )

    for url in (
        "ftp://example.org/x",
        "http://localhost/x",
        "http://127.0.0.1/x",
        "http://[::1]/x",
        "http://169.254.169.254/latest/meta-data",
        "http://private.test/x",
        "http://mixed.test/x",
        "http://user:pw@example.org/x",
        "https://example.org/bounce",
    ):
        outcome = await read(url)
        assert outcome.is_error is True, url
        assert (
            "private or local" in outcome.content
            or "Only http" in outcome.content
            or "credentials" in outcome.content
        ), (url, outcome.content)
    assert private.call_count == 0
    assert (await read("https://example.org/loop")).content == "Too many redirects."
    assert (await read("https://nowhere.test/x")).content == "The URL's host could not be resolved."
    assert "not text" in (await read("https://example.org/binary")).content


@respx.mock
async def test_read_url_stops_at_its_byte_cap(monkeypatch) -> None:
    fake_resolver(monkeypatch)
    respx.get(host=PUBLIC_IP, path="/long.txt").mock(
        return_value=httpx.Response(
            200, headers={"content-type": "text/plain"}, content=b"x" * 5000
        )
    )
    outcome = await chat_web.read_url(
        "https://example.org/long.txt", max_bytes=1000, max_chars=20_000, timeout=5, pdf_timeout=5
    )
    assert outcome.is_error is False
    assert outcome.content.split("\n\n", 1)[1].count("x") == 1000
    assert outcome.content.endswith("[... the content goes on past what was read ...]")
    respx.get(host=PUBLIC_IP, path="/exact.txt").mock(
        return_value=httpx.Response(
            200, headers={"content-type": "text/plain"}, content=b"y" * 1000
        )
    )
    exact = await chat_web.read_url(
        "https://example.org/exact.txt", max_bytes=1000, max_chars=20_000, timeout=5, pdf_timeout=5
    )
    assert exact.content.endswith("y" * 1000)


def test_html_to_text_and_names() -> None:
    title, text = chat_web.html_to_text("<title>T</title><div>a<br>b</div><ul><li>c</li></ul>")
    assert (title, text) == ("T", "a\nb\n\nc")
    assert chat_web.with_extension("paper", "application/pdf") == "paper.pdf"
    assert chat_web.with_extension("paper.PDF", "application/pdf") == "paper.PDF"
    assert chat_web.with_extension("notes", None) == "notes"
    response = httpx.Response(
        200, headers={"content-disposition": 'attachment; filename="attention.pdf"'}
    )
    assert chat_web.server_filename(response, "https://example.org/dl?id=3") == "attention.pdf"
    assert (
        chat_web.server_filename(httpx.Response(200), "https://example.org/a/b%20c.pdf")
        == "b c.pdf"
    )
    assert chat_web.server_filename(httpx.Response(200), "https://example.org/") == "download"
    assert chat_web._is_global("8.8.8.8") and not chat_web._is_global("10.0.0.1")
    assert not chat_web._is_global("::ffff:127.0.0.1") and not chat_web._is_global("fe80::1")
    assert not chat_web._is_global("0.0.0.0") and not chat_web._is_global("224.0.0.1")  # noqa: S104


# --- downloading ----------------------------------------------------------------------------


@respx.mock
async def test_download_file_files_the_bytes_like_an_upload(
    make_client, tmp_path, monkeypatch
) -> None:
    """docs/workspace.md § Files, *A download is an upload*: the file lands at
    `files/<folder>/<name>` with its row, the folders made where missing, a taken name
    suffixed, the staging name gone, and the call recorded."""
    fake_resolver(monkeypatch)
    client = await web_client(make_client, tmp_path)
    pdf = build_text_layer_pdf(tmp_path / "paper.pdf").read_bytes()
    respx.get(host=PUBLIC_IP, path="/dl").mock(
        return_value=httpx.Response(
            200,
            headers={
                "content-type": "application/pdf",
                "content-disposition": 'attachment; filename="attention.pdf"',
            },
            content=pdf,
        )
    )
    stub_messages(
        [
            one_call(
                "d1",
                "download_file",
                {"url": "https://example.org/dl", "folder": "Papers/2017", "name": ""},
            ),
            one_call(
                "d2",
                "download_file",
                {"url": "https://example.org/dl", "folder": "Papers/2017", "name": "attention"},
            ),
            ([{"type": "text", "text": "Kept."}], "end_turn"),
        ]
    )
    conversation_id = await new_conversation(client)
    await send(client, conversation_id, text="Keep the paper")
    await _run_queued_turn(client)
    first, second = await tool_results(client, conversation_id)
    assert first["is_error"] is False and first["output"].startswith(
        f"Kept attention.pdf ({len(pdf):,} bytes, application/pdf) in Papers/2017."
    )
    assert second["is_error"] is False and second["output"].startswith("Kept attention (2).pdf")

    async with client.app.state.sessionmaker() as db:
        year = (await db.execute(select(Folder).where(Folder.name == "2017"))).scalar_one()
        papers = (await db.execute(select(Folder).where(Folder.name == "Papers"))).scalar_one()
        rows = (
            (
                await db.execute(
                    select(Document).where(Document.kind == "file").order_by(Document.id)
                )
            )
            .scalars()
            .all()
        )
    assert year.parent_id == papers.id
    assert [(row.name, row.folder_id, row.media_type, row.size_bytes) for row in rows] == [
        ("attention.pdf", year.id, "application/pdf", len(pdf)),
        ("attention (2).pdf", year.id, "application/pdf", len(pdf)),
    ]
    root = user_root(client.app.state.settings.workspaces_dir, client.user_id)
    assert (root / "files" / "Papers" / "2017" / "attention.pdf").read_bytes() == pdf
    assert (root / "files" / "Papers" / "2017" / "attention (2).pdf").read_bytes() == pdf
    assert not [
        name for name, _ in safe_io.list_dir(root, root / "files") if name.startswith(".sr-tmp")
    ]
    assert await recorded(client, "download_file") == 2
    assert await turn_credits(client, conversation_id) == ("succeeded", 1)
    # The Files page lists them where the agent put them.
    library = (await client.get("/api/library")).json()
    assert {doc["name"] for doc in library["documents"]} >= {"attention.pdf", "attention (2).pdf"}


@respx.mock
async def test_a_download_past_a_cap_or_the_quota_keeps_nothing(
    make_client, tmp_path, monkeypatch
) -> None:
    """docs/chat.md § Tools: a body past `DOWNLOAD_MAX_BYTES`, past the storage the quota
    leaves, or past the user's day cap is refused, nothing filed and nothing staged."""
    fake_resolver(monkeypatch)
    client = await web_client(make_client, tmp_path, download_max_bytes=100, download_max_per_day=1)
    respx.get(host=PUBLIC_IP, path="/big").mock(
        return_value=httpx.Response(200, headers={"content-type": "text/plain"}, content=b"x" * 500)
    )
    respx.get(host=PUBLIC_IP, path="/small").mock(
        return_value=httpx.Response(200, headers={"content-type": "text/plain"}, content=b"ok")
    )
    respx.get(host=PUBLIC_IP, path="/missing").mock(return_value=httpx.Response(404))
    stub_messages(
        [
            one_call(
                "d1",
                "download_file",
                {"url": "https://example.org/big", "folder": "", "name": "big.txt"},
            ),
            one_call("d2", "download_file", {"url": "http://10.1.2.3/x", "folder": "", "name": ""}),
            one_call(
                "d3",
                "download_file",
                {"url": "https://example.org/missing", "folder": "", "name": ""},
            ),
            one_call(
                "d4",
                "download_file",
                {"url": "https://example.org/small", "folder": "", "name": "a.txt"},
            ),
            one_call(
                "d5",
                "download_file",
                {"url": "https://example.org/small", "folder": "", "name": "b.txt"},
            ),
            ([{"type": "text", "text": "Done."}], "end_turn"),
        ]
    )
    conversation_id = await new_conversation(client)
    await send(client, conversation_id, text="Keep")
    await _run_queued_turn(client)
    big, private, missing, small, capped = await tool_results(client, conversation_id)
    assert big["is_error"] and "larger than the 100 bytes" in big["output"]
    assert private["is_error"] and "private or local" in private["output"]
    assert missing["is_error"] and "HTTP 404" in missing["output"]
    assert small["is_error"] is False and small["output"].startswith(
        "Kept a.txt (2 bytes, text/plain) in the top level"
    )
    assert capped["is_error"] and "Today's limit on downloads (the user's)" in capped["output"]
    root = user_root(client.app.state.settings.workspaces_dir, client.user_id)
    names = {name for name, _ in safe_io.list_dir(root, root / "files")}
    assert "a.txt" in names and not [name for name in names if name.startswith(".sr-tmp")]
    assert "big.txt" not in names and "b.txt" not in names
    assert await recorded(client, "download_file") == 1


@respx.mock
async def test_a_download_the_quota_cannot_hold_is_refused(
    make_client, tmp_path, monkeypatch
) -> None:
    """docs/workspace.md § Files: the room the quota leaves is checked before a byte lands,
    so a file that does not fit is never written, let alone filed."""
    fake_resolver(monkeypatch)
    client = await web_client(make_client, tmp_path)
    root = user_root(client.app.state.settings.workspaces_dir, client.user_id)
    conversation_id = await new_conversation(client)
    async with client.app.state.sessionmaker() as db:
        used = (await measure_storage(db, client.app.state.settings, client.user_id, root)).total
    # The same workspace and user, on a plan with just enough room for a few bytes more.
    tight = make_client(
        workspaces_dir=str(tmp_path / "workspaces"), free_storage_bytes=used + 50, **WEB_KEYS
    )
    respx.get(host=PUBLIC_IP, path="/f").mock(
        return_value=httpx.Response(200, headers={"content-type": "text/plain"}, content=b"x" * 400)
    )
    stub_messages(
        [
            one_call(
                "d1",
                "download_file",
                {"url": "https://example.org/f", "folder": "", "name": "f.txt"},
            ),
            ([{"type": "text", "text": "Done."}], "end_turn"),
        ]
    )
    await send(client, conversation_id, text="Keep")
    await _run_queued_turn(tight)
    [result] = await tool_results(client, conversation_id)
    assert result["is_error"] and "storage is full" in result["output"]
    names = {name for name, _ in safe_io.list_dir(root, root / "files")}
    assert "f.txt" not in names and not [name for name in names if name.startswith(".sr-tmp")]
    assert await recorded(client, "download_file") == 0


@respx.mock
async def test_a_web_tool_the_turn_does_not_declare_is_never_run(
    make_client, tmp_path, monkeypatch
) -> None:
    """docs/chat.md § Tools: with `CHAT_WEB_ACCESS` off the tools are not declared, and a
    call to one all the same — a history that still names it, a model's guess — answers an
    error without a fetch, a record or a charge."""
    fake_resolver(monkeypatch)
    client = await web_client(make_client, tmp_path, chat_web_access=False)
    page = respx.get(host=PUBLIC_IP, path="/p").mock(
        return_value=httpx.Response(200, headers={"content-type": "text/plain"}, content=b"p")
    )
    brave = respx.get(host="api.search.brave.com", path="/res/v1/web/search").mock(
        return_value=httpx.Response(200, json=brave_results("One"))
    )
    stub_messages(
        [
            one_call("r1", "read_url", {"url": "https://example.org/p"}),
            one_call("s1", "web_search", {"query": "anything"}),
            one_call(
                "d1",
                "download_file",
                {"url": "https://example.org/p", "folder": "", "name": "p.txt"},
            ),
            ([{"type": "text", "text": "Done."}], "end_turn"),
        ]
    )
    conversation_id = await new_conversation(client)
    await send(client, conversation_id, text="Try the web")
    await _run_queued_turn(client)
    assert [
        (result["is_error"], result["output"])
        for result in await tool_results(client, conversation_id)
    ] == [
        (True, "'read_url' is not one of this turn's tools"),
        (True, "'web_search' is not one of this turn's tools"),
        (True, "'download_file' is not one of this turn's tools"),
    ]
    assert page.call_count == 0 and brave.call_count == 0
    assert await recorded(client, "web_search") == 0
    assert await recorded(client, "download_file") == 0
    root = user_root(client.app.state.settings.workspaces_dir, client.user_id)
    assert "p.txt" not in {name for name, _ in safe_io.list_dir(root, root / "files")}
    assert await turn_credits(client, conversation_id) == ("succeeded", 1)


@respx.mock
async def test_read_url_bounds_the_title_and_caps_the_whole_result(tmp_path, monkeypatch) -> None:
    """The title is one line at most and the cap holds for the whole result, so a page
    whose title alone would fill it still answers its text; a response without a type is
    read as text, or as a PDF by its magic."""
    fake_resolver(monkeypatch)
    page = (
        "<html><head><title>" + "t" * 25_000 + "</title></head>"
        "<body><p>The body is here.</p></body></html>"
    )
    respx.get(host=PUBLIC_IP, path="/titled").mock(
        return_value=httpx.Response(
            200, headers={"content-type": "text/html"}, content=page.encode()
        )
    )
    outcome = await chat_web.read_url(
        "https://example.org/titled", max_bytes=100_000, max_chars=20_000, timeout=5, pdf_timeout=5
    )
    assert outcome.is_error is False
    title, rest = outcome.content.split("\n", 1)
    assert title == "t" * chat_web.TITLE_MAX_CHARS
    assert rest.startswith("https://example.org/titled\n\n") and "The body is here." in rest
    small = await chat_web.read_url(
        "https://example.org/titled", max_bytes=100_000, max_chars=100, timeout=5, pdf_timeout=5
    )
    assert small.is_error is False and len(small.content) <= 100

    respx.get(host=PUBLIC_IP, path="/untyped").mock(
        return_value=httpx.Response(200, content=b"plain words")
    )
    untyped = await chat_web.read_url(
        "https://example.org/untyped", max_bytes=1000, max_chars=20_000, timeout=5, pdf_timeout=5
    )
    assert untyped.is_error is False and untyped.content.endswith("plain words")
    pdf = build_text_layer_pdf(tmp_path / "paper.pdf").read_bytes()
    respx.get(host=PUBLIC_IP, path="/untyped.pdf").mock(
        return_value=httpx.Response(200, content=pdf)
    )
    as_pdf = await chat_web.read_url(
        "https://example.org/untyped.pdf",
        max_bytes=100_000,
        max_chars=20_000,
        timeout=5,
        pdf_timeout=5,
    )
    assert as_pdf.is_error is False and "<html" not in as_pdf.content
    assert as_pdf.content.strip() != "https://example.org/untyped.pdf"


@respx.mock
async def test_a_download_without_a_content_type_takes_the_names_type(
    make_client, tmp_path, monkeypatch
) -> None:
    """docs/workspace.md § Files: a server that says nothing about the type leaves it to the
    name's extension, as a browser's shrug on an upload does — never `text/plain`."""
    fake_resolver(monkeypatch)
    client = await web_client(make_client, tmp_path)
    pdf = build_text_layer_pdf(tmp_path / "paper.pdf").read_bytes()
    assert chat_web.media_type_of(httpx.Response(200, content=pdf)) == ("", None)
    respx.get(host=PUBLIC_IP, path="/paper.pdf").mock(return_value=httpx.Response(200, content=pdf))
    stub_messages(
        [
            one_call(
                "d1",
                "download_file",
                {"url": "https://example.org/paper.pdf", "folder": "", "name": ""},
            ),
            ([{"type": "text", "text": "Kept."}], "end_turn"),
        ]
    )
    conversation_id = await new_conversation(client)
    await send(client, conversation_id, text="Keep it")
    await _run_queued_turn(client)
    [result] = await tool_results(client, conversation_id)
    assert result["is_error"] is False
    assert result["output"].startswith(f"Kept paper.pdf ({len(pdf):,} bytes, application/pdf)")
    async with client.app.state.sessionmaker() as db:
        row = (
            await db.execute(
                select(Document).where(Document.kind == "file", Document.name == "paper.pdf")
            )
        ).scalar_one()
    assert row.media_type == "application/pdf"
