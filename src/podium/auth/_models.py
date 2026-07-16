"""`api_keys` — the auth-bootstrap table. Deliberately NOT under RLS: a key is resolved by its
unguessable hash *before* any tenant context exists, and the row is what establishes that context.
Only the hash is stored; the raw token is shown once at creation.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import DateTime, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from podium.db import Base


def _now() -> datetime:
    return datetime.now(UTC)


class ApiKey(Base):
    __tablename__ = "api_keys"

    id: Mapped[str] = mapped_column(String, primary_key=True)  # ak_<uuid4hex>
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"))
    company_id: Mapped[str | None] = mapped_column(ForeignKey("companies.id"), nullable=True)
    user_id: Mapped[str | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    key_hash: Mapped[str] = mapped_column(String, unique=True)  # sha256 hex of the raw token
    prefix: Mapped[str] = mapped_column(String)  # first chars, for display only
    name: Mapped[str] = mapped_column(String)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
