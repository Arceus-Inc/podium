"""One consistent error shape for the whole API: `{"error": {"code", "message", "details"?}}`.

Handlers translate the three exception families the app raises — HTTP errors, request-validation
failures, and DB integrity violations — into that envelope, and never leak internals (SQL, stack).
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy.exc import IntegrityError
from starlette.exceptions import HTTPException as StarletteHTTPException

_STATUS_CODES: dict[int, str] = {
    400: "bad_request",
    401: "unauthorized",
    403: "forbidden",
    404: "not_found",
    409: "conflict",
    422: "validation_error",
    429: "rate_limit_exceeded",
    500: "internal_error",
    503: "unavailable",
}


def _envelope(
    code: str, message: str, details: list[dict[str, str]] | None = None
) -> dict[str, Any]:
    error: dict[str, Any] = {"code": code, "message": message}
    if details is not None:
        error["details"] = details
    return {"error": error}


async def _on_http_exception(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, StarletteHTTPException)
    code = _STATUS_CODES.get(exc.status_code, "error")
    return JSONResponse(
        _envelope(code, str(exc.detail)), status_code=exc.status_code, headers=exc.headers
    )


async def _on_validation_error(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, RequestValidationError)
    details = [
        {
            "field": ".".join(str(p) for p in err["loc"] if p != "body"),
            "message": err["msg"],
            "code": err["type"],
        }
        for err in exc.errors()
    ]
    return JSONResponse(
        _envelope("validation_error", "Request validation failed", details), status_code=422
    )


async def _on_integrity_error(request: Request, exc: Exception) -> JSONResponse:
    # A unique/FK violation is a client conflict, not a 500 — and its SQL text never reaches the wire.
    return JSONResponse(_envelope("conflict", "resource conflict"), status_code=409)


def install_error_handlers(app: FastAPI) -> None:
    app.add_exception_handler(StarletteHTTPException, _on_http_exception)
    app.add_exception_handler(RequestValidationError, _on_validation_error)
    app.add_exception_handler(IntegrityError, _on_integrity_error)
