"""M5 engine state store: the chorus ledger lives in podium's Postgres, per-company by FORCE RLS.

Migration 0008 creates the engine tables (chorus's own Postgres-native DDL — uuid/timestamptz/
jsonb/boolean) in the shared database and grants the runtime role exactly those tables. A company
graph built with `ledger_dsn` runs chorus against them, scoped to its company; the SQLite path
stays for `ledger_backend="sqlite"` companies until the default flips.
"""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import psycopg
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from company import CompanyConfig, build

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")

_FAKE_MODEL = {"api_key": "test-key", "base_url": "http://localhost:9", "deployment": "none"}


def _pg_conninfo(database_url: str, *, user: str) -> str:
    """The sync psycopg DSN for the ephemeral cluster, as the given role."""
    return database_url.replace("+asyncpg", "").replace("://postgres@", f"://{user}@")


async def test_migration_creates_engine_tables_with_grants_and_rls(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    async with sessionmaker() as s:
        tables = {
            row.tablename: (row.rowsecurity, row.forcerowsecurity)
            for row in (
                await s.execute(
                    text(
                        "SELECT c.relname AS tablename, c.relrowsecurity AS rowsecurity, "
                        "c.relforcerowsecurity AS forcerowsecurity "
                        "FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
                        "WHERE n.nspname = 'public' AND c.relkind = 'r'"
                    )
                )
            ).all()
        }
        # The engine tables exist alongside the product tables, RLS enabled AND forced.
        for engine_table in ("task", "wake", "employee", "run", "goal"):
            assert engine_table in tables, f"engine table {engine_table} missing"
            assert tables[engine_table] == (True, True), f"{engine_table} lacks FORCE RLS"
        # The runtime role is granted the engine tables (never a blanket schema grant).
        granted = (
            await s.execute(
                text("SELECT has_table_privilege('podium_app', 'task', 'SELECT, INSERT')")
            )
        ).scalar_one()
        assert granted is True


def test_company_graph_runs_on_the_shared_postgres_ledger(
    database_url: str, tmp_path: Path
) -> None:
    company_id = str(uuid4())
    graph = build(
        CompanyConfig(
            **_FAKE_MODEL,
            workdir=tmp_path / "co",
            company_id=company_id,
            ledger_dsn=_pg_conninfo(database_url, user="podium_app"),
        )
    )
    worker = graph.org.hire(name="Ace", role="backend_engineer")
    task = graph.org.submit("write the launch plan", assignee=worker.name)

    # The rows landed in the SHARED Postgres, stamped with this company's id (auto-stamped by the
    # session GUC — chorus never wrote company_id itself).
    with psycopg.connect(_pg_conninfo(database_url, user="postgres")) as admin:
        employee_companies = {
            str(row[0])
            for row in admin.execute("SELECT DISTINCT company_id FROM employee").fetchall()
        }
        assert company_id in employee_companies
        row = admin.execute(
            "SELECT company_id, intent FROM task WHERE id = %s", (task.id,)
        ).fetchone()
        assert row is not None
        assert (str(row[0]), row[1]) == (company_id, "write the launch plan")
    # And no SQLite file was created — Postgres is the store, not a mirror.
    assert not (tmp_path / "co" / "ledger.db").exists()


def test_two_postgres_companies_are_isolated(database_url: str, tmp_path: Path) -> None:
    dsn = _pg_conninfo(database_url, user="podium_app")
    id_a, id_b = str(uuid4()), str(uuid4())
    graph_a = build(
        CompanyConfig(**_FAKE_MODEL, workdir=tmp_path / "a", company_id=id_a, ledger_dsn=dsn)
    )
    graph_b = build(
        CompanyConfig(**_FAKE_MODEL, workdir=tmp_path / "b", company_id=id_b, ledger_dsn=dsn)
    )
    graph_a.org.hire(name="Ace", role="backend_engineer")
    # B's graph sees none of A's employees: FORCE RLS scopes podium_app to B's company only.
    assert all(employee.name != "Ace" for employee in graph_b.org._ledger.employees.list())
    # Both companies can employ the same slug — composite (company_id, id) identity.
    hired_b = graph_b.org.hire(name="Ace", role="backend_engineer")
    assert hired_b.id == "ace"


async def test_conductor_host_honours_the_ledger_backend_flag(
    database_url: str,
    sessionmaker: async_sessionmaker[AsyncSession],
    app_sessionmaker: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """companies.ledger_backend is the rollout lever: postgres companies get PostgresLedger,
    sqlite companies keep the per-company file — decided per company, inside one conductor."""
    from chorus.ledger import SqliteLedger
    from chorus.ledger.postgres import PostgresLedger

    from podium.companies import create_company

    # Imported from the module, not the package init — podium.conductor deliberately avoids
    # importing chorus at package-import time (the graph host is conductor-process-only).
    from podium.conductor._chorus_executor import CompanyGraphHost
    from podium.logs import RunLogStore
    from podium.workspaces import create_workspace

    async with sessionmaker() as s, s.begin():
        ws = await create_workspace(s, name="A", slug="a")
        pg_company = await create_company(
            s, workspace_id=ws.id, slug="pg", name="PG", ledger_backend="postgres"
        )
        lite_company = await create_company(
            s, workspace_id=ws.id, slug="lite", name="Lite", ledger_backend="sqlite"
        )
        ws_id, pg_id, lite_id = ws.id, pg_company.id, lite_company.id

    host = CompanyGraphHost(
        **_FAKE_MODEL,
        workdir=tmp_path,
        app_sessionmaker=app_sessionmaker,
        log_store=RunLogStore(tmp_path / "logs"),
        engine_ledger_dsn=_pg_conninfo(database_url, user="podium_app"),
    )
    pg_runtime = await host.ensure(pg_id, ws_id)
    lite_runtime = await host.ensure(lite_id, ws_id)
    try:
        assert isinstance(pg_runtime.graph.org._ledger, PostgresLedger)
        assert isinstance(lite_runtime.graph.org._ledger, SqliteLedger)
    finally:
        await host.aclose()


def test_non_uuid_company_id_is_rejected_for_postgres(tmp_path: Path) -> None:
    """The RLS GUC casts to uuid — a non-uuid company id must fail at build time, not mid-query."""
    with pytest.raises(ValueError, match="uuid"):
        build(
            CompanyConfig(
                **_FAKE_MODEL,
                workdir=tmp_path / "x",
                company_id="company",  # the SQLite-era default — not a uuid
                ledger_dsn="postgresql://ignored@localhost/ignored",
            )
        )
