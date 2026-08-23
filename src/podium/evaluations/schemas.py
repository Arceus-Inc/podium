"""HTTP serialization DTOs for eval-run comparisons."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Self

from pydantic import BaseModel, ConfigDict

from podium.evaluations.models import (
    ArtifactRevisionInspection,
    EvalRunComparison,
    EvalRunSnapshot,
    FrozenJsonArray,
    FrozenJsonObject,
    FrozenJsonValue,
)


class AgentConfigSkillPinOut(BaseModel):
    model_config = ConfigDict(from_attributes=True, frozen=True)

    skill_revision_id: str


class AgentConfigToolPinOut(BaseModel):
    model_config = ConfigDict(from_attributes=True, frozen=True)

    identifier: str
    provenance: str


class AgentConfigRevisionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True, frozen=True)

    id: str
    agent_id: str
    revision_no: int
    agents_md_revision: str
    agents_md_content: str
    provider: str
    model: str
    sandbox_profile: str
    skill_pins: tuple[AgentConfigSkillPinOut, ...]
    tool_pins: tuple[AgentConfigToolPinOut, ...]
    created_at: datetime


class SkillRevisionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True, frozen=True)

    id: str
    skill_id: str
    revision_no: int
    action: str
    file_inventory: str
    content_hash: str
    label: str | None
    source_run_ids: tuple[str, ...]
    author_run_id: str | None
    restored_from_revision_id: str | None
    created_at: datetime


class EvalCaseOut(BaseModel):
    model_config = ConfigDict(from_attributes=True, frozen=True)

    id: str
    skill_revision_id: str
    name: str
    input_text: str
    expected_behavior: str
    created_at: datetime


class ArtifactRevisionOut(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str
    artifact_id: str
    revision: int
    resource_ref: dict[str, object] | None
    summary: str | None
    created_by_run_id: str | None
    created_at: datetime

    @classmethod
    def from_domain(cls, revision: ArtifactRevisionInspection) -> Self:
        return cls(
            id=revision.id,
            artifact_id=revision.artifact_id,
            revision=revision.revision,
            resource_ref=(
                _thaw_json_object(revision.resource_ref)
                if revision.resource_ref is not None
                else None
            ),
            summary=revision.summary,
            created_by_run_id=revision.created_by_run_id,
            created_at=revision.created_at,
        )


class EvalRunSnapshotOut(BaseModel):
    """One immutable Chorus eval-run record at the HTTP boundary."""

    model_config = ConfigDict(from_attributes=True, frozen=True)

    id: str
    eval_suite_id: str
    eval_cases: tuple[EvalCaseOut, ...]
    skill_revision: SkillRevisionOut
    agent_config_revision: AgentConfigRevisionOut
    provider: str
    model: str
    input_snapshot: str
    output_snapshot: str
    input_tokens: int
    output_tokens: int
    cost_usd: Decimal
    status: str
    artifact_revisions: tuple[ArtifactRevisionOut, ...]
    created_at: datetime
    started_at: datetime
    completed_at: datetime

    @classmethod
    def from_domain(cls, snapshot: EvalRunSnapshot) -> Self:
        return cls(
            id=snapshot.id,
            eval_suite_id=snapshot.eval_suite_id,
            eval_cases=tuple(EvalCaseOut.model_validate(case) for case in snapshot.eval_cases),
            skill_revision=SkillRevisionOut.model_validate(snapshot.skill_revision),
            agent_config_revision=AgentConfigRevisionOut.model_validate(
                snapshot.agent_config_revision
            ),
            provider=snapshot.provider,
            model=snapshot.model,
            input_snapshot=snapshot.input_snapshot,
            output_snapshot=snapshot.output_snapshot,
            input_tokens=snapshot.input_tokens,
            output_tokens=snapshot.output_tokens,
            cost_usd=snapshot.cost_usd,
            status=snapshot.status,
            artifact_revisions=tuple(
                ArtifactRevisionOut.from_domain(revision)
                for revision in snapshot.artifact_revisions
            ),
            created_at=snapshot.created_at,
            started_at=snapshot.started_at,
            completed_at=snapshot.completed_at,
        )


class EvalRunDeltasOut(BaseModel):
    """Persisted numeric candidate-minus-baseline deltas."""

    model_config = ConfigDict(from_attributes=True, frozen=True)

    input_tokens: int
    output_tokens: int
    cost_usd: Decimal
    duration_ms: int


class EvalRunComparisonOut(BaseModel):
    """One compatible pair of pinned evaluation runs."""

    model_config = ConfigDict(from_attributes=True, frozen=True)

    baseline: EvalRunSnapshotOut
    candidate: EvalRunSnapshotOut
    deltas: EvalRunDeltasOut

    @classmethod
    def from_domain(cls, comparison: EvalRunComparison) -> Self:
        return cls(
            baseline=EvalRunSnapshotOut.from_domain(comparison.baseline),
            candidate=EvalRunSnapshotOut.from_domain(comparison.candidate),
            deltas=EvalRunDeltasOut.model_validate(comparison.deltas),
        )


def _thaw_json_object(value: FrozenJsonObject) -> dict[str, object]:
    return {entry.key: _thaw_json(entry.value) for entry in value.entries}


def _thaw_json(value: FrozenJsonValue) -> object:
    if isinstance(value, FrozenJsonObject):
        return _thaw_json_object(value)
    if isinstance(value, FrozenJsonArray):
        return [_thaw_json(item) for item in value.items]
    return value


__all__ = [
    "AgentConfigRevisionOut",
    "AgentConfigSkillPinOut",
    "AgentConfigToolPinOut",
    "ArtifactRevisionOut",
    "EvalCaseOut",
    "EvalRunComparisonOut",
    "EvalRunDeltasOut",
    "EvalRunSnapshotOut",
    "SkillRevisionOut",
]
