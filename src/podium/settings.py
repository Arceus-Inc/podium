"""Runtime configuration, read once from the environment (pydantic-settings)."""

from __future__ import annotations

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """All podium config. `database_url` is the only required value in M0."""

    model_config = SettingsConfigDict(env_prefix="PODIUM_", env_file=".env", extra="ignore")

    # asyncpg driver URL, e.g. postgresql+asyncpg://podium:podium@localhost:5432/podium
    database_url: str = "postgresql+asyncpg://podium:podium@localhost:5432/podium"
    # pool sizing — asyncpg pool via SQLAlchemy; conservative defaults, tune per shard later.
    db_pool_size: int = 5
    db_max_overflow: int = 5
    log_json: bool = True

    # Per-company JWT signing. The master secret MUST be overridden in any real deployment.
    jwt_master_secret: str = "dev-insecure-change-me"
    instance_id: str = "local"

    # Per-actor rate limit (sliding window).
    rate_limit_max: int = 100
    rate_limit_window_seconds: float = 60.0

    # Conductor. Model creds drive real CompanyGraphs; control url is a privileged (RLS-bypass)
    # connection used only for cross-tenant discovery. app work still goes through podium_app.
    model_api_key: str = ""
    model_base_url: str = ""
    model_deployment: str = ""
    workdir: Path = Path(".podium")
    # Durable run transcripts (file per run). The conductor writes these and the api reads them, so
    # both MUST share this path (dev-embedded or a shared volume); across hosts without shared storage
    # /logs 404s until the deferred object-store mirror lands.
    log_dir: Path = Path(".podium/logs")
    conductor_embedded: bool = False  # dev: run the conductor inside the api lifespan
    conductor_control_database_url: str = ""  # empty → same as database_url
    conductor_lease_seconds: int = 300
    conductor_poll_interval: float = 5.0
    conductor_batch_size: int = 10

    # M5: the engine state store. Companies with ledger_backend="postgres" run their chorus ledger
    # on this sync-psycopg DSN (RLS-scoped per company under the non-superuser role). Empty → derive
    # from database_url (strip the +asyncpg driver marker).
    engine_ledger_dsn: str = ""

    def resolved_engine_ledger_dsn(self) -> str:
        return self.engine_ledger_dsn or self.database_url.replace("+asyncpg", "")


def get_settings() -> Settings:
    return Settings()
