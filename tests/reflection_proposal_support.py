"""Real-ledger setup for Podium reflection proposal views."""

from __future__ import annotations

import uuid

from chorus.ids import mint_id
from chorus.ledger import (
    Artifact,
    ArtifactRevision,
    ArtifactType,
    Ledger,
    ReflectionProposal,
    ReflectionProposalTarget,
    ReflectionTargetKind,
    Routine,
    RoutineRun,
    RoutineTrigger,
    Run,
    RunStatus,
    Task,
    TrajectoryRef,
)
from chorus.workforce import Employee


def _trajectory(ledger: Ledger, suffix: str) -> TrajectoryRef:
    employee_id = f"trajectory-agent-{suffix}"
    ledger.employees.create(Employee(id=employee_id, name="Trajectory Agent", role="engineer"))
    task = ledger.tasks.submit(Task(id=mint_id(), intent=f"trajectory {suffix}"))
    run = ledger.runs.create(
        Run(id=mint_id(), employee_id=employee_id, task_id=task.id, status=RunStatus.SUCCEEDED)
    )
    return TrajectoryRef(run_id=run.id, task_id=task.id)


def create_reflection_proposal(
    database_url: str,
    company_id: uuid.UUID,
    *,
    suffix: str,
) -> ReflectionProposal:
    """Create a valid immutable proposal through Chorus's public repositories."""
    dsn = database_url.replace("+asyncpg", "").replace("://postgres@", "://podium_app@")
    ledger = Ledger.open(dsn, company_id=str(company_id))
    try:
        coach_id = f"reflection-coach-{suffix}"
        target_id = f"target-agent-{suffix}"
        ledger.employees.create(
            Employee(id=coach_id, name="Reflection Coach", role="reflection_coach")
        )
        ledger.employees.create(Employee(id=target_id, name="Target Agent", role="engineer"))

        source_task = ledger.tasks.submit(
            Task(id=mint_id(), intent="review repeated trajectory failures")
        )
        routine = ledger.routines.create(
            Routine(
                id=mint_id(),
                employee_id=coach_id,
                intent_template="proposal-only reflection",
            )
        )
        trigger = ledger.routine_triggers.create(
            RoutineTrigger(id=mint_id(), routine_id=routine.id)
        )
        routine_run = ledger.routine_runs.record(
            RoutineRun(id=mint_id(), routine_id=routine.id, trigger_id=trigger.id)
        )
        ledger.routine_runs.dispatch(routine_run.id, linked_task_id=source_task.id)
        ledger.routine_runs.complete(routine_run.id)
        source_run = ledger.runs.create(
            Run(
                id=mint_id(),
                employee_id=coach_id,
                task_id=source_task.id,
                status=RunStatus.SUCCEEDED,
            )
        )

        evidence_task = ledger.tasks.submit(Task(id=mint_id(), intent="capture evidence"))
        evidence_artifact = ledger.artifacts.create(
            Artifact(id=mint_id(), task_id=evidence_task.id, type=ArtifactType.FINDING)
        )
        evidence_revision = ledger.artifact_revisions.record(
            ArtifactRevision(id=mint_id(), artifact_id=evidence_artifact.id)
        )

        return ledger.reflection_proposals.create(
            ReflectionProposal(
                artifact_id=mint_id(),
                artifact_revision_id=mint_id(),
                target=ReflectionProposalTarget(
                    kind=ReflectionTargetKind.SKILL,
                    owner_employee_id=target_id,
                    target_id="backend-engineer/replay-evidence",
                    target_revision="skill@4",
                ),
                diff=(
                    "--- a/SKILL.md\n+++ b/SKILL.md\n@@ -1 +1,2 @@\n existing\n"
                    "+require replay evidence\n"
                ),
                rationale="Repeated failures omit replay evidence.",
                trajectory_refs=(
                    _trajectory(ledger, f"{suffix}-one"),
                    _trajectory(ledger, f"{suffix}-two"),
                ),
                evidence_artifact_revision_ids=(evidence_revision.id,),
                source_routine_run_id=routine_run.id,
                source_run_id=source_run.id,
                source_employee_id=coach_id,
            )
        )
    finally:
        ledger.close()


__all__ = ["create_reflection_proposal"]
