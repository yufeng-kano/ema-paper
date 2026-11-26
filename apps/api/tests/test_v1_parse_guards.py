"""Request-level guards on `POST /v1/parse`: limits, caps, bad input and model choice."""

from __future__ import annotations

import base64
import io
import json
import struct
import uuid
import zlib
from pathlib import Path

import httpx
import pytest
from httpx import AsyncClient
from PIL import Image
from sqlalchemy import select

from sightread.auth.api_keys import create_api_key
from sightread.db.models import Job, utcnow
from tests.billing_helpers import add_parses
from tests.conftest import PARSE_MODEL, api_key_client
from tests.factories import user_row
from tests.helpers import staged_uploads, v1_parse


async def test_a_corrupt_pdf_is_rejected_before_a_job_exists(
    client: AsyncClient, documents
) -> None:
    await api_key_client(client)
    response = await v1_parse(client, documents["corrupt_pdf"])

    assert response.status_code == 400
    assert "PDF" in response.json()["error"]["message"]
    assert staged_uploads(client) == []


async def test_the_page_cap_is_enforced(make_client, documents) -> None:
    client = await api_key_client(make_client(page_cap=2))
    response = await v1_parse(client, documents["mixed_pdf"])

    assert response.status_code == 400
    assert "page cap" in response.json()["error"]["message"]


async def test_the_size_cap_is_enforced_while_streaming(make_client, documents) -> None:
    # A cap just under the file size: the cheap content-length check lets this through, so
    # the streaming copy is what has to stop it.
    size = documents["mixed_pdf"].stat().st_size
    client = await api_key_client(make_client(upload_max_bytes=size - 1))
    response = await v1_parse(client, documents["mixed_pdf"])

    assert response.status_code == 413
    upload_dir = Path(client.app.state.settings.upload_dir)
    assert list(upload_dir.iterdir()) == []


async def test_an_oversized_body_is_refused_before_it_is_read(make_client, documents) -> None:
    client = await api_key_client(make_client(upload_max_bytes=64))
    response = await v1_parse(client, documents["mixed_pdf"])

    assert response.status_code == 413
    assert staged_uploads(client) == []


async def test_an_unsupported_media_type_is_rejected(client: AsyncClient, tmp_path) -> None:
    await api_key_client(client)
    document = tmp_path / "notes.txt"
    document.write_text("plain text")

    response = await v1_parse(client, document, media_type="text/plain", model=PARSE_MODEL)
    assert response.status_code == 400


def _png_claiming(width: int, height: int) -> bytes:
    """A tiny PNG whose header claims `width` x `height` 1-bit pixels."""

    def chunk(kind: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
        )

    header = struct.pack(">IIBBBBB", width, height, 1, 0, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(b""))
        + chunk(b"IEND", b"")
    )


def _bmp() -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (4, 4), (255, 0, 0)).save(buffer, format="BMP")
    return buffer.getvalue()


@pytest.mark.parametrize(
    "data",
    [
        # Past Pillow's decompression-bomb bound, from a file of a few dozen bytes.
        pytest.param(_png_claiming(20000, 10000), id="bomb"),
        # Decoded only as one of the accepted formats, whatever the name says.
        pytest.param(_bmp(), id="bmp-named-png"),
    ],
)
async def test_an_image_that_does_not_decode_as_an_accepted_one_is_refused(
    client: AsyncClient, data: bytes
) -> None:
    await api_key_client(client)
    response = await client.post(
        "/v1/parse",
        files={"file": ("scan.png", data, "image/png")},
        data={"model": PARSE_MODEL},
    )

    assert response.status_code == 400
    assert response.json()["error"]["message"] == "The image could not be decoded"
    assert staged_uploads(client) == []


async def test_an_invalid_page_selection_is_rejected(client: AsyncClient, documents) -> None:
    await api_key_client(client)
    response = await v1_parse(client, documents["mixed_pdf"], pages="4-9")

    assert response.status_code == 400
    assert "outside" in response.json()["error"]["message"]


async def test_the_per_user_running_cap_returns_429(
    client: AsyncClient, sessionmaker, documents
) -> None:
    await api_key_client(client)
    user_id = client.user_id

    await add_parses(sessionmaker, user_id, 2, status="running", started_at=utcnow())

    response = await v1_parse(client, documents["text_pdf"])

    assert response.status_code == 429
    assert response.json()["error"]["type"] == "rate_limit"
    assert response.headers["Retry-After"] == "30"


async def test_the_json_base64_fallback_creates_the_same_job(
    client: AsyncClient, sessionmaker, documents
) -> None:
    await api_key_client(client)
    encoded = base64.b64encode(documents["text_pdf"].read_bytes()).decode("ascii")

    response = await client.post(
        "/v1/parse",
        json={
            "source": encoded,
            "filename": "text.pdf",
            "media_type": "application/pdf",
            "model": PARSE_MODEL,
        },
    )

    assert response.status_code == 202
    async with sessionmaker() as db:
        job = await db.get(Job, uuid.UUID(response.json()["job_id"]))
    assert job.kind == "pdf"
    assert job.size_bytes == documents["text_pdf"].stat().st_size
    assert Path(job.source_path).exists()


async def test_broken_base64_is_rejected(client: AsyncClient) -> None:
    await api_key_client(client)
    response = await client.post(
        "/v1/parse", json={"source": "not base64 at all!!", "model": PARSE_MODEL}
    )
    assert response.status_code == 400


async def test_a_job_runs_on_the_deployments_parse_model(
    client: AsyncClient, sessionmaker, documents
) -> None:
    """No model in the request: the deployment's parse model, never a stored pick
    (docs/agents.md § Enqueue rules)."""
    await api_key_client(client)
    response = await v1_parse(client, documents["text_pdf"])

    assert response.status_code == 202, response.text
    async with sessionmaker() as db:
        job = await db.get(Job, uuid.UUID(response.json()["job_id"]))
    assert (job.model, job.agent) == (PARSE_MODEL, "parse")


async def test_a_deployment_without_a_parse_model_refuses_every_parse(
    make_client, sessionmaker, documents
) -> None:
    client = await api_key_client(make_client(parse_model=""))
    # Naming a model does not turn the 503 into the 400 for a model not offered.
    for fields in ({}, {"model": PARSE_MODEL}):
        response = await v1_parse(client, documents["text_pdf"], **fields)

        assert response.status_code == 503, fields
        message = response.json()["error"]["message"]
        assert message == "No parse model is configured on this deployment", fields
    async with sessionmaker() as db:
        assert (await db.execute(select(Job))).scalars().all() == []
    assert staged_uploads(client) == []


async def test_a_deployment_without_kano_proxy_refuses_every_parse(make_client, documents) -> None:
    for overrides in ({"kano_proxy_base_url": ""}, {"kano_proxy_api_key": ""}):
        client = await api_key_client(make_client(**overrides))
        response = await v1_parse(client, documents["text_pdf"])

        assert response.status_code == 503, overrides
        assert "not configured" in response.json()["error"]["message"]


async def test_jobs_of_other_users_are_invisible(make_client, sessionmaker, documents) -> None:
    owner = await api_key_client(make_client())
    accepted = await v1_parse(owner, documents["text_pdf"])
    job_id = accepted.json()["job_id"]

    # A second identity: the dev login always signs in the same user, so the other account
    # is a row of its own with a key made straight through the module.
    stranger = make_client()
    async with sessionmaker() as db:
        other = user_row("stranger@example.com")
        db.add(other)
        await db.flush()
        _, plaintext = await create_api_key(db, other, "stranger")
        await db.commit()

    stranger.headers["Authorization"] = f"Bearer {plaintext}"
    assert (await stranger.get(f"/v1/jobs/{job_id}")).status_code == 404
    assert (await stranger.get(f"/v1/jobs/{job_id}/result")).status_code == 404


async def test_a_chunked_json_body_is_counted_against_the_parse_cap(make_client) -> None:
    client = await api_key_client(make_client(upload_max_bytes=64))
    cap = client.app.state.settings.parse_json_body_max_bytes
    # No Content-Length: the bytes are counted as they arrive, and a base64 source past the
    # cap is refused before the route can buffer it whole.
    payload = json.dumps({"source": "A" * (cap + 1024), "filename": "big.pdf"}).encode()

    async def chunks():
        for start in range(0, len(payload), 4096):
            yield payload[start : start + 4096]

    response = await client.post(
        "/v1/parse", content=chunks(), headers={"Content-Type": "application/json"}
    )
    assert response.status_code == 413
    # A declared length past the cap is refused before a byte is read.
    declared = await client.post(
        "/v1/parse",
        content=payload,
        headers={"Content-Type": "application/json", "Content-Length": str(len(payload))},
    )
    assert declared.status_code == 413
    # Under the ceiling, the route's own checks answer: 64 bytes of upload is the cap here.
    small = base64.b64encode(b"%PDF-1.4 " + b"x" * 200).decode()
    over = await client.post("/v1/parse", json={"source": small, "filename": "a.pdf"})
    assert over.status_code == 413


def _multipart(content: bytes) -> tuple[bytes, str]:
    """A `/v1/parse` form carrying `content` as its PDF: the body and its Content-Type."""
    request = httpx.Request(
        "POST",
        "https://testserver/v1/parse",
        files={"file": ("text.pdf", content, "application/pdf")},
        data={"model": PARSE_MODEL},
    )
    return request.read(), request.headers["content-type"]


async def test_a_multipart_label_in_any_letter_case_is_read_as_the_form(
    client: AsyncClient, documents
) -> None:
    """MIME types ignore case, as the body-limit middleware does: the route reads such a
    body as the form, never as JSON it would hold whole."""
    await api_key_client(client)
    body, content_type = _multipart(documents["text_pdf"].read_bytes())

    answers = [
        await client.post("/v1/parse", content=body, headers={"Content-Type": label})
        for label in (
            content_type,
            content_type.replace("multipart/form-data", "Multipart/Form-Data"),
        )
    ]

    assert [answer.status_code for answer in answers] == [202, 202], answers[1].text
    assert answers[0].json()["job_id"] == answers[1].json()["job_id"]


async def test_an_oversized_multipart_body_in_another_letter_case_stops_at_the_cap(
    make_client,
) -> None:
    client = await api_key_client(make_client(upload_max_bytes=64))
    body, content_type = _multipart(b"%PDF-1.4 " + b"x" * (512 * 1024))
    chunks = [body[start : start + 4096] for start in range(0, len(body), 4096)]
    sent = 0

    async def undeclared():
        # No Content-Length: only the capped reader can stop this body.
        nonlocal sent
        for chunk in chunks:
            sent += 1
            yield chunk

    response = await client.post(
        "/v1/parse",
        content=undeclared(),
        headers={
            "Content-Type": content_type.replace("multipart/form-data", "MULTIPART/FORM-DATA")
        },
    )

    assert response.status_code == 413
    assert sent < len(chunks)


async def test_a_repeated_content_type_is_read_as_the_route_reads_it(make_client) -> None:
    """The middleware decides multipart on the Content-Type the route reads, the first of
    repeated ones: a JSON body that also claims to be a form is counted at the JSON cap."""
    client = await api_key_client(make_client(upload_max_bytes=64))
    cap = client.app.state.settings.parse_json_body_max_bytes
    payload = json.dumps({"source": "A" * (cap * 4), "filename": "big.pdf"}).encode()
    chunks = [payload[start : start + 4096] for start in range(0, len(payload), 4096)]
    sent = 0

    async def undeclared():
        # No Content-Length: only the middleware's count can stop this body.
        nonlocal sent
        for chunk in chunks:
            sent += 1
            yield chunk

    response = await client.post(
        "/v1/parse",
        content=undeclared(),
        headers=[
            ("Content-Type", "application/json"),
            ("Content-Type", "multipart/form-data; boundary=x"),
        ],
    )

    assert response.status_code == 413
    assert sent < len(chunks)
