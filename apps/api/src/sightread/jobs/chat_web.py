"""The chat agent's web tools (docs/chat.md § Tools): a web search through Brave, a
scholarly search through OpenAlex, one URL read as text, and one URL downloaded into the
user's files. All of it the worker's own HTTP — never the VM's, which stays offline
(docs/sandbox.md § Isolation) — under one guard: only http and https, every address the
host resolves to a global one, the connection made to an address that was checked with the
name kept for TLS, each redirect checked again — and every request under the deployment's
own User-Agent. The counting, charging and filing around a call belong to the runner
(`chat_runner.py`); this module makes the calls."""

from __future__ import annotations

import asyncio
import codecs
import contextlib
import html
import ipaddress
import logging
import mimetypes
import os
import re
import socket
import tempfile
import urllib.parse
import zlib
from collections.abc import AsyncIterator
from dataclasses import dataclass
from email.message import Message
from html.parser import HTMLParser
from pathlib import Path

import httpx

from ..parsing import poppler
from .chat_tools import ToolOutcome, truncate

logger = logging.getLogger(__name__)

RESULTS_PER_SEARCH = 10
# A page's title, which heads what `read_url` answers, is one line of it at most: the
# result's cap then holds for the whole result, the title included.
TITLE_MAX_CHARS = 300
MAX_REDIRECTS = 5
# Read in pieces: a page or a file streams through a bounded buffer, never whole in memory
# past its cap (docs/chat.md § Tools).
CHUNK_BYTES = 64 * 1024
# What a read or a download that failed tells the model to do next: an address that refused
# or broke off once is rarely the way to the fact (docs/chat.md § Tools).
ANOTHER_SOURCE = "find another source rather than retrying it"
PDF_MEDIA_TYPE = "application/pdf"
TEXT_MEDIA_TYPES = frozenset(
    {"application/json", "application/xml", "application/x-ndjson", "application/javascript"}
)
NOT_TEXT = (
    "The URL answers {media_type}, which is not text; download_file keeps such a file for the "
    "user, and the shell can open it where there is one."
)


class FetchRefused(Exception):
    """The guard, a cap or the server refused the fetch; the message is the tool's answer."""


# --- the logs -------------------------------------------------------------------------------


class _QueryRedactor(logging.Filter):
    """httpx logs every request's URL at INFO, and a URL `read_url` or `download_file`
    fetches — or is redirected to — may carry its credential in the query, a presigned
    signature or an access token: the query and the fragment leave every URL in those
    records before they are written (docs/chat.md § Tools)."""

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.args, tuple) and any(
            isinstance(arg, httpx.URL) for arg in record.args
        ):
            record.args = tuple(
                arg.copy_with(query=None, fragment=None) if isinstance(arg, httpx.URL) else arg
                for arg in record.args
            )
        return True


QUERY_REDACTOR = _QueryRedactor()


def redact_request_logs() -> None:
    """Put the redaction on httpx's logger: once per process, before the first fetch."""
    logging.getLogger("httpx").addFilter(QUERY_REDACTOR)


# --- the guard ------------------------------------------------------------------------------


def resolve_host(host: str) -> list[str]:
    """Every address the name resolves to, once each, through the system resolver and in
    its order: glibc sorts an address this host has no route to after the others — an
    IPv6 one on a worker without IPv6 — where a sort by text would put it first. The tests
    replace it with a documentation address (docs/testing.md)."""
    infos = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    return list(dict.fromkeys(str(info[4][0]) for info in infos))


def _is_global(address: str) -> bool:
    """A public address: not loopback, private, link-local, multicast, reserved or
    unspecified — so not the compose network's, not the host's own."""
    try:
        ip = ipaddress.ip_address(address.split("%", 1)[0])
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return ip.is_global and not ip.is_multicast


@dataclass(frozen=True)
class Target:
    """Where a checked URL is fetched from: the URL with each checked address in place of
    the name, in the resolver's order, and the name itself, for TLS and the `Host` header."""

    connects: tuple[httpx.URL, ...]
    host: str
    host_header: str


async def check_target(url: str) -> Target:
    """The guard: the scheme, the host and every address it resolves to, refused with a
    reason the model can read (docs/chat.md § Tools)."""
    try:
        parsed = httpx.URL(url.strip())
    except (httpx.InvalidURL, TypeError) as exc:
        raise FetchRefused("The URL could not be parsed.") from exc
    if parsed.scheme not in ("http", "https"):
        raise FetchRefused("Only http and https URLs can be fetched.")
    host = parsed.host
    if not host:
        raise FetchRefused("The URL names no host.")
    if parsed.userinfo:
        raise FetchRefused("A URL with credentials in it is not fetched.")
    lowered = host.lower().rstrip(".")
    if lowered == "localhost" or lowered.endswith(".localhost"):
        raise FetchRefused("The URL points at a private or local network, which is not fetched.")
    try:
        ipaddress.ip_address(lowered)
        addresses = [lowered]
    except ValueError:
        try:
            addresses = await asyncio.to_thread(resolve_host, lowered)
        except (socket.gaierror, UnicodeError) as exc:
            raise FetchRefused("The URL's host could not be resolved.") from exc
    if not addresses:
        raise FetchRefused("The URL's host could not be resolved.")
    if not all(_is_global(address) for address in addresses):
        raise FetchRefused("The URL points at a private or local network, which is not fetched.")
    connects = tuple(parsed.copy_with(host=address) for address in addresses)
    default_port = 443 if parsed.scheme == "https" else 80
    host_header = host if parsed.port in (None, default_port) else f"{host}:{parsed.port}"
    return Target(connects=connects, host=host, host_header=host_header)


@contextlib.asynccontextmanager
async def fetch(
    url: str, *, timeout: float, user_agent: str
) -> AsyncIterator[tuple[httpx.Response, str]]:
    """One GET under the guard, streaming: the response (its body not read) and the URL it
    came from after redirects, each hop checked as the first was, a host's checked
    addresses tried in turn until one connects. The whole exchange is bounded by
    `timeout`: httpx's own is per operation, and a slow drip would otherwise hold a worker
    for as long as it cared to."""
    async with (
        asyncio.timeout(timeout),
        httpx.AsyncClient(
            timeout=timeout,
            follow_redirects=False,
            # Only gzip is asked for: `iter_body` inflates it a bounded piece at a time.
            headers={"User-Agent": user_agent, "Accept-Encoding": "gzip"},
        ) as client,
    ):
        current = url
        for _hop in range(MAX_REDIRECTS + 1):
            target = await check_target(current)
            for index, connect in enumerate(target.connects):
                request = client.build_request(
                    "GET",
                    connect,
                    headers={"Host": target.host_header},
                    extensions={"sni_hostname": target.host},
                )
                try:
                    response = await client.send(request, stream=True)
                    break
                except httpx.ConnectError:
                    # An address this worker cannot connect to — an IPv6 one without an
                    # IPv6 route, a server that refuses — gives way to the next one.
                    if index == len(target.connects) - 1:
                        raise
            location = response.headers.get("location")
            if response.is_redirect and location:
                await response.aclose()
                current = str(httpx.URL(current).join(location))
                continue
            try:
                yield response, current
            finally:
                await response.aclose()
            return
        raise FetchRefused("Too many redirects.")


def failure(exc: Exception, timeout: float) -> str:
    """What a call that failed on the way says happened, as it happened — never a timeout
    for what was not one, so the model knows whether to wait, retry or look elsewhere
    (docs/chat.md § Tools)."""
    if isinstance(exc, (httpx.TimeoutException, TimeoutError)):
        return f"the server did not answer within {timeout:g} seconds"
    if isinstance(exc, httpx.ConnectError):
        return "no connection could be made (refused, unreachable or TLS failed)"
    return "the connection broke off before the answer was read"


def shown(url: str) -> str:
    """A URL as a result or an error names it: scheme, host and path, no query and no
    fragment — a signed URL's signature, a token in its query, stays out of what the
    model is told and the conversation keeps, as it stays out of the log."""
    return str(httpx.URL(url).copy_with(query=None, fragment=None))


def media_type_of(response: httpx.Response) -> tuple[str, str | None]:
    """The response's media type and charset, lower-cased, parameters dropped — the type
    empty where the server sent none, not the `text/plain` the parser would assume."""
    header = response.headers.get("content-type", "")
    if not header.strip():
        return "", None
    message = Message()
    message["content-type"] = header
    return message.get_content_type().lower(), message.get_param("charset")


async def iter_body(response: httpx.Response) -> AsyncIterator[bytes]:
    """The body as the server meant it, in pieces of at most `CHUNK_BYTES`: the wire's
    bytes as they are, or — where the server gzipped them — inflated here a bounded piece
    at a time. httpx's own decoding inflates each raw chunk whole before handing any of it
    over, and a few compressed kilobytes can stand for gigabytes; this way no caller's cap
    is reached through more memory than one piece (docs/chat.md § Tools). Only gzip is
    asked for; a body the server encoded some other way anyway is refused."""
    encodings = [
        part.strip().lower()
        for part in response.headers.get("content-encoding", "").split(",")
        if part.strip()
    ]
    if not encodings or encodings == ["identity"]:
        async for chunk in response.aiter_raw(CHUNK_BYTES):
            yield chunk
        return
    if encodings not in (["gzip"], ["x-gzip"]):
        raise FetchRefused(
            f"The server sent content encoded as {', '.join(encodings)}, which is not read."
        )
    # One inflater per gzip member — gzip allows them concatenated — and none between
    # members: bytes that follow a member's end are the next member's or junk, never
    # held unread. Each raw chunk is inflated in pieces of at most `CHUNK_BYTES`.
    inflater: zlib._Decompress | None = None
    try:
        async for raw in response.aiter_raw(CHUNK_BYTES):
            pending = raw
            while pending:
                if inflater is None:
                    inflater = zlib.decompressobj(wbits=zlib.MAX_WBITS | 16)
                piece = inflater.decompress(pending, CHUNK_BYTES)
                if piece:
                    yield piece
                if inflater.eof:
                    pending = inflater.unused_data
                    inflater = None
                else:
                    pending = inflater.unconsumed_tail
    except zlib.error as exc:
        raise FetchRefused("The server's gzip could not be read.") from exc
    if inflater is not None:
        # The body ended inside a member: what was inflated is not the whole of it.
        raise FetchRefused("The server's gzip ended before its end; nothing was kept.")


async def read_capped(response: httpx.Response, max_bytes: int) -> tuple[bytes, bool]:
    """Up to `max_bytes` of the body, and whether it went on past them — the connection is
    then closed rather than drained."""
    pieces: list[bytes] = []
    size = 0
    async for chunk in iter_body(response):
        # One byte past the cap says the body goes on; nothing more is read or held.
        pieces.append(chunk[: max_bytes + 1 - size])
        size += len(pieces[-1])
        if size > max_bytes:
            return b"".join(pieces)[:max_bytes], True
    return b"".join(pieces), False


# --- searches ---------------------------------------------------------------------------------


_TAG_RE = re.compile(r"<[^>]+>")


def _plain(text: object) -> str:
    """Brave marks matches with tags and entities; the model gets plain text."""
    return html.unescape(_TAG_RE.sub("", str(text or ""))).strip()


def _upstream_failure(service: str, response: httpx.Response) -> ToolOutcome:
    if response.status_code in (401, 403):
        logger.warning("%s refused the deployment's key (%d)", service, response.status_code)
        return ToolOutcome(
            f"{service} refused this deployment's key; tell the user the search is not "
            "available right now.",
            is_error=True,
        )
    if response.status_code == 429:
        return ToolOutcome(
            f"{service} is rate-limiting this deployment; try again later.", is_error=True
        )
    logger.warning("%s answered %d", service, response.status_code)
    return ToolOutcome(
        f"{service} did not answer the search (HTTP {response.status_code}).", is_error=True
    )


def _search_client(timeout: float, user_agent: str) -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=timeout, headers={"User-Agent": user_agent})


async def web_search(
    query: str, *, url: str, api_key: str, timeout: float, user_agent: str
) -> ToolOutcome:
    """The Brave Search API: ten web results as text — title, URL, snippet, age. An error
    outcome is never charged: the runner records a call only for a result (docs/chat.md
    § Tools)."""
    query = query.strip()
    if not query:
        return ToolOutcome("The query is empty.", is_error=True)
    try:
        async with asyncio.timeout(timeout), _search_client(timeout, user_agent) as client:
            response = await client.get(
                url,
                params={"q": query[:400], "count": RESULTS_PER_SEARCH},
                headers={"Accept": "application/json", "X-Subscription-Token": api_key},
            )
    except (httpx.HTTPError, TimeoutError) as exc:
        logger.warning("Brave did not answer: %s", exc.__class__.__name__)
        return ToolOutcome(
            f"The web search failed: {failure(exc, timeout)}; try again.", is_error=True
        )
    if response.status_code != 200:
        return _upstream_failure("Brave", response)
    try:
        results = response.json().get("web", {}).get("results", [])
    except ValueError:
        return ToolOutcome("Brave answered something that is not JSON.", is_error=True)
    if not results:
        return ToolOutcome(f"No web results for {query!r}.")
    lines = [f"Web results for {query!r} (Brave):"]
    for index, result in enumerate(results[:RESULTS_PER_SEARCH], start=1):
        if not isinstance(result, dict):
            continue
        age = _plain(result.get("age"))
        lines.append(f"{index}. {_plain(result.get('title')) or '(untitled)'}")
        lines.append(f"   {_plain(result.get('url'))}")
        snippet = _plain(result.get("description"))
        if snippet:
            lines.append(f"   {snippet}{f' ({age})' if age else ''}")
    return ToolOutcome("\n".join(lines))


def _authors(work: dict) -> str:
    names = [
        (authorship.get("author") or {}).get("display_name")
        for authorship in work.get("authorships") or []
        if isinstance(authorship, dict)
    ]
    names = [name for name in names if name]
    if not names:
        return "unknown authors"
    return ", ".join(names[:3]) + (" et al." if len(names) > 3 else "")


async def search_papers(
    query: str, *, url: str, api_key: str, timeout: float, user_agent: str
) -> ToolOutcome:
    """OpenAlex works by relevance: title, year, authors, venue, DOI, citations and the
    open-access PDF's URL where OpenAlex knows one (docs/chat.md § Tools)."""
    query = query.strip()
    if not query:
        return ToolOutcome("The query is empty.", is_error=True)
    params = {
        "search": query[:400],
        "per-page": RESULTS_PER_SEARCH,
        "select": (
            "id,doi,title,publication_year,authorships,primary_location,open_access,"
            "cited_by_count,best_oa_location"
        ),
    }
    # The key as a header, never in the URL: httpx logs every request's URL at INFO.
    try:
        async with asyncio.timeout(timeout), _search_client(timeout, user_agent) as client:
            response = await client.get(
                url, params=params, headers={"Authorization": f"Bearer {api_key}"}
            )
    except (httpx.HTTPError, TimeoutError) as exc:
        logger.warning("OpenAlex did not answer: %s", exc.__class__.__name__)
        return ToolOutcome(
            f"The paper search failed: {failure(exc, timeout)}; try again.", is_error=True
        )
    if response.status_code != 200:
        return _upstream_failure("OpenAlex", response)
    try:
        works = response.json().get("results", [])
    except ValueError:
        return ToolOutcome("OpenAlex answered something that is not JSON.", is_error=True)
    if not works:
        return ToolOutcome(f"No works match {query!r}.")
    lines = [f"Works matching {query!r} (OpenAlex):"]
    for index, work in enumerate(works[:RESULTS_PER_SEARCH], start=1):
        if not isinstance(work, dict):
            continue
        source = ((work.get("primary_location") or {}).get("source") or {}).get("display_name")
        pdf = (work.get("best_oa_location") or {}).get("pdf_url") or (
            work.get("open_access") or {}
        ).get("oa_url")
        page = (work.get("primary_location") or {}).get("landing_page_url")
        year = work.get("publication_year") or "n.d."
        head = f"{index}. {work.get('title') or '(untitled)'} ({year})"
        lines.append(f"{head} — {_authors(work)}" + (f" — {source}" if source else ""))
        facts = []
        if work.get("doi"):
            facts.append(f"DOI: {work['doi']}")
        facts.append(f"cited by {int(work.get('cited_by_count') or 0)}")
        if pdf:
            facts.append(f"PDF: {pdf}")
        elif page:
            facts.append(f"page: {page}")
        lines.append("   " + " · ".join(facts))
    return ToolOutcome("\n".join(lines))


# --- reading a URL ----------------------------------------------------------------------------


class _TextExtractor(HTMLParser):
    """A page's text: scripts, styles and the like dropped, block elements as line breaks,
    the title kept apart."""

    SKIP = frozenset({"script", "style", "noscript", "template", "svg", "iframe", "head"})
    BLOCK = frozenset(
        {
            "p", "div", "br", "li", "h1", "h2", "h3", "h4", "h5", "h6", "tr", "section",
            "article", "header", "footer", "blockquote", "pre", "table", "ul", "ol", "hr",
            "nav", "aside", "main", "dd", "dt", "figure", "figcaption", "td", "th",
        }
    )  # fmt: skip

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.title: list[str] = []
        self._skipping = 0
        self._in_title = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "title":
            self._in_title = True
        elif tag in self.SKIP:
            self._skipping += 1
        elif tag in self.BLOCK:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag == "title":
            self._in_title = False
        elif tag in self.SKIP:
            self._skipping = max(0, self._skipping - 1)
        elif tag in self.BLOCK:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self.title.append(data)
        elif not self._skipping:
            self.parts.append(data)


_BLANK_LINES_RE = re.compile(r"\n\s*\n\s*\n+")
_SPACES_RE = re.compile(r"[ \t\r\f\v]+")


def html_to_text(markup: str) -> tuple[str, str]:
    """The page's title and its text, whitespace collapsed to what reads."""
    extractor = _TextExtractor()
    extractor.feed(markup)
    extractor.close()
    text = _SPACES_RE.sub(" ", "".join(extractor.parts))
    text = "\n".join(line.strip() for line in text.split("\n"))
    text = _BLANK_LINES_RE.sub("\n\n", text).strip()
    return _SPACES_RE.sub(" ", "".join(extractor.title)).strip(), text


def _decode(data: bytes, charset: str | None) -> str:
    """Bytes as text, the declared charset first, UTF-8 (BOM or not) otherwise, with
    replacement rather than refusal; a character the cap split is dropped."""
    for encoding in ((charset,) if charset else ()) + ("utf-8-sig",):
        try:
            decoder = codecs.getincrementaldecoder(encoding)("replace")
        except LookupError:
            continue
        return decoder.decode(data).replace("\x00", "")
    return data.decode("utf-8", "replace")


async def read_url(
    url: str,
    *,
    max_bytes: int,
    max_chars: int,
    timeout: float,
    pdf_timeout: float,
    user_agent: str,
) -> ToolOutcome:
    """One URL as text (docs/chat.md § Tools): the page's title and text, a PDF's text
    layer, a text file as it is; cut at `max_chars` like every tool result."""
    try:
        async with fetch(url, timeout=timeout, user_agent=user_agent) as (response, final_url):
            if response.status_code >= 400:
                return ToolOutcome(
                    f"{shown(final_url)} answered HTTP {response.status_code}; {ANOTHER_SOURCE}.",
                    is_error=True,
                )
            media_type, charset = media_type_of(response)
            if media_type and not (
                media_type == PDF_MEDIA_TYPE
                or media_type.startswith("text/")
                or media_type in TEXT_MEDIA_TYPES
                or media_type.endswith("+xml")
                or media_type.endswith("+json")
            ):
                return ToolOutcome(NOT_TEXT.format(media_type=media_type), is_error=True)
            data, cut = await read_capped(response, max_bytes)
    except FetchRefused as exc:
        return ToolOutcome(str(exc), is_error=True)
    except (httpx.HTTPError, TimeoutError) as exc:
        logger.info("read_url could not fetch: %s", exc.__class__.__name__)
        return ToolOutcome(
            f"The URL could not be read: {failure(exc, timeout)}; {ANOTHER_SOURCE}.",
            is_error=True,
        )
    if not media_type:
        # The server said nothing: a PDF by its magic, else text.
        media_type = PDF_MEDIA_TYPE if data.startswith(b"%PDF-") else "text/plain"
    if media_type == PDF_MEDIA_TYPE:
        title = ""
        try:
            text, goes_on = await _pdf_text(data, max_chars=max_chars, timeout=pdf_timeout)
        except poppler.PopplerError:
            return ToolOutcome("The PDF could not be read.", is_error=True)
        cut = cut or goes_on
    elif media_type in ("text/html", "application/xhtml+xml"):
        title, text = html_to_text(_decode(data, charset))
    else:
        title, text = "", _decode(data, charset)
    if not text.strip():
        return ToolOutcome(f"{shown(final_url)} has no text to read.", is_error=True)
    source = shown(final_url)
    head = f"{title[:TITLE_MAX_CHARS]}\n{source}\n\n" if title else f"{source}\n\n"
    result = truncate(head + text, max_chars)
    if cut and len(result) == len(head) + len(text):
        result += "\n\n[... the content goes on past what was read ...]"
    return ToolOutcome(result)


async def _pdf_text(data: bytes, *, max_chars: int, timeout: float) -> tuple[str, bool]:
    """A PDF's text layer through Poppler, from a file of its own in a private directory
    (docs/parsing.md); four bytes a character at most, so the cap always holds."""
    with tempfile.TemporaryDirectory(prefix="sr-read-url-") as work:
        path = Path(work) / "page.pdf"
        path.write_bytes(data)
        text_bytes, goes_on = await poppler.pdf_text(
            path, cwd=Path(work), timeout=timeout, max_bytes=4 * max_chars
        )
    return _decode(text_bytes, None), goes_on


# --- downloading --------------------------------------------------------------------------------


@dataclass(frozen=True)
class Downloaded:
    """What a download streamed to a staging file: its size, the name the server or the
    URL gave it, and its media type as the server said it."""

    size: int
    filename: str
    media_type: str | None


_SEGMENT_RE = re.compile(r"[^/]+$")


def server_filename(response: httpx.Response, final_url: str) -> str:
    """The name a download carries when the call gave none: `Content-Disposition`'s, else
    the URL path's last segment, else `download`."""
    message = Message()
    message["content-disposition"] = response.headers.get("content-disposition", "")
    with contextlib.suppress(Exception):
        name = message.get_filename()
        if name and name.strip():
            return name.strip()
    path = urllib.parse.unquote(httpx.URL(final_url).path)
    match = _SEGMENT_RE.search(path)
    return match.group(0) if match and match.group(0).strip() else "download"


def with_extension(name: str, media_type: str | None) -> str:
    """A name with no extension takes the media type's: `paper` served as a PDF is
    `paper.pdf`, so the file opens as what it is."""
    if "." in name.strip(".") or not media_type:
        return name
    guessed = mimetypes.guess_extension(media_type.split(";", 1)[0].strip().lower())
    return f"{name}{guessed}" if guessed else name


async def download(
    url: str,
    fd: int,
    *,
    max_bytes: int,
    room: int,
    timeout: float,
    user_agent: str,
) -> Downloaded:
    """One URL streamed into the open descriptor `fd` — the staging file the caller made
    at the top of `files/` — up to `max_bytes` and up to `room`, the storage the quota
    still allows (docs/chat.md § Tools, docs/workspace.md § Files). A body past either is
    refused: `FetchRefused` names which, and the caller removes the staging file."""
    cap = min(max_bytes, room)
    async with fetch(url, timeout=timeout, user_agent=user_agent) as (response, final_url):
        if response.status_code >= 400:
            raise FetchRefused(
                f"{shown(final_url)} answered HTTP {response.status_code}; {ANOTHER_SOURCE}."
            )
        declared = response.headers.get("content-length")
        if declared and declared.isdigit() and int(declared) > cap:
            raise FetchRefused(_too_large(int(declared), max_bytes, room))
        size = 0
        async for chunk in iter_body(response):
            size += len(chunk)
            if size > cap:
                raise FetchRefused(_too_large(size, max_bytes, room))
            await asyncio.to_thread(_write_all, fd, chunk)
        media_type, _charset = media_type_of(response)
        return Downloaded(
            size=size,
            filename=server_filename(response, final_url),
            # None where the server said nothing, or only octet-stream: the name's
            # extension decides then, as it does for an upload (docs/workspace.md § Files).
            media_type=media_type
            if media_type and media_type != "application/octet-stream"
            else None,
        )


def _too_large(size: int, max_bytes: int, room: int) -> str:
    if size > max_bytes:
        return f"The file is larger than the {max_bytes:,} bytes a download may be."
    return "The user's storage is full; the file does not fit."


def _write_all(fd: int, chunk: bytes) -> None:
    view = memoryview(chunk)
    while view:
        written = os.write(fd, view)
        view = view[written:]
