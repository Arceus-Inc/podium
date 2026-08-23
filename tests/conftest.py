"""Spin a throwaway PostgreSQL 18 cluster for the suite and run real Alembic migrations against it.

No Docker required: initdb into a tmpdir, pg_ctl on a free TCP port (LC_ALL=C works around the
macOS/PG18 'postmaster became multithreaded' FATAL), alembic upgrade head, tear the cluster down.
This is the "green gate against real Postgres" M0 exits on.
"""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import sys
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from podium.db import make_engine, make_sessionmaker

PG_BIN = Path("/opt/homebrew/opt/postgresql@18/bin")


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@pytest.fixture(scope="session")
def database_url(tmp_path_factory: pytest.TempPathFactory) -> Iterator[str]:
    if not PG_BIN.exists():
        pytest.skip(f"PostgreSQL 18 not found at {PG_BIN}")
    data = tmp_path_factory.mktemp("pgdata")
    env = {**os.environ, "LC_ALL": "C"}
    subprocess.run(
        # UTF8 encoding (real event payloads carry model Unicode); locale C keeps the PG18/macOS
        # 'multithreaded postmaster' workaround while allowing UTF8 storage.
        [
            str(PG_BIN / "initdb"),
            "-D",
            str(data),
            "-U",
            "postgres",
            "--auth=trust",
            "--encoding=UTF8",
            "--locale=C",
        ],
        check=True,
        capture_output=True,
        env=env,
    )
    port = _free_port()
    subprocess.run(
        [
            str(PG_BIN / "pg_ctl"),
            "-D",
            str(data),
            "-o",
            f"-p {port} -c listen_addresses=127.0.0.1",
            "-l",
            str(data / "log"),
            "-w",
            "start",
        ],
        check=True,
        capture_output=True,
        env=env,
    )
    url = f"postgresql+asyncpg://postgres@127.0.0.1:{port}/postgres"
    try:
        subprocess.run(
            [sys.executable, "-m", "alembic", "upgrade", "head"],
            check=True,
            capture_output=True,
            env={**env, "PODIUM_DATABASE_URL": url},
            cwd=Path(__file__).resolve().parent.parent,
        )
        yield url
    finally:
        subprocess.run(
            [str(PG_BIN / "pg_ctl"), "-D", str(data), "-w", "stop"],
            capture_output=True,
            env=env,
        )
        shutil.rmtree(data, ignore_errors=True)


@pytest_asyncio.fixture
async def sessionmaker(database_url: str) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """Superuser session — bypasses RLS. Control-plane work (create workspaces) and test setup."""
    engine = make_engine(database_url)
    try:
        yield make_sessionmaker(engine)
    finally:
        await engine.dispose()


@pytest_asyncio.fixture
async def app_sessionmaker(database_url: str) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """Runtime session as the non-superuser `podium_app` role — subject to FORCE RLS."""
    engine = make_engine(database_url.replace("://postgres@", "://podium_app@"))
    try:
        yield make_sessionmaker(engine)
    finally:
        await engine.dispose()


@pytest_asyncio.fixture(autouse=True)
async def _clean(database_url: str) -> AsyncIterator[None]:
    """Truncate tenant tables after each test, as superuser so RLS never hides rows."""
    yield
    engine = make_engine(database_url)
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "TRUNCATE workspaces, companies, api_keys, users, runs, commands, events, "
                "projection_cursors, timeline_items, entity_edges, stream_tickets CASCADE"
            )
        )
    await engine.dispose()
