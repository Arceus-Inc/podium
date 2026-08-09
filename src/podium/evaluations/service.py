"""Read-only comparison of immutable Chorus eval-run records."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, cast

from podium.evaluations.models import (
    AgentConfigRevisionInspection,
    AgentConfigSkillPinInspection,
    AgentConfigToolPinInspection,
    ArtifactRevisionInspection,
    EvalCaseInspection,
    EvalRunComparison,
    EvalRunDeltas,
    EvalRunSnapshot,
    FrozenJsonArray,
    FrozenJsonEntry,
    FrozenJsonObject,
    FrozenJsonValue,
    SkillRevisionInspection,
)

if TYPE_CHECKING:
    from chorus.ledger import (
        AgentConfigRevision,
        ArtifactRevision,
        EvalCase,
        EvalRun,
        Ledger,
        SkillRevision,
    )


class UnknownEvalRunError(ValueError):
    """An eval-run identifier is malformed or not visible in this company."""


class IncompatibleEvalRunsError(ValueError):
    """The requested records are pinned to different suites."""


class EvalRunComparisonFacade:
    """Translate the ledger's immutable records into a comparison view without mutation."""

    def __init__(self, ledger: Ledger) -> None:
        self._ledger = ledger

    def compare(
        self, *, baseline_run_id: str | None, candidate_run_id: str | None
    ) -> EvalRunComparison:
        baseline = self._run_or_unknown(baseline_run_id)
        candidate = self._run_or_unknown(candidate_run_id)
        if baseline.eval_suite_id != candidate.eval_suite_id:
            raise IncompatibleEvalRunsError
        suite = self._ledger.eval_suites.get(baseline.eval_suite_id)
        if suite is None:
            raise RuntimeError("persisted eval run references a missing eval suite")

        eval_cases = tuple(self._eval_case(case_id) for case_id in suite.case_ids)
        baseline_snapshot = self._snapshot(
            baseline, eval_cases, self._skill_revision(baseline.skill_revision_id)
        )
        candidate_snapshot = self._snapshot(
            candidate, eval_cases, self._skill_revision(candidate.skill_revision_id)
        )
        return EvalRunComparison(
            baseline=baseline_snapshot,
            candidate=candidate_snapshot,
            deltas=EvalRunDeltas(
                input_tokens=candidate.usage.input_tokens - baseline.usage.input_tokens,
                output_tokens=candidate.usage.output_tokens - baseline.usage.output_tokens,
                cost_usd=candidate.usage.cost_usd - baseline.usage.cost_usd,
                duration_ms=_duration_ms(candidate) - _duration_ms(baseline),
            ),
        )

    def _run_or_unknown(self, run_id: str | None) -> EvalRun:
        if run_id is None:
            raise UnknownEvalRunError
        try:
            uuid.UUID(run_id)
        except ValueError:
            raise UnknownEvalRunError from None
        run = self._ledger.eval_runs.get(run_id)
        if run is None:
            raise UnknownEvalRunError
        return run

    def _snapshot(
        self,
        run: EvalRun,
        eval_cases: tuple[EvalCaseInspection, ...],
        skill_revision: SkillRevisionInspection,
    ) -> EvalRunSnapshot:
        agent_config = self._agent_config_revision(run.agent_config_revision.value)
        artifact_revisions = tuple(
            self._artifact_revision(revision_id) for revision_id in run.artifact_revision_ids
        )
        return EvalRunSnapshot(
            id=run.id,
            eval_suite_id=run.eval_suite_id,
            eval_cases=eval_cases,
            skill_revision=skill_revision,
            agent_config_revision=agent_config,
            provider=run.provider,
            model=run.model,
            input_snapshot=run.input_snapshot.text,
            output_snapshot=run.output_snapshot.text,
            input_tokens=run.usage.input_tokens,
            output_tokens=run.usage.output_tokens,
            cost_usd=run.usage.cost_usd,
            status=run.status.value,
            artifact_revisions=artifact_revisions,
            created_at=_created_at(run.created_at, "eval run", run.id),
            started_at=run.started_at,
            completed_at=run.completed_at,
        )

    def _agent_config_revision(self, revision_id: str) -> AgentConfigRevisionInspection:
        revision = self._ledger.agent_config_revisions.get(revision_id)
        if revision is None:
            raise RuntimeError(
                f"persisted eval run references missing agent config revision {revision_id}"
            )
        return _agent_config_revision(revision)

    def _skill_revision(self, revision_id: str) -> SkillRevisionInspection:
        revision = self._ledger.skill_revisions.get(revision_id)
        if revision is None:
            raise RuntimeError(
                f"persisted eval run references missing skill revision {revision_id}"
            )
        return _skill_revision(revision)

    def _eval_case(self, case_id: str) -> EvalCaseInspection:
        case = self._ledger.eval_cases.get(case_id)
        if case is None:
            raise RuntimeError(f"persisted eval suite references missing eval case {case_id}")
        return _eval_case(case)

    def _artifact_revision(self, revision_id: str) -> ArtifactRevisionInspection:
        revision = self._ledger.artifact_revisions.get(revision_id)
        if revision is None:
            raise RuntimeError(
                f"persisted eval run references missing artifact revision {revision_id}"
            )
        return _artifact_revision(revision)


def _agent_config_revision(revision: AgentConfigRevision) -> AgentConfigRevisionInspection:
    return AgentConfigRevisionInspection(
        id=revision.id,
        agent_id=revision.agent.value,
        revision_no=revision.revision_no,
        agents_md_revision=revision.agents_md.revision,
        agents_md_content=revision.agents_md.content,
        provider=revision.provider_model.provider,
        model=revision.provider_model.model,
        sandbox_profile=revision.sandbox_profile.value,
        skill_pins=tuple(
            AgentConfigSkillPinInspection(pin.skill_revision_id) for pin in revision.skill_pins
        ),
        tool_pins=tuple(
            AgentConfigToolPinInspection(pin.identifier, pin.provenance)
            for pin in revision.tool_pins
        ),
        created_at=_created_at(revision.created_at, "agent config revision", revision.id),
    )


def _skill_revision(revision: SkillRevision) -> SkillRevisionInspection:
    return SkillRevisionInspection(
        id=revision.id,
        skill_id=revision.skill_id,
        revision_no=revision.revision_no,
        action=revision.action,
        file_inventory=revision.file_inventory,
        content_hash=revision.content_hash,
        label=revision.label,
        source_run_ids=revision.source_run_ids,
        author_run_id=revision.author_run_id,
        restored_from_revision_id=revision.restored_from_revision_id,
        created_at=_created_at(revision.created_at, "skill revision", revision.id),
    )


def _eval_case(case: EvalCase) -> EvalCaseInspection:
    return EvalCaseInspection(
        id=case.id,
        skill_revision_id=case.skill_revision_id,
        name=case.name,
        input_text=case.input_text,
        expected_behavior=case.expected_behavior,
        created_at=_created_at(case.created_at, "eval case", case.id),
    )


def _artifact_revision(revision: ArtifactRevision) -> ArtifactRevisionInspection:
    return ArtifactRevisionInspection(
        id=revision.id,
        artifact_id=revision.artifact_id,
        revision=revision.revision,
        resource_ref=(
            _freeze_json_object(revision.resource_ref)
            if revision.resource_ref is not None
            else None
        ),
        summary=revision.summary,
        created_by_run_id=revision.created_by_run_id,
        created_at=_created_at(revision.created_at, "artifact revision", revision.id),
    )


def _created_at(created_at: object, kind: str, record_id: str) -> datetime:
    if not isinstance(created_at, datetime):
        raise RuntimeError(f"persisted {kind} {record_id} is missing created_at")
    return created_at


def _freeze_json_object(value: dict[str, object]) -> FrozenJsonObject:
    return FrozenJsonObject(
        tuple(FrozenJsonEntry(key, _freeze_json(item)) for key, item in value.items())
    )


def _freeze_json(value: object) -> FrozenJsonValue:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, list):
        return FrozenJsonArray(tuple(_freeze_json(item) for item in cast(list[object], value)))
    if isinstance(value, dict):
        if not all(isinstance(key, str) for key in value):
            raise RuntimeError("persisted artifact revision resource_ref has a non-string key")
        return _freeze_json_object(cast(dict[str, object], value))
    raise RuntimeError("persisted artifact revision resource_ref contains a non-JSON value")


def _duration_ms(run: EvalRun) -> int:
    duration: timedelta = run.completed_at - run.started_at
    return duration.days * 86_400_000 + duration.seconds * 1_000 + duration.microseconds // 1_000


__all__ = [
    "EvalRunComparisonFacade",
    "IncompatibleEvalRunsError",
    "UnknownEvalRunError",
]
