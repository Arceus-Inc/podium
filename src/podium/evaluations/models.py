"""Immutable domain state for an ordered eval-run comparison."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import TypeAlias

JsonScalar: TypeAlias = str | int | float | bool | None


@dataclass(frozen=True)
class FrozenJsonEntry:
    key: str
    value: FrozenJsonValue


@dataclass(frozen=True)
class FrozenJsonObject:
    entries: tuple[FrozenJsonEntry, ...]


@dataclass(frozen=True)
class FrozenJsonArray:
    items: tuple[FrozenJsonValue, ...]


FrozenJsonValue: TypeAlias = JsonScalar | FrozenJsonObject | FrozenJsonArray


@dataclass(frozen=True)
class AgentConfigSkillPinInspection:
    skill_revision_id: str


@dataclass(frozen=True)
class AgentConfigToolPinInspection:
    identifier: str
    provenance: str


@dataclass(frozen=True)
class AgentConfigRevisionInspection:
    id: str
    agent_id: str
    revision_no: int
    agents_md_revision: str
    agents_md_content: str
    provider: str
    model: str
    sandbox_profile: str
    skill_pins: tuple[AgentConfigSkillPinInspection, ...]
    tool_pins: tuple[AgentConfigToolPinInspection, ...]
    created_at: datetime


@dataclass(frozen=True)
class SkillRevisionInspection:
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


@dataclass(frozen=True)
class EvalCaseInspection:
    id: str
    skill_revision_id: str
    name: str
    input_text: str
    expected_behavior: str
    created_at: datetime


@dataclass(frozen=True)
class ArtifactRevisionInspection:
    id: str
    artifact_id: str
    revision: int
    resource_ref: FrozenJsonObject | None
    summary: str | None
    created_by_run_id: str | None
    created_at: datetime


@dataclass(frozen=True)
class EvalRunSnapshot:
    """The persisted fields that can be compared without interpreting agent prose."""

    id: str
    eval_suite_id: str
    eval_cases: tuple[EvalCaseInspection, ...]
    skill_revision: SkillRevisionInspection
    agent_config_revision: AgentConfigRevisionInspection
    provider: str
    model: str
    input_snapshot: str
    output_snapshot: str
    input_tokens: int
    output_tokens: int
    cost_usd: Decimal
    status: str
    artifact_revisions: tuple[ArtifactRevisionInspection, ...]
    created_at: datetime
    started_at: datetime
    completed_at: datetime


@dataclass(frozen=True)
class EvalRunDeltas:
    """Candidate-minus-baseline deltas for persisted numeric measurements only."""

    input_tokens: int
    output_tokens: int
    cost_usd: Decimal
    duration_ms: int


@dataclass(frozen=True)
class EvalRunComparison:
    """An ordered comparison of two runs pinned to the same eval suite."""

    baseline: EvalRunSnapshot
    candidate: EvalRunSnapshot
    deltas: EvalRunDeltas


__all__ = [
    "AgentConfigRevisionInspection",
    "AgentConfigSkillPinInspection",
    "AgentConfigToolPinInspection",
    "ArtifactRevisionInspection",
    "EvalCaseInspection",
    "EvalRunComparison",
    "EvalRunDeltas",
    "EvalRunSnapshot",
    "FrozenJsonArray",
    "FrozenJsonEntry",
    "FrozenJsonObject",
    "FrozenJsonValue",
    "JsonScalar",
    "SkillRevisionInspection",
]
