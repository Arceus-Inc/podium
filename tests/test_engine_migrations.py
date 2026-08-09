"""The Podium migration root applies and verifies every engine-owned stream on real Postgres."""

from __future__ import annotations

import httpx
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from podium.db.engine_migrations import EngineMigrationStream, engine_migration_streams
from podium.main import create_app


def _expected_records(stream: EngineMigrationStream) -> tuple[tuple[str, str], ...]:
    return tuple(
        sorted(
            [(record.id, record.checksum) for record in stream.required_records]
            + [(migration.id, migration.checksum) for migration in stream.migrations]
        )
    )


def _engine_tables(stream: EngineMigrationStream) -> tuple[str, ...]:
    return tuple(
        table_name for migration in stream.migrations for table_name in migration.table_names()
    )


async def test_host_applies_engine_streams_with_exact_records_rls_and_grants(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """Fresh Alembic upgrade applies immutable DDL and its smallest usable runtime grants."""
    async with sessionmaker() as session:
        for stream in engine_migration_streams():
            rows = (
                await session.execute(text(f"SELECT id, checksum FROM {stream.metadata_table}"))
            ).tuples()
            assert tuple(sorted((str(row[0]), str(row[1])) for row in rows)) == _expected_records(
                stream
            )

            metadata_granted = (
                await session.execute(
                    text(
                        "SELECT has_table_privilege("
                        "'podium_app', :table_name, 'SELECT')"
                    ).bindparams(table_name=stream.metadata_table)
                )
            ).scalar_one()
            assert metadata_granted is True

            for table_name in _engine_tables(stream):
                rls = (
                    await session.execute(
                        text(
                            "SELECT c.relrowsecurity, c.relforcerowsecurity "
                            "FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
                            "WHERE n.nspname = 'public' AND c.relname = :table_name"
                        ).bindparams(table_name=table_name)
                    )
                ).one()
                assert rls == (True, True), f"{table_name} must be protected by FORCE RLS"
                table_granted = (
                    await session.execute(
                        text(
                            "SELECT has_table_privilege("
                            "'podium_app', :table_name, 'SELECT, INSERT, UPDATE, DELETE')"
                        ).bindparams(table_name=table_name)
                    )
                ).scalar_one()
                assert table_granted is True


async def test_readyz_hides_checksum_drift(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    stream = next(stream for stream in engine_migration_streams() if stream.name == "horizon")
    migration = stream.migrations[0]
    app = create_app()
    app.state.sessionmaker = sessionmaker
    async with sessionmaker() as session:
        await session.execute(
            text(
                f"UPDATE {stream.metadata_table} SET checksum = 'drifted' "
                "WHERE id = :identifier"
            ).bindparams(identifier=migration.id)
        )
        await session.commit()
        try:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client:
                response = await client.get("/readyz")
        finally:
            await session.execute(
                text(
                    f"UPDATE {stream.metadata_table} SET checksum = :checksum "
                    "WHERE id = :identifier"
                ).bindparams(identifier=migration.id, checksum=migration.checksum)
            )
            await session.commit()
    assert response.status_code == 503
    assert response.json() == {"status": "unavailable"}


async def test_readyz_hides_database_ahead_state(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    stream = next(stream for stream in engine_migration_streams() if stream.name == "lattice")
    app = create_app()
    app.state.sessionmaker = sessionmaker
    async with sessionmaker() as session:
        await session.execute(
            text(
                f"INSERT INTO {stream.metadata_table} (id, checksum) "
                "VALUES ('9999_database_ahead', 'unshipped')"
            )
        )
        await session.commit()
        try:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client:
                response = await client.get("/readyz")
        finally:
            await session.execute(
                text(
                    f"DELETE FROM {stream.metadata_table} WHERE id = '9999_database_ahead'"
                )
            )
            await session.commit()
    assert response.status_code == 503
    assert response.json() == {"status": "unavailable"}
