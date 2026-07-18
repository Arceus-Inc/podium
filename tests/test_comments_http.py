"""OM-3 — comments as coordination, surfaced: the task-thread doors.

Task-anchored engine messages ARE the comment thread. GET reads it; POST lets the human on the
board join it — the comment is delivered through the engine's own path, so the recipient's next
beat sees it (paperclip: the board participates in the same channel as the agents)."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

import httpx
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import podium.db.metadata  # noqa: F401  -- register every model so FK targets resolve
from podium.auth import create_api_key
from podium.companies import create_company
from podium.control import ControlPlaneProvider
from podium.main import create_app
from podium.workspaces import create_workspace


@pytest_asyncio.fixture
async def api(
    database_url: str,
    app_sessionmaker: async_sessionmaker[AsyncSession],
) -> AsyncIterator[httpx.AsyncClient]:
    app = create_app()
    app.state.sessionmaker = app_sessionmaker
    app.state.control_provider = ControlPlaneProvider(
        engine_dsn=database_url.replace("+asyncpg", "").replace("://postgres@", "://podium_app@")
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
        yield client


def _seed_task(dsn: str, company_id: str) -> str:
    from chorus.ledger import Ledger, Message, Task
    from chorus.workforce import Employee

    ledger = Ledger.open(dsn, company_id=company_id)
    try:
        ledger.employees.create(Employee(id="rex", name="Rex", role="engineer"))
        ledger.employees.create(Employee(id="mia", name="Mia", role="manager"))
        task = ledger.tasks.submit(
            Task(id=str(uuid.uuid4()), intent="write the parser", assignee_employee_id="rex")
        )
        ledger.messages.send(
            Message(
                id=str(uuid.uuid4()),
                from_employee_id="mia",
                to_employee_id="rex",
                task_id=task.id,
                body="handle CRLF",
            )
        )
        return task.id
    finally:
        ledger.close()


async def _mint(sessionmaker: async_sessionmaker[AsyncSession], slug: str) -> tuple[str, str, str]:
    async with sessionmaker() as s, s.begin():
        ws = await create_workspace(s, name=slug.upper(), slug=slug)
        company = await create_company(s, workspace_id=ws.id, slug=slug, name=slug.upper())
        _, token = await create_api_key(s, workspace_id=ws.id, name="k")
        return str(ws.id), str(company.id), token


async def test_thread_reads_and_the_human_can_join_it(
    database_url: str,
    sessionmaker: async_sessionmaker[AsyncSession],
    api: httpx.AsyncClient,
) -> None:
    ws_id, company_id, token = await _mint(sessionmaker, "cmt")
    dsn = database_url.replace("+asyncpg", "").replace("://postgres@", "://podium_app@")
    task_id = _seed_task(dsn, company_id)
    base = f"/v1/workspaces/{ws_id}/companies/{company_id}"
    headers = {"Authorization": f"Bearer {token}"}

    listed = await api.get(f"{base}/tasks/{task_id}/comments", headers=headers)
    assert listed.status_code == 200
    thread = listed.json()
    assert [c["body"] for c in thread] == ["handle CRLF"]
    assert thread[0]["author"] == "mia"

    posted = await api.post(
        f"{base}/tasks/{task_id}/comments", headers=headers, json={"body": "ship by friday"}
    )
    assert posted.status_code == 201
    comment = posted.json()
    assert comment["body"] == "ship by friday"
    assert comment["author"]  # the authenticated human, never client-supplied

    thread = (await api.get(f"{base}/tasks/{task_id}/comments", headers=headers)).json()
    assert [c["body"] for c in thread] == ["handle CRLF", "ship by friday"]

    # Delivered through the engine: the assignee got a wake, so their next beat reads it.
    from chorus.ledger import Ledger

    ledger = Ledger.open(dsn, company_id=company_id)
    try:
        assert any(w.employee_id == "rex" for w in ledger.wakes.queued())
    finally:
        ledger.close()


async def test_comment_doors_are_fail_closed(
    database_url: str,
    sessionmaker: async_sessionmaker[AsyncSession],
    api: httpx.AsyncClient,
) -> None:
    ws_id, company_id, token = await _mint(sessionmaker, "cmt2")
    base = f"/v1/workspaces/{ws_id}/companies/{company_id}"
    headers = {"Authorization": f"Bearer {token}"}

    assert (await api.get(f"{base}/tasks/nope/comments")).status_code == 401
    assert (await api.get(f"{base}/tasks/nope/comments", headers=headers)).status_code == 404
    missing = await api.post(
        f"{base}/tasks/{uuid.uuid4()}/comments", headers=headers, json={"body": "x"}
    )
    assert missing.status_code == 404
