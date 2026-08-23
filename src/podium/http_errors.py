"""Typed RFC 9457 problem responses and request correlation for the HTTP API."""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable, Mapping
from contextvars import ContextVar
from http import HTTPStatus
from typing import Annotated, cast
from uuid import uuid4

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, ConfigDict, StringConstraints
from sqlalchemy.exc import IntegrityError
from starlette.exceptions import HTTPException as StarletteHTTPException

PROBLEM_MEDIA_TYPE = "application/problem+json"
REQUEST_ID_HEADER = "X-Request-ID"
RequestId = Annotated[
    str,
    StringConstraints(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9._~-]+$"),
]
_REQUEST_ID: ContextVar[str] = ContextVar("request_id")
_REQUEST_ID_PATTERN = re.compile(r"^[A-Za-z0-9._~-]{1,128}$")
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
_ERROR_STATUSES = tuple(_STATUS_CODES)
_OPENAPI_ERROR_STATUSES = frozenset(str(status) for status in _ERROR_STATUSES)


class ValidationErrorItem(BaseModel):
    """One invalid request location, kept separate from framework error dictionaries."""

    model_config = ConfigDict(frozen=True)

    field: str
    message: str
    code: str


class ProblemDetails(BaseModel):
    """RFC 9457 problem detail with Podium's request-correlation extension."""

    model_config = ConfigDict(frozen=True)

    type: str
    title: str
    status: int
    detail: str
    instance: str
    trace_id: RequestId
    errors: tuple[ValidationErrorItem, ...] | None = None


class ProblemHTTPException(StarletteHTTPException):
    """An HTTP exception with an explicit RFC 9457 problem type."""

    def __init__(
        self,
        *,
        status_code: int,
        code: str,
        detail: str,
        headers: Mapping[str, str] | None = None,
    ) -> None:
        super().__init__(status_code=status_code, detail=detail, headers=headers)
        self.code = code


def get_request_id() -> str:
    """Return the request ID for the current HTTP handler or exception handler."""
    return _REQUEST_ID.get()


def _incoming_or_generated_request_id(request: Request) -> str:
    incoming = request.headers.get(REQUEST_ID_HEADER)
    if incoming is not None and _REQUEST_ID_PATTERN.fullmatch(incoming):
        return incoming
    return str(uuid4())


async def _with_request_id(
    request: Request, call_next: Callable[[Request], Awaitable[Response]]
) -> Response:
    request_id = _incoming_or_generated_request_id(request)
    token = _REQUEST_ID.set(request_id)
    try:
        response = await call_next(request)
    finally:
        _REQUEST_ID.reset(token)
    response.headers[REQUEST_ID_HEADER] = request_id
    return response


def _problem(
    request: Request,
    *,
    status: int,
    code: str,
    detail: str,
    errors: tuple[ValidationErrorItem, ...] | None = None,
) -> ProblemDetails:
    return ProblemDetails(
        type=f"urn:podium:problem:{code}",
        title=HTTPStatus(status).phrase,
        status=status,
        detail=detail,
        instance=request.url.path,
        trace_id=get_request_id(),
        errors=errors,
    )


def _problem_response(
    problem: ProblemDetails, *, headers: Mapping[str, str] | None = None
) -> JSONResponse:
    return JSONResponse(
        problem.model_dump(mode="json", exclude_none=True),
        status_code=problem.status,
        headers=headers,
        media_type=PROBLEM_MEDIA_TYPE,
    )


async def _on_http_exception(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, StarletteHTTPException)
    status = exc.status_code
    code = exc.code if isinstance(exc, ProblemHTTPException) else _STATUS_CODES.get(status, "error")
    return _problem_response(
        _problem(request, status=status, code=code, detail=str(exc.detail)),
        headers=exc.headers,
    )


async def _on_validation_error(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, RequestValidationError)
    errors = tuple(
        ValidationErrorItem(
            field=".".join(str(part) for part in error["loc"] if part != "body"),
            message=str(error["msg"]),
            code=str(error["type"]),
        )
        for error in exc.errors()
    )
    return _problem_response(
        _problem(
            request,
            status=422,
            code="validation_error",
            detail="Request validation failed",
            errors=errors,
        )
    )


async def _on_integrity_error(request: Request, exc: Exception) -> JSONResponse:
    # A unique/FK violation is a client conflict, not a 500 — and its SQL text never reaches the wire.
    return _problem_response(
        _problem(request, status=409, code="conflict", detail="resource conflict")
    )


def problem_responses() -> dict[int | str, dict[str, object]]:
    """Use FastAPI's application-wide response declaration for every HTTP operation."""
    return {status: {"model": ProblemDetails} for status in _ERROR_STATUSES}


def cache_problem_openapi(app: FastAPI) -> None:
    """Cache the generated schema after replacing only declared error media types."""
    schema = app.openapi()
    paths = schema["paths"]
    for path in paths.values():
        for operation in path.values():
            responses = operation.get("responses")
            if responses is None:
                continue
            for status, response in responses.items():
                if status not in _OPENAPI_ERROR_STATUSES:
                    continue
                content = response.get("content")
                if content is None:
                    continue
                problem_content = content.pop("application/json", None)
                if problem_content is not None:
                    content[PROBLEM_MEDIA_TYPE] = problem_content


def install_error_handlers(app: FastAPI) -> None:
    responses = cast(dict[int | str, dict[str, object]], app.router.responses)
    responses.update(problem_responses())
    app.middleware("http")(_with_request_id)
    app.add_exception_handler(StarletteHTTPException, _on_http_exception)
    app.add_exception_handler(RequestValidationError, _on_validation_error)
    app.add_exception_handler(IntegrityError, _on_integrity_error)
