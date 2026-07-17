"""M5.1 exit proof: every podium entity id is a native Postgres uuid, DB-minted via uuidv7().

Native in BOTH layers: `uuid` columns in Postgres (information_schema agrees) and `uuid.UUID`
objects in Python — no prefixed strings, no text ids. Chorus-boundary ids (`engine_task_id`,
`events.employee_id`) stay text until the M5.2 engine port; everything podium mints is uuid.
"""

from __future__ import annotations

import uuid

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from podium.auth import create_api_key
from podium.companies import create_company
from podium.conductor import enqueue_command
from podium.db import tenant_session
from podium.runs import create_run
from podium.users import create_user
from podium.workspaces import create_workspace

# Every (table, column) that must be a native uuid in Postgres. Chorus-boundary text columns
# (runs.engine_task_id, events.employee_id) are deliberately absent.
_UUID_COLUMNS = {
    ("workspaces", "id"),
    ("companies", "id"),
    ("companies", "workspace_id"),
    ("users", "id"),
    ("users", "workspace_id"),
    ("api_keys", "id"),
    ("api_keys", "workspace_id"),
    ("api_keys", "company_id"),
    ("api_keys", "user_id"),
    ("runs", "id"),
    ("runs", "workspace_id"),
    ("runs", "company_id"),
    ("commands", "id"),
    ("commands", "workspace_id"),
    ("commands", "company_id"),
    ("commands", "run_id"),
    ("events", "company_id"),
    ("events", "workspace_id"),
    ("events", "run_id"),
}


async def test_every_entity_id_is_a_db_minted_uuid7(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    async with sessionmaker() as s, s.begin():
        ws = await create_workspace(s, name="Acme", slug="acme")
        company = await create_company(s, workspace_id=ws.id, slug="c", name="C")
        user = await create_user(s, workspace_id=ws.id, email="a@acme.io", name="A")
        key, _token = await create_api_key(s, workspace_id=ws.id, name="k")
        ws_id, company_id = ws.id, company.id
        minted = [ws.id, company.id, user.id, key.id]
    async with tenant_session(sessionmaker, ws_id) as s:
        run, _ = await create_run(
            s, workspace_id=ws_id, company_id=company_id, directive="d", idempotency_key="k"
        )
        command = await enqueue_command(s, workspace_id=ws_id, company_id=company_id, type="cancel")
        minted += [run.id, command.id]
    for value in minted:
        assert isinstance(value, uuid.UUID), f"expected uuid.UUID, got {type(value)}: {value!r}"
        assert value.version == 7  # DB-minted uuidv7 — time-ordered, never random-v4


async def test_id_columns_are_native_uuid_in_postgres(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    stmt = text(
        "SELECT table_name, column_name, data_type FROM information_schema.columns "
        "WHERE table_schema = 'public'"
    )
    async with sessionmaker() as s:
        rows = (await s.execute(stmt)).all()
    found = {
        (r.table_name, r.column_name): r.data_type
        for r in rows
        if (r.table_name, r.column_name) in _UUID_COLUMNS
    }
    assert set(found) == _UUID_COLUMNS  # every expected column exists
    wrong = {key: dtype for key, dtype in found.items() if dtype != "uuid"}
    assert wrong == {}, f"non-uuid id columns: {wrong}"


async def test_rls_still_bites_with_uuid_guc(
    sessionmaker: async_sessionmaker[AsyncSession],
    app_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """The workspace wall holds across the uuid port: the policy casts the GUC to uuid."""
    async with sessionmaker() as s, s.begin():
        a = await create_workspace(s, name="Alpha", slug="alpha")
        b = await create_workspace(s, name="Beta", slug="beta")
        a_id, b_id = a.id, b.id
    async with tenant_session(app_sessionmaker, a_id) as s:
        await create_company(s, workspace_id=a_id, slug="c", name="A Co")
    async with tenant_session(app_sessionmaker, b_id) as s:
        rows = (await s.execute(text("SELECT id FROM companies"))).all()
    assert rows == []  # B's uuid-scoped session sees none of A's rows


async def test_rls_fails_closed_on_reset_empty_guc(
    sessionmaker: async_sessionmaker[AsyncSession],
    app_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """The Postgres gotcha the NULLIF cast exists for: after a transaction-local GUC ends, the same
    connection reports '' (empty string), not NULL — and ''::uuid would ERROR. The policy must
    treat '' exactly like unset: zero rows, no exception."""
    async with sessionmaker() as s, s.begin():
        ws = await create_workspace(s, name="Gamma", slug="gamma")
        ws_id = ws.id
    async with tenant_session(app_sessionmaker, ws_id) as s:
        await create_company(s, workspace_id=ws_id, slug="c", name="G Co")
    async with app_sessionmaker() as s:
        # Force the reset-to-'' state explicitly on this very connection, then query.
        await s.execute(text("SELECT set_config('app.workspace_id', '', false)"))
        rows = (await s.execute(text("SELECT id FROM companies"))).all()
    assert rows == []  # '' fails closed — no rows, no InvalidTextRepresentation error
