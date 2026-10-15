"""A ceiling on request bodies the API would otherwise buffer whole (docs/api.md).

Uploads stream to disk under `UPLOAD_MAX_BYTES` and are not touched here: a multipart
body is theirs. Everything else — JSON a route parses in memory, a document save above
all — is refused past `JSON_BODY_MAX_BYTES`, by the declared length when there is one
and by counting the bytes as they arrive when there is not, before any of it reaches
the route.
"""

from __future__ import annotations

import json

from starlette.types import ASGIApp, Message, Receive, Scope, Send

TOO_LARGE = {"error": {"type": "invalid_request", "message": "The request body is too large"}}


# The routes that stream their body to disk under UPLOAD_MAX_BYTES themselves.
STREAMING_UPLOADS = frozenset({("POST", "/v1/parse"), ("POST", "/api/library/documents")})


class BodyTooLarge(Exception):
    """Raised from the request's own `receive`, inside the route, so the app's error
    handlers answer it (413) like any other request failure."""


class BodyLimitMiddleware:
    def __init__(self, app: ASGIApp, *, max_bytes: int) -> None:
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = {key.lower(): value for key, value in scope.get("headers", [])}
        if (scope.get("method"), scope.get("path")) in STREAMING_UPLOADS:
            # The only routes that read a body without holding it (docs/api.md § POST
            # /v1/parse); a multipart label on any other route buys no exemption.
            await self.app(scope, receive, send)
            return
        declared = headers.get(b"content-length", b"")
        if declared.isdigit() and int(declared) > self.max_bytes:
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
                if received > self.max_bytes:
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
