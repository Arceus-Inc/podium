"""HTTP read doors for the durable timeline projection."""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import uuid
from datetime import UTC, datetime, timedelta
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from podium.auth import Actor, Resource, decide, enforce_rate_limit, get_sessionmaker
from podium.companies.service import get_company
from podium.db import tenant_session
from podium.timeline.models import TimelineItem
from podium.timeline.repository import TimelineCursor, get_item, page_items
from podium.timeline.schemas import (
    TimelineDetailEnvelope,
    TimelineItemView,
    TimelineLinks,
    TimelinePageEnvelope,
    TimelinePageMeta,
)

router = APIRouter(prefix="/v1/workspaces/{workspace_id}/companies/{company_id}", tags=["timeline"])

_CATEGORIES = frozenset(
    {"direction", "work", "deliverable", "people", "learning", "cost", "system"}
)
_CURSOR_MAX_LENGTH = 512


async def _visible_company_or_404(
    sessionmaker: async_sessionmaker[AsyncSession],
    actor: Actor,
    workspace_id: uuid.UUID,
    company_id: uuid.UUID,
) -> None:
    resource = Resource(kind="company", workspace_id=workspace_id, company_id=company_id)
    if not decide(actor, "read", resource):
        raise HTTPException(status_code=403, detail="forbidden")
    async with tenant_session(sessionmaker, actor.workspace_id) as session:
        company = await get_company(session, company_id, user_id=actor.user_id)
    if company is None:
        raise HTTPException(status_code=404, detail="company not found")


def _parse_utc(value: str, *, field: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=f"{field} must be an ISO-8601 UTC timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
        raise HTTPException(status_code=422, detail=f"{field} must be a UTC timestamp")
    return parsed.astimezone(UTC)


def _encode_cursor(item: TimelineItem) -> str:
    return base64.urlsafe_b64encode(str(item.source_event_seq).encode()).decode().rstrip("=")


def _decode_cursor(value: str) -> TimelineCursor:
    try:
        padding = "=" * (-len(value) % 4)
        decoded = base64.b64decode(value + padding, altchars=b"-_", validate=True).decode()
        source_event_seq = int(decoded)
        if source_event_seq < 0:
            raise ValueError("negative sequence")
        if _encode_cursor_for_seq(source_event_seq) != value:
            raise ValueError("noncanonical cursor")
        return TimelineCursor(source_event_seq=source_event_seq)
    except (ValueError, UnicodeDecodeError, binascii.Error) as exc:
        raise HTTPException(status_code=422, detail="cursor is invalid") from exc


def _encode_cursor_for_seq(source_event_seq: int) -> str:
    return base64.urlsafe_b64encode(str(source_event_seq).encode()).decode().rstrip("=")


def _list_link(
    *,
    workspace_id: uuid.UUID,
    company_id: uuid.UUID,
    limit: int,
    cursor: str | None,
    category: str | None,
    attention: bool | None,
    actor_type: str | None,
    actor_id: str | None,
    subject_type: str | None,
    subject_id: str | None,
    occurred_after: str | None,
    occurred_before: str | None,
) -> str:
    parameters: list[tuple[str, str]] = [("limit", str(limit))]
    optional_parameters = (
        ("cursor", cursor),
        ("category", category),
        ("attention", str(attention).lower() if attention is not None else None),
        ("actor_type", actor_type),
        ("actor_id", actor_id),
        ("subject_type", subject_type),
        ("subject_id", subject_id),
        ("occurred_after", occurred_after),
        ("occurred_before", occurred_before),
    )
    parameters.extend((name, value) for name, value in optional_parameters if value is not None)
    path = f"/v1/workspaces/{workspace_id}/companies/{company_id}/timeline"
    return f"{path}?{urlencode(parameters)}"


def _etag(view: TimelineItemView) -> str:
    payload = json.dumps(view.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
    return f'"{hashlib.sha256(payload.encode()).hexdigest()}"'


def _if_none_match_matches(value: str | None, etag: str) -> bool:
    if value is None:
        return False
    return value.strip() == "*" or any(
        validator.strip().removeprefix("W/") == etag for validator in value.split(",")
    )


@router.get("/timeline", response_model=TimelinePageEnvelope)
async def list_timeline(
    workspace_id: uuid.UUID,
    company_id: uuid.UUID,
    limit: int = Query(default=50, ge=1, le=200),
    cursor: str | None = Query(default=None, max_length=_CURSOR_MAX_LENGTH),
    category: str | None = Query(default=None),
    attention: bool | None = Query(default=None),
    actor_type: str | None = Query(default=None),
    actor_id: str | None = Query(default=None),
    subject_type: str | None = Query(default=None),
    subject_id: str | None = Query(default=None),
    occurred_after: str | None = Query(default=None),
    occurred_before: str | None = Query(default=None),
    actor: Actor = Depends(enforce_rate_limit),
    sessionmaker: async_sessionmaker[AsyncSession] = Depends(get_sessionmaker),
) -> TimelinePageEnvelope:
    if category is not None and category not in _CATEGORIES:
        raise HTTPException(status_code=422, detail="category is invalid")
    after = _parse_utc(occurred_after, field="occurred_after") if occurred_after is not None else None
    before = _parse_utc(occurred_before, field="occurred_before") if occurred_before is not None else None
    if after is not None and before is not None and after > before:
        raise HTTPException(status_code=422, detail="occurred_after must not be after occurred_before")
    position = _decode_cursor(cursor) if cursor is not None else None
    await _visible_company_or_404(sessionmaker, actor, workspace_id, company_id)
    async with tenant_session(sessionmaker, actor.workspace_id) as session:
        page = await page_items(
            session,
            company_id=company_id,
            limit=limit,
            cursor=position,
            category=category,
            attention=attention,
            actor_type=actor_type,
            actor_id=actor_id,
            subject_type=subject_type,
            subject_id=subject_id,
            occurred_after=after,
            occurred_before=before,
        )
    views = tuple(TimelineItemView.model_validate(item) for item in page.items)
    next_cursor = _encode_cursor(page.items[-1]) if page.has_more else None
    self_link = _list_link(
        workspace_id=workspace_id,
        company_id=company_id,
        limit=limit,
        cursor=cursor,
        category=category,
        attention=attention,
        actor_type=actor_type,
        actor_id=actor_id,
        subject_type=subject_type,
        subject_id=subject_id,
        occurred_after=occurred_after,
        occurred_before=occurred_before,
    )
    next_link = (
        _list_link(
            workspace_id=workspace_id,
            company_id=company_id,
            limit=limit,
            cursor=next_cursor,
            category=category,
            attention=attention,
            actor_type=actor_type,
            actor_id=actor_id,
            subject_type=subject_type,
            subject_id=subject_id,
            occurred_after=occurred_after,
            occurred_before=occurred_before,
        )
        if next_cursor is not None
        else None
    )
    return TimelinePageEnvelope(
        data=views,
        meta=TimelinePageMeta(
            has_more=page.has_more,
            next_cursor=next_cursor,
            as_of_seq=page.as_of_seq,
        ),
        links=TimelineLinks(self=self_link, next=next_link),
    )


@router.get("/timeline/{item_id}", response_model=TimelineDetailEnvelope)
async def get_timeline_item(
    workspace_id: uuid.UUID,
    company_id: uuid.UUID,
    item_id: uuid.UUID,
    request: Request,
    response: Response,
    actor: Actor = Depends(enforce_rate_limit),
    sessionmaker: async_sessionmaker[AsyncSession] = Depends(get_sessionmaker),
) -> TimelineDetailEnvelope | Response:
    await _visible_company_or_404(sessionmaker, actor, workspace_id, company_id)
    async with tenant_session(sessionmaker, actor.workspace_id) as session:
        item = await get_item(session, company_id=company_id, item_id=item_id)
    if item is None:
        raise HTTPException(status_code=404, detail="timeline item not found")
    view = TimelineItemView.model_validate(item)
    etag = _etag(view)
    if _if_none_match_matches(request.headers.get("if-none-match"), etag):
        return Response(status_code=304, headers={"ETag": etag})
    response.headers["ETag"] = etag
    detail_link = f"/v1/workspaces/{workspace_id}/companies/{company_id}/timeline/{item_id}"
    return TimelineDetailEnvelope(data=view, links=TimelineLinks(self=detail_link, next=None))
