"""Runs HTTP surface: authenticate → decide → tenant_session. The workspace is always the actor's.

Creating a run first fetches the target company through the tenant session, so a company that RLS
hides (another tenant's) yields 404 — no run is ever created against a foreign company.

Ordinary run routes type path ids as `uuid.UUID`, so FastAPI rejects malformed ids with 422 before
any query runs. Session inspection routes deliberately accept strings and parse them locally,
returning the same opaque 404 for malformed, missing, and tenant-invisible company or run ids.
"""

from __future__ import annotations

import base64
import binascii
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Annotated, NoReturn, Protocol, TypeVar, runtime_checkable
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, Response
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from starlette.concurrency import run_in_threadpool

from podium.auth import Actor, Resource, decide, enforce_rate_limit, get_sessionmaker
from podium.companies import get_company
from podium.control import CompanyControlPlane
from podium.db import tenant_session
from podium.http_errors import ProblemHTTPException
from podium.logs import RunLogStore
from podium.runs.models import TERMINAL_STATUSES
from podium.runs.schemas import (
    RunCreate,
    RunOut,
    RunPage,
    RunPageLinks,
    RunPageMeta,
    RunSessionCheckpointOut,
)
from podium.runs.service import (
    IdempotencyKeyReuseError,
    RunCursor,
    create_run,
    get_run,
    list_run_session_checkpoints,
    list_runs_page,
    request_cancel,
)
from podium.runs.session_state import AgentSessionView, RunSessionState

router = APIRouter(prefix="/v1", tags=["runs"])

IdempotencyKeyHeader = Annotated[
    str | None,
    Header(
        alias="Idempotency-Key",
        description="Preferred idempotency key for run creation; the request-body alias is deprecated.",
        min_length=1,
        max_length=IDEMPOTENCY_KEY_MAX_LENGTH,
    ),
]

_T = TypeVar("_T")


@runtime_checkable
class _ControlPlaneProvider(Protocol):
    def read_plane(
        self, *, workspace_id: uuid.UUID, company_id: uuid.UUID
    ) -> CompanyControlPlane: ...


def _authorize(
    actor: Actor,
    action: str,
    company_id: uuid.UUID | None = None,
    workspace_id: uuid.UUID | None = None,
) -> None:
    resource = Resource(
        kind="run", workspace_id=workspace_id or actor.workspace_id, company_id=company_id
    )
    if not decide(actor, action, resource):
        raise HTTPException(status_code=403, detail="forbidden")


def _get_log_store(request: Request) -> RunLogStore:
    store: RunLogStore = request.app.state.log_store
    return store


def _decode_cursor(value: str) -> RunCursor:
    """Decode the opaque keyset cursor, rejecting malformed or non-canonical values."""
    try:
        padding = "=" * (-len(value) % 4)
        raw = base64.b64decode(value + padding, altchars=b"-_", validate=True)
        if base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=") != value:
            raise ValueError
        created_at_text, separator, run_id_text = raw.decode("ascii").partition("|")
        if not separator or "|" in run_id_text:
            raise ValueError
        created_at = datetime.fromisoformat(created_at_text)
        if created_at.tzinfo is None:
            raise ValueError
        return RunCursor(created_at=created_at.astimezone(UTC), id=uuid.UUID(run_id_text))
    except (ValueError, UnicodeDecodeError, binascii.Error):
        raise HTTPException(status_code=422, detail="invalid cursor") from None


def _encode_cursor(created_at: datetime, run_id: uuid.UUID) -> str:
    payload = f"{created_at.astimezone(UTC).isoformat()}|{run_id}".encode("ascii")
    return base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")


def _validate_list_query(request: Request) -> None:
    if set(request.query_params) - {"cursor", "limit"}:
        raise HTTPException(status_code=422, detail="unexpected query parameter")


def _canonical_runs_url(
    workspace_id: uuid.UUID, company_id: uuid.UUID, *, cursor: str | None, limit: int
) -> str:
    path = f"/v1/workspaces/{workspace_id}/companies/{company_id}/runs"
    query: list[tuple[str, str]] = [("limit", str(limit))]
    if cursor is not None:
        query.insert(0, ("cursor", cursor))
    return f"{path}?{urlencode(query)}"


async def _read_run(
    company_id: uuid.UUID,
    run_id: uuid.UUID,
    actor: Actor,
    sessionmaker: async_sessionmaker[AsyncSession],
) -> RunOut:
    async with tenant_session(sessionmaker, actor.workspace_id) as session:
        run = await get_run(session, run_id, user_id=actor.user_id)
    if run is None or run.company_id != company_id:
        raise HTTPException(status_code=404, detail="run not found")
    return RunOut.model_validate(run)


@router.post(
    "/companies/{company_id}/runs",
    status_code=202,
    response_model=RunOut,
    response_model_exclude_unset=True,
    responses={
        200: {
            "model": RunOut,
            "headers": {
                "Idempotency-Replayed": {
                    "description": "True when this terminal run is an idempotent replay.",
                    "schema": {"type": "boolean"},
                }
            },
        }
    },
)

def _get_control_provider(request: Request) -> _ControlPlaneProvider:
    provider: object | None = getattr(request.app.state, "control_provider", None)
    if not isinstance(provider, _ControlPlaneProvider):
        raise HTTPException(status_code=503, detail="control plane unavailable")
    return provider


async def _plane_read(
    provider: _ControlPlaneProvider,
    *,
    workspace_id: uuid.UUID,
    company_id: uuid.UUID,
    read: Callable[[CompanyControlPlane], _T],
) -> _T:
    """Open a short-lived Chorus read plane only after Podium visibility is established."""

    def _run() -> _T:
        plane = provider.read_plane(workspace_id=workspace_id, company_id=company_id)
        try:
            return read(plane)
        finally:
            plane.close()

    return await run_in_threadpool(_run)


def _run_not_found() -> NoReturn:
    raise HTTPException(status_code=404, detail="run not found")


def _uuid_or_not_found(value: str) -> uuid.UUID:
    try:
        return uuid.UUID(value)
    except ValueError:
        _run_not_found()


async def create(
    company_id: uuid.UUID,
    body: RunCreate,
    response: Response,
    header_idempotency_key: IdempotencyKeyHeader = None,
    actor: Actor = Depends(enforce_rate_limit),
    sessionmaker: async_sessionmaker[AsyncSession] = Depends(get_sessionmaker),
) -> RunOut:
    return await _create_run(
        workspace_id=None,
        company_id=company_id,
        body=body,
        response=response,
        header_idempotency_key=header_idempotency_key,
        actor=actor,
        sessionmaker=sessionmaker,
    )


@router.post(
    "/workspaces/{workspace_id}/companies/{company_id}/runs",
    status_code=202,
    response_model=RunOut,
    responses={
        200: {
            "model": RunOut,
            "headers": {
                "Idempotency-Replayed": {
                    "description": "True when this terminal run is an idempotent replay.",
                    "schema": {"type": "boolean"},
                }
            },
        }
    },
)
async def create_canonical(
    workspace_id: uuid.UUID,
    company_id: uuid.UUID,
    body: RunCreate,
    response: Response,
    header_idempotency_key: IdempotencyKeyHeader = None,
    actor: Actor = Depends(enforce_rate_limit),
    sessionmaker: async_sessionmaker[AsyncSession] = Depends(get_sessionmaker),
) -> RunOut:
    return await _create_run(
        workspace_id=workspace_id,
        company_id=company_id,
        body=body,
        response=response,
        header_idempotency_key=header_idempotency_key,
        actor=actor,
        sessionmaker=sessionmaker,
    )


async def _create_run(
    *,
    workspace_id: uuid.UUID | None,
    company_id: uuid.UUID,
    body: RunCreate,
    response: Response,
    header_idempotency_key: str | None,
    actor: Actor,
    sessionmaker: async_sessionmaker[AsyncSession],
) -> RunOut:
    if workspace_id is not None and workspace_id != actor.workspace_id:
        raise HTTPException(status_code=404, detail="company not found")
    body_idempotency_key = body.idempotency_key
    if (
        header_idempotency_key is not None
        and body_idempotency_key is not None
        and header_idempotency_key != body_idempotency_key
    ):
        raise HTTPException(status_code=422, detail="Idempotency-Key conflicts with idempotency_key")
    idempotency_key = header_idempotency_key or body_idempotency_key
    if idempotency_key is None:
        raise HTTPException(status_code=422, detail="Idempotency-Key is required")
    _authorize(actor, "create", company_id)
    async with tenant_session(sessionmaker, actor.workspace_id) as session:
        if await get_company(session, company_id, user_id=actor.user_id) is None:
            raise HTTPException(status_code=404, detail="company not found")
        try:
            run, created = await create_run(
                session,
                workspace_id=actor.workspace_id,
                company_id=company_id,
                directive=body.directive,
                idempotency_key=idempotency_key,
                params=body.params().model_dump(exclude_none=True),
            )
        except IdempotencyKeyReuseError as exc:
            raise ProblemHTTPException(
                status_code=422,
                code="idempotency_key_reuse",
                detail="Idempotency-Key was already used for a different run request",
            ) from exc
    if not created:
        if run.status not in TERMINAL_STATUSES:
            raise ProblemHTTPException(
                status_code=409,
                code="idempotency_in_progress",
                detail="The idempotent run request is still in progress",
                headers={"Retry-After": "1"},
            )
        response.status_code = 200
        response.headers["Idempotency-Replayed"] = "true"
    return RunOut.model_validate(run)


@router.get("/companies/{company_id}/runs/{run_id}", response_model=RunOut)
async def get(
    company_id: uuid.UUID,
    run_id: uuid.UUID,
    actor: Actor = Depends(enforce_rate_limit),
    sessionmaker: async_sessionmaker[AsyncSession] = Depends(get_sessionmaker),
) -> RunOut:
    _authorize(actor, "read", company_id)
    return await _read_run(company_id, run_id, actor, sessionmaker)


@router.get(
    "/workspaces/{workspace_id}/companies/{company_id}/runs/{run_id}",
    response_model=RunOut,
    response_model_exclude_unset=True,
)
async def get_canonical(
    workspace_id: uuid.UUID,
    company_id: uuid.UUID,
    run_id: uuid.UUID,
    actor: Actor = Depends(enforce_rate_limit),
    sessionmaker: async_sessionmaker[AsyncSession] = Depends(get_sessionmaker),
) -> RunOut:
    _authorize(actor, "read", company_id, workspace_id)
    return await _read_run(company_id, run_id, actor, sessionmaker)


@router.get(
    "/workspaces/{workspace_id}/companies/{company_id}/runs",
    response_model=RunPage,
    response_model_exclude_unset=True,
)
async def list_canonical(
    workspace_id: uuid.UUID,
    company_id: uuid.UUID,
    request: Request,
    cursor: str | None = Query(None, max_length=512),
    limit: int = Query(50, ge=1, le=200),
    actor: Actor = Depends(enforce_rate_limit),
    sessionmaker: async_sessionmaker[AsyncSession] = Depends(get_sessionmaker),
) -> RunPage:
    _authorize(actor, "read", company_id, workspace_id)
    _validate_list_query(request)
    async with tenant_session(sessionmaker, actor.workspace_id) as session:
        if await get_company(session, company_id, user_id=actor.user_id) is None:
            raise HTTPException(status_code=404, detail="company not found")
        rows = await list_runs_page(
            session,
            company_id,
            cursor=_decode_cursor(cursor) if cursor is not None else None,
            limit=limit + 1,
        )
    has_more = len(rows) > limit
    runs = rows[:limit]
    next_cursor = _encode_cursor(runs[-1].created_at, runs[-1].id) if has_more else None
    return RunPage(
        data=[RunOut.model_validate(run) for run in runs],
        meta=RunPageMeta(next_cursor=next_cursor, has_more=has_more),
        links=RunPageLinks(
            self=_canonical_runs_url(workspace_id, company_id, cursor=cursor, limit=limit),
            next=(
                _canonical_runs_url(
                    workspace_id, company_id, cursor=next_cursor, limit=limit
                )
                if next_cursor is not None
                else None
            ),
        ),
    )


@router.get(
    "/companies/{company_id}/runs/{run_id}/session-checkpoints",
    response_model=tuple[RunSessionCheckpointOut, ...],
)
async def list_session_checkpoints(
    company_id: str,
    run_id: str,
    actor: Actor = Depends(enforce_rate_limit),
    sessionmaker: async_sessionmaker[AsyncSession] = Depends(get_sessionmaker),
) -> tuple[RunSessionCheckpointOut, ...]:
    """List a visible run's immutable Dream checkpoints in global append order."""
    parsed_company_id = _uuid_or_not_found(company_id)
    parsed_run_id = _uuid_or_not_found(run_id)
    _authorize(actor, "read")
    if actor.company_id is not None and actor.company_id != parsed_company_id:
        _run_not_found()
    async with tenant_session(sessionmaker, actor.workspace_id) as session:
        if await get_company(session, parsed_company_id, user_id=actor.user_id) is None:
            _run_not_found()
        run = await get_run(session, parsed_run_id)
        if run is None or run.company_id != parsed_company_id:
            _run_not_found()
        checkpoints = await list_run_session_checkpoints(session, run_id=parsed_run_id)
    return tuple(RunSessionCheckpointOut.model_validate(checkpoint) for checkpoint in checkpoints)


@router.get(
    "/companies/{company_id}/runs/{run_id}/session-state",
    response_model=RunSessionState,
)
async def get_session_state(
    company_id: str,
    run_id: str,
    actor: Actor = Depends(enforce_rate_limit),
    sessionmaker: async_sessionmaker[AsyncSession] = Depends(get_sessionmaker),
    provider: _ControlPlaneProvider = Depends(_get_control_provider),
) -> RunSessionState:
    """Return a visible run's latest authoritative Chorus agent-session state."""
    parsed_company_id = _uuid_or_not_found(company_id)
    parsed_run_id = _uuid_or_not_found(run_id)
    _authorize(actor, "read")
    if actor.company_id is not None and actor.company_id != parsed_company_id:
        _run_not_found()
    async with tenant_session(sessionmaker, actor.workspace_id) as session:
        company = await get_company(session, parsed_company_id, user_id=actor.user_id)
        run = await get_run(session, parsed_run_id)
        if company is None or run is None or run.company_id != parsed_company_id:
            _run_not_found()
        engine_task_id = run.engine_task_id

    session_view: AgentSessionView | None = None
    if engine_task_id is not None:
        session_view = await _plane_read(
            provider,
            workspace_id=actor.workspace_id,
            company_id=parsed_company_id,
            read=lambda plane: plane.session_state.latest_for_task(engine_task_id),
        )
    return RunSessionState(
        run_id=parsed_run_id,
        engine_task_id=engine_task_id,
        session=session_view,
    )


@router.post("/runs/{run_id}/cancel", response_model=RunOut)
async def cancel(
    run_id: uuid.UUID,
    actor: Actor = Depends(enforce_rate_limit),
    sessionmaker: async_sessionmaker[AsyncSession] = Depends(get_sessionmaker),
) -> RunOut:
    _authorize(actor, "cancel")
    async with tenant_session(sessionmaker, actor.workspace_id) as session:
        if await get_run(session, run_id) is None:
            raise HTTPException(status_code=404, detail="run not found")
        await request_cancel(session, run_id)
        run = await get_run(session, run_id)
        assert run is not None
        return RunOut.model_validate(run)


@router.get("/runs/{run_id}/logs")
async def logs(
    run_id: uuid.UUID,
    actor: Actor = Depends(enforce_rate_limit),
    sessionmaker: async_sessionmaker[AsyncSession] = Depends(get_sessionmaker),
    store: RunLogStore = Depends(_get_log_store),
) -> Response:
    """Stream a run's durable transcript from the log store. 404 if the run has produced none."""
    _authorize(actor, "read")
    async with tenant_session(sessionmaker, actor.workspace_id) as session:
        run = await get_run(session, run_id)  # RLS hides a foreign run → None → 404
    if run is None or run.log_ref is None or not store.exists(run_id):
        raise HTTPException(status_code=404, detail="no logs for this run")
    data, sha256 = store.load(run_id)  # single read; Starlette sets Content-Length from the bytes
    return Response(
        content=data, media_type="text/plain; charset=utf-8", headers={"X-Log-Sha256": sha256}
    )
