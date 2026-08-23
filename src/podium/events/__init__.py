"""Events domain package — the append-only per-company event log and its cursor-paging API."""

from __future__ import annotations

from podium.events._broadcaster import EVENTS_CHANNEL, Broadcaster, Subscription, Wake
from podium.events.models import Event, EventChannel, EventPayload
from podium.events.service import (
    allocate_company_seq,
    append_event,
    append_product_event,
    list_company_events,
    list_product_events,
    list_run_events,
    max_company_seq,
)

__all__ = [
    "EVENTS_CHANNEL",
    "Broadcaster",
    "Event",
    "EventChannel",
    "EventPayload",
    "Subscription",
    "Wake",
    "allocate_company_seq",
    "append_event",
    "append_product_event",
    "list_company_events",
    "list_product_events",
    "list_run_events",
    "max_company_seq",
]
