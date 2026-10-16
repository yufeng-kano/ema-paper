"""A ceiling on request bodies the API would otherwise buffer whole (docs/api.md).

Uploads stream to disk under `UPLOAD_MAX_BYTES` and are not touched here: a multipart
body on an upload route is theirs. Everything else — JSON a route parses in memory, a
document save above all — is refused past `JSON_BODY_MAX_BYTES`, by the declared length
when there is one and by counting the bytes as they arrive when there is not, before
any of it reaches the route. The JSON form of `/v1/parse`, which `request.json()` holds
whole, gets its own ceiling: the upload cap in base64 plus the envelope.
"""

from __future__ import annotations

import json

from starlette.types import ASGIApp, Message, Receive, Scope, Send

TOO_LARGE = {"error": {"type": "invalid_request", "message": "The request body is too large"}}


# The routes that stream a multipart body to disk under UPLOAD_MAX_BYTES themselves.
PARSE_ROUTE = ("POST", "/v1/parse")
STREAMING_UPLOADS = frozenset({PARSE_ROUTE, ("POST", "/api/library/documents")})
MULTIPART = b"multipart/form-data"


class BodyTooLarge(Exception):
    """Raised from the request's own `receive`, inside the route, so the app's error
    handlers answer it (413) like any other request failure."""


class BodyLimitMiddleware:
    def __init__(self, app: ASGIApp, *, max_bytes: int, parse_json_max_bytes: int) -> None:
        self.app = app
        self.max_bytes = max_bytes
        self.parse_json_max_bytes = parse_json_max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = {key.lower(): value for key, value in scope.get("headers", [])}
        route = (scope.get("method"), scope.get("path"))
        content_type = headers.get(b"content-type", b"").lower()
        if route in STREAMING_UPLOADS and content_type.startswith(MULTIPART):
            # The only routes that read a body without holding it, and only their
            # multipart form (docs/api.md § POST /v1/parse); a multipart label on any
            # other route buys no exemption, and a JSON body on these is counted.
            await self.app(scope, receive, send)
            return
        limit = self.parse_json_max_bytes if route == PARSE_ROUTE else self.max_bytes
        declared = headers.get(b"content-length", b"")
        if declared.isdigit() and int(declared) > limit:
            await _refuse(send)
            return

        received = 0
        overflowed = False
        body = json.dumps(TOO_LARGE).encode()

        async def counted() -> Message:
            nonlocal received, overflowed
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > limit:
                    overflowed = True
                    raise BodyTooLarge()
            return message

        async def answered(message: Message) -> None:
            # The framework turns a failed body read into its own 400; what the client
            # must hear is 413, so the response is rewritten on its way out.
            if overflowed and message["type"] == "http.response.start":
                message = {
                    "type": "http.response.start",
                    "status": 413,
                    "headers": [
                        (b"content-type", b"application/json"),
                        (b"content-length", str(len(body)).encode()),
                    ],
                }
            elif overflowed and message["type"] == "http.response.body":
                message = {"type": "http.response.body", "body": body, "more_body": False}
            await send(message)

        await self.app(scope, counted, answered)


async def _refuse(send: Send) -> None:
    body = json.dumps(TOO_LARGE).encode()
    await send(
        {
            "type": "http.response.start",
            "status": 413,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode()),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})
