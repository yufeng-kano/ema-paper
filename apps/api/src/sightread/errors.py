"""The single error shape used by both planes (docs/api.md)."""

from __future__ import annotations

import logging
from typing import Literal

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from .body_limit import BodyTooLarge

# `revision_conflict`: a save whose `base_revision` is not the file's current one — the
# one 409 a client answers by reloading, apart from every other 409 (docs/api.md).
ErrorType = Literal[
    "invalid_request",
    "revision_conflict",
    "auth",
    "rate_limit",
    "payment",
    "insufficient_credits",
    "upstream",
    "internal",
]

_STATUS_TO_TYPE: dict[int, ErrorType] = {
    400: "invalid_request",
    401: "auth",
    403: "auth",
    404: "invalid_request",
    405: "invalid_request",
    409: "invalid_request",
    413: "invalid_request",
    422: "invalid_request",
    429: "rate_limit",
    402: "payment",
    501: "internal",
    502: "upstream",
    503: "upstream",
    504: "upstream",
}

logger = logging.getLogger(__name__)


class ApiError(Exception):
    """Raise anywhere; the handlers below render it as the documented error envelope."""

    def __init__(
        self,
        status_code: int,
        error_type: ErrorType,
        message: str,
        headers: dict[str, str] | None = None,
        details: dict | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.error_type = error_type
        self.message = message
        self.headers = headers
        # Extra fields inside `error`, for the one refusal a client acts on by number:
        # `insufficient_credits` carries `need`, `remaining` and `period_end`
        # (docs/billing.md § Refusals).
        self.details = details


def error_response(
    status_code: int,
    error_type: ErrorType,
    message: str,
    headers: dict[str, str] | None = None,
    details: dict | None = None,
) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={"error": {"type": error_type, "message": message, **(details or {})}},
        headers=headers,
    )


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(ApiError)
    async def _api_error(_: Request, exc: ApiError) -> JSONResponse:
        return error_response(
            exc.status_code, exc.error_type, exc.message, exc.headers, exc.details
        )

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(_: Request, exc: StarletteHTTPException) -> JSONResponse:
        error_type = _STATUS_TO_TYPE.get(exc.status_code, "internal")
        message = exc.detail if isinstance(exc.detail, str) else "Request failed"
        return error_response(exc.status_code, error_type, message, dict(exc.headers or {}))

    @app.exception_handler(RequestValidationError)
    async def _validation_error(_: Request, exc: RequestValidationError) -> JSONResponse:
        first = exc.errors()[0] if exc.errors() else None
        where = ".".join(str(part) for part in first["loc"]) if first else "request"
        message = f"{where}: {first['msg']}" if first else "Invalid request"
        return error_response(422, "invalid_request", message)

    @app.exception_handler(BodyTooLarge)
    async def _body_too_large(_: Request, exc: BodyTooLarge) -> JSONResponse:
        return JSONResponse(
            {"error": {"type": "invalid_request", "message": "The request body is too large"}},
            status_code=413,
        )

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception) -> JSONResponse:
        # The traceback goes to the log; the client gets nothing that could leak
        # credential material or internals (docs/auth.md § Logging rule).
        logger.exception("Unhandled error on %s %s", request.method, request.url.path)
        return error_response(500, "internal", "Internal error")
