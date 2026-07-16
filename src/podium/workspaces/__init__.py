"""Workspaces domain package. models + service today; router + schemas arrive with M1."""

from __future__ import annotations

from podium.workspaces.models import Workspace
from podium.workspaces.service import create_workspace

__all__ = ["Workspace", "create_workspace"]
