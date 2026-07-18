# Design: the Evidence Protocol — one owner for strict-TDD's contract

*2026-07-18, loop iteration 9. Motivation: the strict-TDD contract is currently an implicit
protocol smeared across five places — the gate (`chorus_harness/_tdd_gate.py`), the kernel's
evidence validator (`tdd_review_v1` parsing), the DoD rubric prose, the factory's
`subagent_evidence` map, and role briefs/tests — held together by repeated string literals
("test_author", "test_plan.json", "test_evidence/red.json", the pre-RED write prefixes).
Two implementations of "what counts as valid RED" exist (gate unlock vs kernel verify). That
is drift waiting to happen.*

## Principle

A verification protocol is DATA, not folklore. One typed object declares the artifacts, their
authors, their claims, and the validation; every consumer — gate, kernel, factory, rubric —
derives from it. Nothing else may name an evidence path or the author subagent.

## The shape (chorus/outcomes/_evidence_protocol.py)

    @dataclass(frozen=True)
    class EvidenceArtifact:
        path: str                       # worktree-relative, e.g. "test_evidence/red.json"
        required_claim: Mapping | None  # {"authored": True} / {"verdict": "red-confirmed"}
        author: EvidenceAuthor          # AUTHOR_SUBAGENT | KERNEL | REVIEWER_SUBAGENT
        read_only_after_write: bool

    @dataclass(frozen=True)
    class EvidenceProtocol:
        key: str                        # == the ReviewedBuildEvidenceProfile value
        author_subagent: str            # the ONE place "test_author" is spelled
        artifacts: tuple[EvidenceArtifact, ...]
        pre_red_write_allow: tuple[str, ...]

        def artifact(self, name: str) -> EvidenceArtifact
        def validate_red(self, worktree, *, changed_production) -> Verdict   # gate unlock
        def validate_full(self, worktree) -> Verdict                        # kernel verify

    TDD_REVIEW_V1 = EvidenceProtocol(key="tdd_review_v1", author_subagent="test_author", ...)
    def protocol_for(profile: ReviewedBuildEvidenceProfile) -> EvidenceProtocol

`validate_red` and `validate_full` are the SAME implementation the kernel and the gate both
call — one truth for hash pinning, claim checking, and production-path cleanliness.

## Consumers after the refactor

| Consumer | Before | After |
|---|---|---|
| TddProductionGate | module constants + own `_valid_red` | `TddProductionGate(worktree, protocol)`; unlock = `protocol.validate_red`; author check = `protocol.author_subagent` |
| Kernel reviewed-build verify | own parsing of the four files | `protocol_for(profile).validate_full(worktree)` |
| Factory `subagent_evidence` map | hand-built dict per role spec | derived from `protocol.artifacts` where `author == AUTHOR_SUBAGENT` |
| Role DoD rubric (`backend_engineer/_dod.py`) | prose repeats the file names | f-string interpolation of `protocol.artifact(...).path` — prose can't drift |
| Role manifests' subagent specs | name "test_author" by convention | validated at registration: a TDD_REVIEW_V1 role MUST declare a subagent named `protocol.author_subagent` (fail-closed, like routine validation) |
| Briefs | named the files (now already lean) | keep the pointer only; no paths |

## Boundaries

- Dream stays generic: the gate remains a chorus_harness wrapper over dream's registry; dream
  never learns TDD. If a second protocol appears (e.g. `design_review_v1`), it's a new
  EvidenceProtocol instance, zero new mechanism.
- No DDL, no behavior change intended: this is a refactor with a compatibility bar — every
  existing gate/kernel test must pass unchanged except where they pinned the duplicated
  constants (update those to import from the protocol).

## Test plan (TDD)

1. RED: protocol unit tests — artifact lookup, validate_red accepts the fixture bundle the
   existing gate tests build, rejects doctored hashes/dirty production (port the existing
   gate-test fixtures to protocol tests).
2. RED: registration fail-closed test — a plugin declaring TDD_REVIEW_V1 without a
   `test_author` subagent is rejected.
3. GREEN: implement protocol; rewire the five consumers; delete the duplicated constants.
4. Full suites: tests/harness, tests/heartbeat, tests/employee, tests/outcomes; ruff+mypy.
