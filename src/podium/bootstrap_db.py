"""One-shot database bootstrap for deployments: role + migrations (CP-6).

``python -m podium.bootstrap_db`` — connects with the ADMIN url (DDL owner), ensures the
non-superuser ``podium_app`` runtime role exists, then runs ``alembic upgrade head`` (which
also syncs pending engine deltas — see migrations/env.py). Idempotent; compose runs it as the
``migrate`` service before api/conductor start.
"""

from __future__ import annotations

import subprocess
import sys

import psycopg

from podium.settings import get_settings

_ENSURE_ROLE = """
DO $$ BEGIN
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'podium_app') THEN
        CREATE ROLE podium_app LOGIN NOSUPERUSER NOBYPASSRLS;
    END IF;
END $$
"""


def main() -> int:
    settings = get_settings()
    admin_dsn = settings.database_url.replace("+asyncpg", "")
    with psycopg.connect(admin_dsn, autocommit=True) as connection:
        connection.execute(_ENSURE_ROLE)
    completed = subprocess.run([sys.executable, "-m", "alembic", "upgrade", "head"], check=False)
    return completed.returncode


if __name__ == "__main__":
    raise SystemExit(main())
