"""Host orchestration for immutable engine-owned PostgreSQL migration streams.

Podium owns the database connection and runtime role grants; Chorus, Horizon, and Lattice own
their DDL.  This module deliberately consumes each engine's small migration contract instead of
copying its SQL or maintaining a second list of migration identifiers.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from sqlalchemy import text
from sqlalchemy.engine import Connection


class MigrationContract(Protocol):
    """The common immutable migration surface exported by every hosted engine."""

    @property
    def id(self) -> str: ...

    @property
    def checksum(self) -> str: ...

    def statements(self) -> list[str]: ...

    def table_names(self) -> list[str]: ...


@dataclass(frozen=True)
class MigrationRecord:
    """One expected or recorded immutable migration checksum."""

    id: str
    checksum: str


@dataclass(frozen=True)
class EngineMigrationStream:
    """An engine-owned stream and its isolated applied-migration table."""

    name: str
    metadata_table: str
    migrations: tuple[MigrationContract, ...]
    required_records: tuple[MigrationRecord, ...] = ()


class EngineMigrationNotReadyError(RuntimeError):
    """A shipped engine migration has not been applied to this database."""


class EngineMigrationDriftError(RuntimeError):
    """An applied immutable migration's checksum no longer matches its package."""


class EngineMigrationAheadError(RuntimeError):
    """The database contains an engine migration not shipped by this process."""


def engine_migration_streams() -> tuple[EngineMigrationStream, ...]:
    """Load the migration contracts from the engine packages at the process boundary."""
    from chorus.ledger import baseline
    from chorus.ledger import load_migrations as load_chorus_migrations
    from horizon.store.postgres import load_migrations as load_horizon_migrations
    from lattice.migrations import load_migrations as load_lattice_migrations

    baseline_id, baseline_checksum, _ = baseline()
    return (
        EngineMigrationStream(
            name="chorus",
            metadata_table="chorus_schema_migrations",
            migrations=tuple(load_chorus_migrations()),
            required_records=(MigrationRecord(baseline_id, baseline_checksum),),
        ),
        EngineMigrationStream(
            name="horizon",
            metadata_table="horizon_schema_migrations",
            migrations=tuple(load_horizon_migrations()),
        ),
        EngineMigrationStream(
            name="lattice",
            metadata_table="lattice_schema_migrations",
            migrations=tuple(load_lattice_migrations()),
        ),
    )


def sync_engine_migrations(connection: Connection) -> None:
    """Apply all pending engine migrations inside Alembic's migration transaction.

    The Chorus baseline is installed by Podium revision 0008.  Before that revision exists we
    intentionally do nothing so partial historical Alembic upgrades retain their old behavior.
    Every later stream is guarded by an exclusive lock on its own metadata table.
    """
    streams = engine_migration_streams()
    if not _metadata_table_exists(connection, streams[0].metadata_table):
        return
    for stream in streams:
        _sync_stream(connection, stream)


def verify_engine_migrations(connection: Connection) -> None:
    """Prove every stream is complete, immutable, and no newer than this process."""
    for stream in engine_migration_streams():
        if not _metadata_table_exists(connection, stream.metadata_table):
            raise EngineMigrationNotReadyError(f"{stream.name} metadata table is missing")
        _validate_applied(stream, _read_applied(connection, stream))


def _sync_stream(connection: Connection, stream: EngineMigrationStream) -> None:
    connection.execute(
        text(
            f"CREATE TABLE IF NOT EXISTS {stream.metadata_table} ("
            "id text PRIMARY KEY, checksum text NOT NULL, "
            "applied_at timestamptz NOT NULL DEFAULT now())"
        )
    )
    connection.execute(text(f"LOCK TABLE {stream.metadata_table} IN EXCLUSIVE MODE"))
    applied = _read_applied(connection, stream)
    pending = _validate_applied(stream, applied, allow_pending=True)
    for migration in pending:
        for statement in migration.statements():
            connection.execute(text(statement))
        connection.execute(
            text(
                f"INSERT INTO {stream.metadata_table} (id, checksum, applied_at) "
                "VALUES (:identifier, :checksum, now())"
            ).bindparams(identifier=migration.id, checksum=migration.checksum)
        )
        for table_name in migration.table_names():
            _grant_runtime_table_access(connection, table_name)
    _grant_runtime_metadata_access(connection, stream.metadata_table)


def _validate_applied(
    stream: EngineMigrationStream,
    applied: tuple[MigrationRecord, ...],
    *,
    allow_pending: bool = False,
) -> tuple[MigrationContract, ...]:
    expected = stream.required_records + tuple(
        MigrationRecord(migration.id, migration.checksum) for migration in stream.migrations
    )
    expected_ids = {record.id for record in expected}
    if len(expected_ids) != len(expected):
        raise RuntimeError(f"{stream.name} ships duplicate migration identifiers")
    applied_ids = {record.id for record in applied}
    ahead = applied_ids - expected_ids
    if ahead:
        raise EngineMigrationAheadError(f"{stream.name} database migration is ahead")
    for record in expected:
        applied_checksum = _checksum_for(applied, record.id)
        if applied_checksum is None:
            if allow_pending and _migration_for(stream.migrations, record.id) is not None:
                continue
            raise EngineMigrationNotReadyError(f"{stream.name} migration is missing")
        if applied_checksum != record.checksum:
            raise EngineMigrationDriftError(f"{stream.name} migration checksum drift")
    return tuple(
        migration
        for migration in stream.migrations
        if _checksum_for(applied, migration.id) is None
    )


def _metadata_table_exists(connection: Connection, table_name: str) -> bool:
    value = connection.execute(
        text("SELECT to_regclass(:table_name)").bindparams(table_name=table_name)
    ).scalar_one()
    return value is not None


def _read_applied(connection: Connection, stream: EngineMigrationStream) -> tuple[MigrationRecord, ...]:
    rows = connection.execute(text(f"SELECT id, checksum FROM {stream.metadata_table}")).tuples()
    return tuple(MigrationRecord(id=str(row[0]), checksum=str(row[1])) for row in rows)


def _checksum_for(applied: tuple[MigrationRecord, ...], identifier: str) -> str | None:
    for record in applied:
        if record.id == identifier:
            return record.checksum
    return None


def _migration_for(
    migrations: tuple[MigrationContract, ...], identifier: str
) -> MigrationContract | None:
    for migration in migrations:
        if migration.id == identifier:
            return migration
    return None


def _grant_runtime_table_access(connection: Connection, table_name: str) -> None:
    connection.execute(text(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {table_name} TO podium_app"))


def _grant_runtime_metadata_access(connection: Connection, table_name: str) -> None:
    connection.execute(text(f"GRANT SELECT ON {table_name} TO podium_app"))
