"""The `workspaces` table — the tenant root every future product table hangs off."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import DateTime, String
from sqlalchemy.orm import Mapped, mapped_column

from podium.db import Base


def _now() -> datetime:
    return datetime.now(UTC)


class Workspace(Base):
    __tablename__ = "workspaces"

    id: Mapped[str] = mapped_column(
        String, primary_key=True
    )  # ws_<uuid4hex>, minted in the service
    slug: Mapped[str] = mapped_column(String, unique=True)  # URL-safe handle
    name: Mapped[str] = mapped_column(String)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
