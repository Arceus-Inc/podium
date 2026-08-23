"""Real-Postgres proofs for immutable, tenant-isolated entity lineage."""

from __future__ import annotations

import asyncio
import uuid

import pytest
from sqlalchemy import UniqueConstraint, text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import podium.db.metadata  # noqa: F401  -- register all ORM FK targets before ORM flushes
from podium.companies import Company, create_company
from podium.db import tenant_session
from podium.entity_edges import (
    EntityEdge,
    EntityEdgeConflictError,
    EntityEdgeDraft,
    EntityEdgeRepository,
    EntityPredicate,
    TraversalBudgetExhausted,
    UnknownEntityPredicate,
)
from podium.runs import Run, create_run
from podium.workspaces import create_workspace


async def _company(
    admin: async_sessionmaker[AsyncSession], *, slug: str
) -> tuple[uuid.UUID, uuid.UUID]:
    async with admin() as session, session.begin():
        workspace = await create_workspace(session, name=slug, slug=slug)
        company = await create_company(
            session, workspace_id=workspace.id, slug=f"{slug}-company", name=slug
        )
    return workspace.id, company.id


def _edge(
    source: str,
    destination: str,
    *,
    run_id: uuid.UUID | None = None,
    employee_id: str | None = None,
) -> EntityEdgeDraft:
    return EntityEdgeDraft(
        src_type="task",
        src_id=source,
        predicate=EntityPredicate.DEPENDS_ON,
        dst_type="task",
        dst_id=destination,
        run_id=run_id,
        employee_id=employee_id,
    )


async def _append(
    app: async_sessionmaker[AsyncSession],
    *,
    workspace_id: uuid.UUID,
    company_id: uuid.UUID,
    source: str,
    destination: str,
) -> None:
    async with tenant_session(app, workspace_id) as session:
        await EntityEdgeRepository(
            session, company_id=company_id, workspace_id=workspace_id
        ).append(_edge(source, destination))


async def test_entity_edges_migration_enforces_rls_ownership_and_append_only(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    async with sessionmaker() as session:
        table = (
            await session.execute(
                text(
                    "SELECT relrowsecurity, relforcerowsecurity "
                    "FROM pg_class WHERE relname = 'entity_edges'"
                )
            )
        ).one()
        constraints = set(
            (
                await session.execute(
                    text(
                        "SELECT conname FROM pg_constraint "
                        "WHERE conrelid = 'entity_edges'::regclass"
                    )
                )
            ).scalars()
        )
        trigger = await session.scalar(
            text("SELECT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname = 'entity_edges_immutable')")
        )

    assert table == (True, True)
    assert {
        "fk_entity_edges_company_workspace",
        "fk_entity_edges_run_company_workspace",
        "fk_entity_edges_employee_company",
    } <= constraints
    assert trigger is True


def test_entity_edge_orm_metadata_matches_podium_owned_constraints() -> None:
    company_unique = {
        constraint.name: tuple(column.name for column in constraint.columns)
        for constraint in Company.__table__.constraints
        if isinstance(constraint, UniqueConstraint)
    }
    run_unique = {
        constraint.name: tuple(column.name for column in constraint.columns)
        for constraint in Run.__table__.constraints
        if isinstance(constraint, UniqueConstraint)
    }
    edge_foreign_keys = {
        constraint.name: tuple(column.name for column in constraint.columns)
        for constraint in EntityEdge.__table__.foreign_key_constraints
    }

    assert company_unique["uq_companies_id_workspace_id"] == ("id", "workspace_id")
    assert run_unique["uq_runs_id_company_id_workspace_id"] == (
        "id",
        "company_id",
        "workspace_id",
    )
    assert edge_foreign_keys["fk_entity_edges_company_workspace"] == (
        "company_id",
        "workspace_id",
    )
    assert edge_foreign_keys["fk_entity_edges_run_company_workspace"] == (
        "run_id",
        "company_id",
        "workspace_id",
    )
    # `employee` is Chorus-owned, so its FK is migration-owned and asserted against PostgreSQL above.
    assert "employee" not in EntityEdge.metadata.tables


async def test_traversal_is_cycle_safe_deterministic_and_hop_bounded(
    sessionmaker: async_sessionmaker[AsyncSession],
    app_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    workspace_id, company_id = await _company(sessionmaker, slug="lineage")
    for source, destination in (("a", "b"), ("b", "c"), ("c", "a"), ("c", "d")):
        await _append(
            app_sessionmaker,
            workspace_id=workspace_id,
            company_id=company_id,
            source=source,
            destination=destination,
        )

    async with tenant_session(app_sessionmaker, workspace_id) as session:
        repository = EntityEdgeRepository(session, company_id=company_id, workspace_id=workspace_id)
        cycle = await repository.traverse(
            "task", "a", [EntityPredicate.DEPENDS_ON], hops=4, budget=10
        )
        bounded = await repository.traverse(
            "task", "a", [EntityPredicate.DEPENDS_ON], hops=2, budget=10
        )
        with pytest.raises(ValueError, match="must not exceed 4"):
            await repository.traverse("task", "a", [EntityPredicate.DEPENDS_ON], hops=5)

    assert [(row.edge.src_id, row.edge.dst_id, row.hop) for row in cycle] == [
        ("a", "b", 1),
        ("b", "c", 2),
        ("c", "a", 3),
        ("c", "d", 3),
    ]
    assert [(row.edge.src_id, row.edge.dst_id, row.hop) for row in bounded] == [
        ("a", "b", 1),
        ("b", "c", 2),
    ]


async def test_traversal_fails_clearly_at_budget_and_rejects_unknown_predicates(
    sessionmaker: async_sessionmaker[AsyncSession],
    app_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    workspace_id, company_id = await _company(sessionmaker, slug="budget")
    for source, destination in (("a", "b"), ("a", "c"), ("a", "d")):
        await _append(
            app_sessionmaker,
            workspace_id=workspace_id,
            company_id=company_id,
            source=source,
            destination=destination,
        )

    async with tenant_session(app_sessionmaker, workspace_id) as session:
        repository = EntityEdgeRepository(session, company_id=company_id, workspace_id=workspace_id)
        with pytest.raises(TraversalBudgetExhausted, match="budget 2 exhausted"):
            await repository.traverse("task", "a", [EntityPredicate.DEPENDS_ON], budget=2)
        with pytest.raises(UnknownEntityPredicate, match="unknown entity predicate"):
            await repository.traverse("task", "a", ["NOT_A_PREDICATE"])


async def test_append_replays_duplicates_and_database_rows_are_immutable(
    sessionmaker: async_sessionmaker[AsyncSession],
    app_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    workspace_id, company_id = await _company(sessionmaker, slug="replay")
    async with tenant_session(app_sessionmaker, workspace_id) as session:
        run, _ = await create_run(
            session,
            workspace_id=workspace_id,
            company_id=company_id,
            directive="trace lineage",
            idempotency_key="lineage-run",
        )
        repository = EntityEdgeRepository(session, company_id=company_id, workspace_id=workspace_id)
        draft = _edge("a", "b", run_id=run.id)
        first = await repository.append(draft)
        replay = await repository.append(draft)
        with pytest.raises(EntityEdgeConflictError) as run_conflict:
            await repository.append(_edge("a", "b"))
        with pytest.raises(EntityEdgeConflictError) as employee_conflict:
            await repository.append(_edge("a", "b", run_id=run.id, employee_id="other"))

    assert replay.id == first.id
    assert run_conflict.value.existing_run_id == run.id
    assert run_conflict.value.requested_run_id is None
    assert employee_conflict.value.existing_employee_id is None
    assert employee_conflict.value.requested_employee_id == "other"
    async with sessionmaker() as session:
        with pytest.raises(DBAPIError, match="append-only"):
            async with session.begin():
                await session.execute(
                    text("UPDATE entity_edges SET dst_id = 'changed' WHERE id = :id"),
                    {"id": first.id},
                )
        with pytest.raises(DBAPIError, match="append-only"):
            async with session.begin():
                await session.execute(
                    text("DELETE FROM entity_edges WHERE id = :id"), {"id": first.id}
                )


async def test_concurrent_divergent_replay_raises_provenance_conflict(
    sessionmaker: async_sessionmaker[AsyncSession],
    app_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    workspace_id, company_id = await _company(sessionmaker, slug="concurrent-replay")
    async with tenant_session(app_sessionmaker, workspace_id) as session:
        run, _ = await create_run(
            session,
            workspace_id=workspace_id,
            company_id=company_id,
            directive="trace lineage",
            idempotency_key="concurrent-lineage-run",
        )

    started = asyncio.Event()

    async def append_competing_replay() -> EntityEdge:
        async with tenant_session(app_sessionmaker, workspace_id) as session:
            started.set()
            return await EntityEdgeRepository(
                session, company_id=company_id, workspace_id=workspace_id
            ).append(_edge("concurrent-a", "concurrent-b"))

    async with tenant_session(app_sessionmaker, workspace_id) as session:
        first = await EntityEdgeRepository(
            session, company_id=company_id, workspace_id=workspace_id
        ).append(_edge("concurrent-a", "concurrent-b", run_id=run.id))
        competing = asyncio.create_task(append_competing_replay())
        await started.wait()
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(asyncio.shield(competing), timeout=0.05)

    with pytest.raises(EntityEdgeConflictError) as conflict:
        await competing
    assert conflict.value.edge_id == first.id
    assert conflict.value.existing_run_id == run.id
    assert conflict.value.requested_run_id is None


async def test_entity_edges_are_denied_across_tenants_and_company_workspace_mismatches(
    sessionmaker: async_sessionmaker[AsyncSession],
    app_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    alpha_workspace, alpha_company = await _company(sessionmaker, slug="alpha-edges")
    beta_workspace, _ = await _company(sessionmaker, slug="beta-edges")
    await _append(
        app_sessionmaker,
        workspace_id=alpha_workspace,
        company_id=alpha_company,
        source="a",
        destination="b",
    )

    async with tenant_session(app_sessionmaker, beta_workspace) as session:
        foreign = EntityEdgeRepository(
            session, company_id=alpha_company, workspace_id=alpha_workspace
        )
        assert await foreign.traverse("task", "a", [EntityPredicate.DEPENDS_ON]) == []
        with pytest.raises(DBAPIError):
            await foreign.append(_edge("x", "y"))

    with pytest.raises(IntegrityError):
        async with tenant_session(app_sessionmaker, beta_workspace) as session:
            mismatched = EntityEdgeRepository(
                session, company_id=alpha_company, workspace_id=beta_workspace
            )
            await mismatched.append(_edge("x", "y"))
