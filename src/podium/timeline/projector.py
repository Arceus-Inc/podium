"""Durable, ordered runtime projection of Chorus events into timeline rows."""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta

import structlog
from chorus.events import Event as ChorusEvent
from chorus.events import EventKind
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from podium.db import tenant_session
from podium.events import Event, list_company_events, max_company_seq
from podium.timeline import repository
from podium.timeline.mapping import TimelinePayloadError, map_timeline_event
from podium.timeline.service import (
    OccurredAtMustBeUTC,
    ProjectionCursorMismatch,
    ProjectionVersionMismatch,
    SourceEventNotFound,
    project_timeline_event,
)
from podium.timeline.service_types import TimelineExclusion, TimelineItemDraft

TIMELINE_PROJECTOR = "timeline"
TIMELINE_PROJECTOR_VERSION = "1"
_BATCH_SIZE = 200
_log = structlog.get_logger("podium.timeline.projector")


class UnknownTimelineEventKind(ValueError):
    """A durable event does not belong to the closed Chorus event vocabulary."""


class DurableTimelinePayloadError(ValueError):
    """A durable event payload cannot be represented by the Chorus event envelope."""


@dataclass(frozen=True, slots=True)
class TimelineProjectionLag:
    """Observable catch-up position for one company's timeline projection."""

    last_projected_seq: int
    latest_event_seq: int
    halted_event_seq: int | None = None
    failure_type: str | None = None
    failure_message: str | None = None

    @property
    def lag(self) -> int:
        return self.latest_event_seq - self.last_projected_seq

    @property
    def is_healthy(self) -> bool:
        return self.failure_type is None


@dataclass(frozen=True, slots=True)
class _EventContext:
    source_event_seq: int
    event_type: str


@dataclass(frozen=True, slots=True)
class _ProjectionAttempt:
    lag: TimelineProjectionLag
    event: _EventContext | None
    error: Exception | None


class _ProjectionStepFailure(Exception):
    def __init__(self, event: _EventContext, error: Exception) -> None:
        self.event = event
        self.error = error
        super().__init__(str(error))


class _RebuildFailure(Exception):
    def __init__(self, attempt: _ProjectionAttempt) -> None:
        error = attempt.error
        assert error is not None
        self.event = attempt.event
        self.error = error
        super().__init__(str(error))


def chorus_event_from_durable(event: Event) -> ChorusEvent:
    """Cross the JSONB-to-Chorus boundary once, rejecting malformed durable envelopes."""
    try:
        kind = EventKind(event.type)
    except ValueError as exc:
        raise UnknownTimelineEventKind(f"unknown durable event kind: {event.type}") from exc
    return ChorusEvent(
        kind=kind,
        at=_utc_datetime(event.created_at),
        trace_id=str(event.trace_id) if event.trace_id is not None else None,
        task_id=event.task_id,
        employee_id=event.employee_id,
        run_id=str(event.run_id) if event.run_id is not None else None,
        payload=_json_object(event.payload),
    )


def _utc_datetime(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise DurableTimelinePayloadError("durable event timestamp must be timezone-aware UTC")
    return value


def _json_object(value: object) -> Mapping[str, object]:
    if not isinstance(value, dict):
        raise DurableTimelinePayloadError("durable event payload must be a JSON object")
    payload: dict[str, object] = {}
    for key, child in value.items():
        if type(key) is not str:
            raise DurableTimelinePayloadError("durable event payload keys must be strings")
        payload[key] = _json_value(child)
    return payload


def _json_value(value: object) -> object:
    if value is None or type(value) in {bool, int, float, str}:
        return value
    if isinstance(value, list):
        return [_json_value(item) for item in value]
    if isinstance(value, dict):
        return _json_object(value)
    raise DurableTimelinePayloadError("durable event payload must contain JSON values")


class TimelineProjector:
    """Catch up a company's timeline in source-sequence order without skipping failures."""

    def __init__(
        self,
        sessionmaker: async_sessionmaker[AsyncSession],
        *,
        company_id: uuid.UUID,
        workspace_id: uuid.UUID,
        projector: str = TIMELINE_PROJECTOR,
        projector_version: str = TIMELINE_PROJECTOR_VERSION,
    ) -> None:
        self._sm = sessionmaker
        self._company_id = company_id
        self._workspace_id = workspace_id
        self._projector = projector
        self._projector_version = projector_version
        self._lag = TimelineProjectionLag(last_projected_seq=0, latest_event_seq=0)

    @property
    def lag(self) -> TimelineProjectionLag:
        return self._lag

    async def catch_up(self) -> TimelineProjectionLag:
        """Project every currently durable event until the first invalid or conflicting row."""
        try:
            async with tenant_session(self._sm, self._workspace_id) as session:
                attempt = await self._project_in_session(session)
        except _ProjectionStepFailure as failure:
            return await self._refresh_durable_failure(failure.error, event=failure.event)
        except Exception as error:
            return await self._refresh_durable_failure(error, event=None)
        return self._record_attempt(attempt)

    async def rebuild(self) -> TimelineProjectionLag:
        """Drop this materialized timeline and deterministically replay the durable source log."""
        try:
            async with tenant_session(self._sm, self._workspace_id) as session:
                await repository.clear_projection(
                    session,
                    company_id=self._company_id,
                    workspace_id=self._workspace_id,
                    projector=self._projector,
                )
                attempt = await self._project_in_session(session)
                if not attempt.lag.is_healthy:
                    raise _RebuildFailure(attempt)
        except _RebuildFailure as failure:
            return await self._refresh_durable_failure(failure.error, event=failure.event)
        except _ProjectionStepFailure as failure:
            return await self._refresh_durable_failure(failure.error, event=failure.event)
        except Exception as error:
            return await self._refresh_durable_failure(error, event=None)
        return self._record_attempt(attempt)

    async def _project_in_session(self, session: AsyncSession) -> _ProjectionAttempt:
        cursor = await repository.get_cursor(
            session, company_id=self._company_id, projector=self._projector
        )
        last_projected_seq = cursor.last_event_seq if cursor is not None else 0
        latest_event_seq = await max_company_seq(session, self._company_id)
        if cursor is not None and cursor.projector_version != self._projector_version:
            return self._failed_attempt(
                last_projected_seq=last_projected_seq,
                latest_event_seq=latest_event_seq,
                event=None,
                error=ProjectionVersionMismatch(
                    "projector version differs from the durable cursor"
                ),
            )
        while last_projected_seq < latest_event_seq:
            events = await list_company_events(
                session,
                self._company_id,
                after=last_projected_seq,
                limit=_BATCH_SIZE,
            )
            if not events:
                return self._healthy_attempt(last_projected_seq, latest_event_seq)
            for event in events:
                try:
                    chorus_event = chorus_event_from_durable(event)
                    mapped = map_timeline_event(chorus_event, source_event_seq=event.seq)
                    item, exclusion = _projection_input(mapped)
                    await project_timeline_event(
                        session,
                        company_id=self._company_id,
                        workspace_id=self._workspace_id,
                        projector=self._projector,
                        projector_version=self._projector_version,
                        expected_prior_seq=last_projected_seq,
                        item=item,
                        exclusion=exclusion,
                    )
                except (
                    DurableTimelinePayloadError,
                    OccurredAtMustBeUTC,
                    ProjectionCursorMismatch,
                    ProjectionVersionMismatch,
                    SourceEventNotFound,
                    TimelinePayloadError,
                    UnknownTimelineEventKind,
                ) as error:
                    return self._failed_attempt(
                        last_projected_seq=last_projected_seq,
                        latest_event_seq=latest_event_seq,
                        event=_event_context(event),
                        error=error,
                    )
                except Exception as error:
                    raise _ProjectionStepFailure(_event_context(event), error) from error
                last_projected_seq = event.seq
            latest_event_seq = await max_company_seq(session, self._company_id)
        return self._healthy_attempt(last_projected_seq, latest_event_seq)

    def _healthy_attempt(
        self, last_projected_seq: int, latest_event_seq: int
    ) -> _ProjectionAttempt:
        return _ProjectionAttempt(
            lag=TimelineProjectionLag(
                last_projected_seq=last_projected_seq,
                latest_event_seq=latest_event_seq,
            ),
            event=None,
            error=None,
        )

    def _failed_attempt(
        self,
        *,
        last_projected_seq: int,
        latest_event_seq: int,
        event: _EventContext | None,
        error: Exception,
    ) -> _ProjectionAttempt:
        return _ProjectionAttempt(
            lag=TimelineProjectionLag(
                last_projected_seq=last_projected_seq,
                latest_event_seq=latest_event_seq,
                halted_event_seq=event.source_event_seq if event is not None else None,
                failure_type=type(error).__name__,
                failure_message=str(error),
            ),
            event=event,
            error=error,
        )

    def _record_attempt(self, attempt: _ProjectionAttempt) -> TimelineProjectionLag:
        self._lag = attempt.lag
        if attempt.error is not None:
            self._log_failure(attempt.lag, event=attempt.event)
        return self._lag

    async def _refresh_durable_failure(
        self, error: Exception, *, event: _EventContext | None
    ) -> TimelineProjectionLag:
        try:
            async with tenant_session(self._sm, self._workspace_id) as session:
                cursor = await repository.get_cursor(
                    session, company_id=self._company_id, projector=self._projector
                )
                last_projected_seq = cursor.last_event_seq if cursor is not None else 0
                latest_event_seq = await max_company_seq(session, self._company_id)
        except Exception as refresh_error:
            last_projected_seq = self._lag.last_projected_seq
            latest_event_seq = max(self._lag.latest_event_seq, last_projected_seq)
            error = RuntimeError(
                f"{type(error).__name__}: {error}; durable cursor refresh failed: "
                f"{type(refresh_error).__name__}: {refresh_error}"
            )
        self._lag = TimelineProjectionLag(
            last_projected_seq=last_projected_seq,
            latest_event_seq=latest_event_seq,
            halted_event_seq=event.source_event_seq if event is not None else None,
            failure_type=type(error).__name__,
            failure_message=str(error),
        )
        self._log_failure(self._lag, event=event)
        return self._lag

    def _log_failure(self, lag: TimelineProjectionLag, *, event: _EventContext | None) -> None:
        _log.error(
            "timeline_projection_halted",
            company_id=str(self._company_id),
            workspace_id=str(self._workspace_id),
            source_event_seq=event.source_event_seq if event is not None else None,
            event_type=event.event_type if event is not None else None,
            failure_type=lag.failure_type,
            failure_message=lag.failure_message,
        )


def _projection_input(
    mapped: TimelineItemDraft | TimelineExclusion,
) -> tuple[TimelineItemDraft | None, TimelineExclusion | None]:
    if isinstance(mapped, TimelineItemDraft):
        return mapped, None
    return None, mapped


def _event_context(event: Event) -> _EventContext:
    return _EventContext(source_event_seq=event.seq, event_type=event.type)


__all__ = [
    "TIMELINE_PROJECTOR",
    "TIMELINE_PROJECTOR_VERSION",
    "DurableTimelinePayloadError",
    "TimelineProjectionLag",
    "TimelineProjector",
    "UnknownTimelineEventKind",
    "chorus_event_from_durable",
]
