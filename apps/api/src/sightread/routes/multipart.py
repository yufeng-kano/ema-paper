"""The library's upload, read as it arrives (docs/api.md § `POST /api/library/documents`).

Starlette's form parser spools every file part to a temporary file of its own before a route
sees it. A file of the library is streamed into the user's tree instead, under the cap of its
kind, so a two-gigabyte upload is written once, where it will live, and never into the
system's temporary directory (docs/workspace.md § Files). The parser is python-multipart's —
the one Starlette drives — driven here with callbacks that write the one file part through a
descriptor the route opened for it and keep the small fields in memory.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
from collections.abc import AsyncIterator, Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

from python_multipart.exceptions import FormParserError
from python_multipart.multipart import MultipartParser, parse_options_header

from ..errors import ApiError

# A field of the form (`folder_id`) is a few characters; anything longer is not a field.
FIELD_MAX_BYTES = 1024
FIELDS_MAX = 16
UNREADABLE = "The multipart body could not be read"


@dataclass(frozen=True)
class Sink:
    """Where the file part goes: a descriptor the route opened for it, the path it is at,
    the cap of its kind with the name of the setting (for the 413), which kind of filing
    the route decided on, and how to take the file back when the upload fails."""

    fd: int
    path: Path
    max_bytes: int
    cap_name: str
    kind: str
    discard: Callable[[], None]


@dataclass
class UploadPart:
    """The one file part: the name and type the client gave it, where it streamed to, and
    how many bytes arrived."""

    filename: str
    content_type: str
    sink: Sink
    size: int = 0


class _Form:
    """The callbacks python-multipart drives, one part at a time."""

    def __init__(self, open_sink: Callable[[str, str], Sink], charset: str) -> None:
        self.open_sink = open_sink
        self.charset = charset
        self.fields: dict[str, str] = {}
        self.part: UploadPart | None = None
        self._headers: dict[bytes, bytes] = {}
        self._header_name = bytearray()
        self._header_value = bytearray()
        self._field_name: str | None = None
        self._field_data = bytearray()
        self._writing = False

    def on_part_begin(self) -> None:
        self._headers = {}
        self._header_name = bytearray()
        self._header_value = bytearray()
        self._field_name = None
        self._field_data = bytearray()
        self._writing = False

    def on_header_field(self, data: bytes, start: int, end: int) -> None:
        self._header_name += data[start:end]

    def on_header_value(self, data: bytes, start: int, end: int) -> None:
        self._header_value += data[start:end]

    def on_header_end(self) -> None:
        self._headers[bytes(self._header_name).lower()] = bytes(self._header_value)
        self._header_name = bytearray()
        self._header_value = bytearray()

    def on_headers_finished(self) -> None:
        _disposition, params = parse_options_header(self._headers.get(b"content-disposition", b""))
        name = params.get(b"name", b"").decode(self.charset, "replace")
        filename = params.get(b"filename")
        if filename is None:
            # A plain field, kept whole in memory — a bounded one.
            if len(self.fields) >= FIELDS_MAX:
                raise ApiError(400, "invalid_request", "The upload has too many fields")
            self._field_name = name
            return
        if name != "file":
            raise ApiError(400, "invalid_request", "Multipart requests need a 'file' part")
        if self.part is not None:
            raise ApiError(400, "invalid_request", "An upload carries one file")
        decoded = filename.decode(self.charset, "replace")
        content_type = self._headers.get(b"content-type", b"").decode("latin-1", "replace")
        # Where the bytes go is decided here, before the first of them, from the name and
        # the type alone (docs/workspace.md § Files).
        self.part = UploadPart(decoded, content_type, self.open_sink(decoded, content_type))
        self._writing = True

    def on_part_data(self, data: bytes, start: int, end: int) -> None:
        if self._writing:
            part = self.part
            assert part is not None
            part.size += end - start
            if part.size > part.sink.max_bytes:
                raise ApiError(413, "invalid_request", f"The upload exceeds {part.sink.cap_name}")
            view = memoryview(data)[start:end]
            while view:
                view = view[os.write(part.sink.fd, view) :]
            return
        self._field_data += data[start:end]
        if len(self._field_data) > FIELD_MAX_BYTES:
            raise ApiError(400, "invalid_request", "A field of the upload is too long")

    def on_part_end(self) -> None:
        if self._writing:
            self._writing = False
        elif self._field_name is not None:
            self.fields[self._field_name] = bytes(self._field_data).decode(self.charset, "replace")

    def close(self) -> None:
        if self.part is not None:
            os.close(self.part.sink.fd)

    def discard(self) -> None:
        if self.part is not None:
            with contextlib.suppress(OSError):
                os.close(self.part.sink.fd)
            with contextlib.suppress(Exception):
                self.part.sink.discard()


async def read_upload_form(
    headers: Mapping[str, str],
    stream: AsyncIterator[bytes],
    open_sink: Callable[[str, str], Sink],
) -> tuple[dict[str, str], UploadPart | None]:
    """Read a `multipart/form-data` body from `stream` (already capped by the caller),
    writing the `file` part into the sink `open_sink(filename, content_type)` returns and
    keeping the other fields. Returns the fields and the file part, None when the body
    carried none; the sink is closed either way, and taken back when the body could not
    be read or a cap refused it (400, 413 — raised from inside the parse)."""
    _kind, params = parse_options_header(headers.get("content-type", ""))
    boundary = params.get(b"boundary")
    if not boundary:
        raise ApiError(400, "invalid_request", UNREADABLE)
    charset = params.get(b"charset", b"utf-8").decode("latin-1", "replace")
    form = _Form(open_sink, charset)
    parser = MultipartParser(
        boundary,
        callbacks={
            "on_part_begin": form.on_part_begin,
            "on_part_data": form.on_part_data,
            "on_part_end": form.on_part_end,
            "on_header_field": form.on_header_field,
            "on_header_value": form.on_header_value,
            "on_header_end": form.on_header_end,
            "on_headers_finished": form.on_headers_finished,
        },
    )
    try:
        # Each chunk parsed, and the file part written, off the event loop: a large upload
        # is a long run of small blocking writes.
        async for chunk in stream:
            await asyncio.to_thread(parser.write, chunk)
        await asyncio.to_thread(parser.finalize)
    except FormParserError as exc:
        form.discard()
        raise ApiError(400, "invalid_request", UNREADABLE) from exc
    except BaseException:
        form.discard()
        raise
    form.close()
    return form.fields, form.part
