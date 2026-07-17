"""The cockpit snapshot — every component's visibility counts in one read (design §3).

Assembled from the control plane's facades (engine ledger, RLS-scoped), the product DB (runs,
spend rollups), and lattice's file-backed semantic store under the company workdir. Internal
components appear as counts and read-only detail — never as control surfaces.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from chorus.memory import EpisodicStore
from lattice.stores.memory_md import MemoryMdStore

from podium.control import CompanyControlPlane


def semantic_root(workdir: Path, company_id: uuid.UUID) -> Path:
    """Where the company's lattice tree lives (workdir-local by design — M3c caveat applies)."""
    return workdir / str(company_id) / "lattice"


def semantic_counts(workdir: Path, company_id: uuid.UUID) -> dict[str, Any]:
    """Active semantic atoms per employee, read straight off the file store."""
    root = semantic_root(workdir, company_id)
    by_employee: dict[str, int] = {}
    if root.is_dir():
        store = MemoryMdStore(root)
        for employee_dir in sorted(root.iterdir()):
            if not (employee_dir / "semantic").is_dir():
                continue
            count = len(store.list_active(employee_dir.name))
            if count:
                by_employee[employee_dir.name] = count
    return {"atoms": sum(by_employee.values()), "by_employee": by_employee}


def semantic_facts(workdir: Path, company_id: uuid.UUID, employee_id: str) -> list[dict[str, Any]]:
    """One employee's active atoms — the Memory section's Semantic detail (read-only)."""
    root = semantic_root(workdir, company_id)
    if not root.is_dir():
        return []
    return [
        {
            "key": atom.key,
            "value": atom.value,
            "activation": atom.activation,
            "source_run_ids": list(atom.source_run_ids),
            "created_at": atom.created_at.isoformat(),
            "key_files": list(atom.key_files),
        }
        for atom in MemoryMdStore(root).list_active(employee_id)
    ]


def engine_components(plane: CompanyControlPlane) -> dict[str, Any]:
    """The engine-side component counts, through the plane's existing facades (no re-derivation)."""
    status = plane.observe.status()
    teams = plane.delegation.teams()
    goal_roots = plane.direction.goal_tree()
    spend = plane.observe.spend_total_cents()
    skills_heads = sum(len(plane.observe.skills(member.id)) for member in plane.workforce.roster())
    board = plane.allocation.board()
    return {
        "horizon": {"goals": len(goal_roots)},
        "chorus": {
            "employees": status.employees,
            "open_tasks": status.open_tasks,
            "running_beats": status.running_beats,
            "blocked": status.blocked_tasks,
            "queued_wakes": len(board.queued),
        },
        "delegation": {"teams": len(teams)},
        "skills": {"heads": skills_heads},
        "llmops": {"spend_cents": spend},
    }


def episodic_counts(workdir: Path, company_id: uuid.UUID) -> dict[str, Any]:
    """Beat-record counts from the company's episodic store (workdir-local SQLite by design)."""
    memory_dir = workdir / str(company_id) / "memory"
    if not memory_dir.is_dir():
        return {"records": 0}
    store = EpisodicStore(memory_dir)
    try:
        return {"records": store.count()}
    finally:
        store.close()


def build_snapshot(
    plane: CompanyControlPlane, *, workdir: Path, company_id: uuid.UUID
) -> dict[str, Any]:
    components = engine_components(plane)
    components["semantic"] = semantic_counts(workdir, company_id)
    components["episodic"] = episodic_counts(workdir, company_id)
    return {"components": components, "generated_at": datetime.now(UTC).isoformat()}


__all__ = [
    "build_snapshot",
    "episodic_counts",
    "semantic_counts",
    "semantic_facts",
    "semantic_root",
]
