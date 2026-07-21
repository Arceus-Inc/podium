# ARCEUS live-run audit — 2026-07-19

A full company ("Lumen") was founded, approved, and delegated **through the browser cockpit**
against the live stack (Postgres 55444, embedded conductor MAX_TICKS=0, `gpt-5.2`). Every layer
was observed from the engine ledger + worktrees as it ran. This is the audit + fix plan so ARCEUS
runs like a real autonomous company rather than a scripted demo.

Company: `019f79d0-3f1a-797b-b14e-25911fb4751a` · workspace `019f79d0-3f19-…`

---

## Implementation status (branch `fix/live-run-audit-waves` in each repo)

| # | Fix | Status | Where |
|---|-----|--------|-------|
| **F1** | DoD by deliverable-kind, not role | ✅ done + tests | chorus `outcomes/_deliverable.py`, `_execution_profile.py` |
| **F2** | Drive horizon's decision→goal→report loop (human-gated) | ✅ done + tests | podium `_chorus_executor.py`, horizon `facade.adopt_goal` |
| **F3** | Distilled episodic body (was 180k-char transcript) | ✅ done + tests | chorus `memory/episodic/narrative.py` |
| **F4** | Operator-tunable lattice consolidation gate (warm-start) | ✅ done + tests | chorus `_lattice_bridge.py`; live env in `cockpit-dev.sh` |
| **F5** | Decompose beat records `delegated`, not `needs_changes/0.0` | ✅ done + tests | chorus `_scheduler.py` |
| **F7** | Stamp artifact `review_state` from the DoD verdict | ✅ done | chorus `_scheduler.py` |
| **F8** | Work lanes show gated-todo + blocked tasks | ✅ done + tests | podium `control/_allocation.py` |
| **F9** | Operator-configurable per-model pricing | ✅ done + tests | chorus `chorus_cli/_beats.py` |
| **F6** | Sprint-exhaustion escalation | ↩︎ reassessed — **already handled** | see below |
| **F10** | Company semantics glossary | ⏳ designed (focused follow-on) | see below |

**F6 reassessment.** The live symptom (a hard beat exhausting its 2 within-beat *sprints* at
needs-changes) is already caught by the outer **repair ladder** (`_climb_repair_ladder`): a failed
beat gets bounded re-wakes (`max_repair_attempts`), then parks `BLOCKED` and opens a
`recovery_action` for a human; a delegated child's block routes to its manager's integrate beat; and
the diagnostic already carries forward (horizon's `StrategyRecord.last_diagnostic` + chorus's
"corrective children inherit failure evidence", and the persistent worktree keeps prior eval JSONs
readable by the retry). Adding more here would be code for a solved problem — the audit over-rated
it. No change made.

**F10 design (focused follow-on, not rushed into a working system).** A company glossary
(term → definition → measurement rule → owner), RLS-scoped in the chorus ledger (a `glossary` table
via a chorus DDL delta — chorus owns engine DDL), a `glossary_define`/`glossary_read` tool pair, and
injection of the active glossary into (a) horizon's decompose context so the reasoner picks defined
metrics instead of inventing freeform `metric`/`target` strings, and (b) the delegation/verifier
briefs so "did we hit the target" resolves against the company's definition. This is a genuine new
subsystem (store + migration + tool + two injection points + tests); it is scoped here for its own
focused change rather than half-built under time pressure.

---

## What actually works (validated live — do not "fix")

The spine is real. Observed, not asserted:

1. **Formation → human approval → materialize.** Casey (CEO) beat, proposed a non-flat org
   (Avery PM-lead → Riley designer, Jordan frontend, Morgan QA/analyst), budgets bounded,
   confidence 0.8. Approved in the browser → 5 employees + management grants materialized, all
   plan-refs resolved correctly.
2. **T3 delegation.** Avery called `team_read`, decomposed **exactly once** into 3 IC tasks with a
   sensible dependency chain (**design → build → test → integrate**), created a `delegation_contract`
   carrying the rubric, went `blocked` awaiting children. No self-implementation.
3. **Dependency re-dispatch.** Each IC wake was consumed on block and correctly **re-woke on
   `deps_resolved`** — designer→done auto-woke fe→done auto-woke qa. The consume-then-rewake
   mechanism holds.
4. **Real deliverables.** Riley produced a genuine `DESIGN.md` calm design system (WCAG 2.1 AA,
   CSS-var tokens, 3 explored variants). Jordan produced a React+Vite markdown editor with live
   preview + localStorage autosave. Morgan produced Playwright specs + `test_evidence/`.
5. **Self-verification is calibrated for clean leaf work.** Designer passed on sprint-1
   (`outcome=pass score=1.0`; command DoD `passed`). Not universally harsh.
6. **Cost + spend tracked** per beat (flat rate), live on the dashboard; SSE spine, Work/Org/
   Delegation/Direction panels all update live.

The engine is sound. The findings below are what stops it being a *company* rather than a
*pipeline*.

---

## Findings (severity-ranked, each with live evidence → root cause → fix)

### F1 · DoD is keyed to the worker's ROLE, not the work order  — **CRITICAL**
**Evidence.** Morgan (profession `analyst`) was assigned *"Add Playwright e2e tests… ensure
evidence artifacts are captured and committed."* She correctly produced `playwright.config.ts`,
`e2e/*.spec.ts`, `test_evidence/{unit,e2e,summary}`. Her generated DoD rubric:
> "You are judging a FINISHED artifact: the file `findings.md` produced by an analyst… PASS it
> when `findings.md` is present…"

No `findings.md` (a testing task never makes one) → **FAIL → task `rejected`.** The in-beat
generator had already self-evaluated the work `pass score=1.0`; the role-derived DoD gate
overrode it and rejected correct work.

**Root cause.** DoD generation is role-templated, not task-aware:
- `frontend_engineer/_dod.py` and `designer/_dod.py` literally `del intent` — the acceptance
  check ignores the task and is fixed per role.
- `analyst/_dod.py` classifies only among *analyst* action classes (recommend/predict/investigate);
  there is no "testing" class, so a test task falls through to the findings.md rubric.
- `backend_engineer/_dod.py` branches 2 ways (report vs PR) — the most task-aware, still narrow.

Acceptance criteria are bolted to the assignee's job title. Fine when everyone does their canonical
job; it **guarantees rejection the moment a lead cross-assigns** — which is what real companies do
constantly. This is the #1 blocker to "runs like a real company" and directly underlies the
in-flight `videocursor: cross-profession delegation` goal.

**Fix.** Derive the DoD from the **deliverable the task asks for**, not the assignee's role:
1. Short term: the delegating **lead attaches a per-child DoD** when it `decompose`s (it already
   authors the intent; have it also emit `{deliverable_kind, checkable}` per child). The lead knows
   it assigned "tests," so it sets a test-shaped DoD.
2. Structural: replace per-role `*_dod.py` templates with one **task-type classifier**
   (`code | tests | design | doc | analysis | report`) → verifier, independent of role. Role only
   supplies defaults when the task type is ambiguous.
3. Guardrail: when the in-beat evaluator says `pass` but the DoD gate says `fail`, **surface the
   contradiction to the lead** (a review escalation) instead of silently rejecting — a human
   manager reconciles; today the gate wins unseen.

### F2 · Horizon (the "decides what's next" brain) is wired but never driven — **HIGH**
**Evidence.** `direction-report.md` stayed empty ("no decisions, no outcomes") through a full
formation+delegation. In `_chorus_executor.py` the conductor only calls `graph.horizon.start()`
and `graph.horizon.report()`. Nothing calls `seed_decision` / `decompose` / `submit_decision` /
`generate`. (The `decompose` in chorus is the *lead's* delegation tool, unrelated to horizon.)

**Root cause.** The reasoner activation correctly *assembled* horizon, but no live caller drives
its Decision→Goal→Outcome loop. Direction is supplied by the human/operator roadmap, so the
autonomous "what's next" engine is inert and its ~1.2k lines produce nothing at runtime.

**Fix.** Close the loop: after a delivery outcome lands, drive
`horizon.note_outcome` → `horizon.generate/decompose` → `submit_decision` so the company proposes
its **own** next goal (human-gated via the existing proposal door) instead of waiting for an
operator roadmap. Until then, "autonomous company" = "human feeds the queue."

### F3 · Episodic memory stores the full raw transcript (180k–190k chars/beat) — **HIGH**
**Evidence.** `episodic_record.body` lengths: casey 191,686 · casey 181,089 · pm 73,527 chars.
The record is the entire beat conversation, not a distilled lesson.

**Root cause.** The episodic writer persists the raw transcript. Recalling even one record would
blow the context window, so recall is effectively unusable and the learning loop's raw material
is noise-heavy.

**Fix.** Persist a **distilled episode** (intent, outcome, key decisions, files touched, 1–2
lessons) — a few hundred chars — and keep the raw transcript only in the run log (already durable).
The distillation can ride the beat-end the harness already runs.

### F4 · The compounding-learning loop cannot fire in a single build — **HIGH**
**Evidence.** 0 skills, 0 semantic atoms, empty `lattice/` after 6 beats. Gate is
`min_cluster=2` **similar episodes per employee**; per-employee counts were casey 2 (dissimilar),
everyone else 1. No gate opened.

**Root cause.** Consolidation is per-employee and needs ≥2 similar beats; a one-shot company build
gives each IC exactly one beat, so learning is structurally impossible until sustained multi-goal
operation. Consolidation is also opt-in (a later beat must call `lattice_apply`), so even an open
gate doesn't guarantee a skill.

**Fix.** (a) Lower/'warm-start' the gate for a company's first N goals, or seed cross-employee
clustering (a "testing" lesson from Morgan should be available to the next tester regardless of who
they are). (b) Make consolidation an **automatic beat-end step** when the gate is open, not a tool
the model must remember to call. This is the Atlan "compounding context" crown jewel — today it is
dark in any run shorter than a full roadmap.

### F5 · Successful decomposition/parent beats record as `needs_changes score=0.0` — **MEDIUM**
**Evidence.** The PM decomposed perfectly and is correctly `blocked` on children, yet its episodic
outcome is `needs_changes score=0.0`. Same shape on CEO formation (0.6 → still needs-changes,
sprint budget exhausted; only human approval landed it).

**Root cause.** A beat that *correctly* hands off (decompose, or "await subtree") has no "done"
verdict of its own, so it records as failing. Harmless today because F2 keeps horizon out of the
loop — but the moment F2 is fixed, horizon's outcome-feedback folds these 0.0s and marks healthy
goals as blocked/failing. It also poisons the episodic learning signal (F3/F4).

**Fix.** Give orchestration beats their own terminal verdict — `delegated` / `awaiting_subtree` =
success, distinct from `needs_changes`. Score the manager on *whether it decomposed well*, not on a
missing leaf artifact.

### F6 · Hard tasks exhaust the 2-sprint budget without self-correcting — **MEDIUM**
**Evidence.** CEO formation ran sprint-1 + sprint-2, both `needs-changes` (0.6 at sprint-2 on a
subtle non-flat `max_team_size` rule), never reached done; human approval masked it. An IC with no
human gate in the same position would be rejected.

**Fix.** Either raise the sprint budget for high-criteria tasks, or on final-sprint `needs-changes`
**escalate to the lead** with the specific unmet items (the evaluator already produces them) rather
than terminating at fail. Bounded retries → escalation, not silent failure.

### F7 · Artifacts land unreviewed; one had a null resource pointer — **MEDIUM**
**Evidence.** Both artifacts had `review_state=null`; one `doc` artifact had `resource_ref=null`
(no file pointer). The verification verdict the beat produced is never written back onto the
artifact.

**Fix.** On beat finalize, stamp the artifact's `review_state` from the DoD verdict and require a
non-null `resource_ref`. Otherwise the artifacts index can't distinguish verified from unreviewed
work.

### F8 · Work UI hides dependency-gated and blocked tasks — **MEDIUM (observability)**
**Evidence.** With 1 running + 2 dependency-gated `todo` + 1 `blocked` parent, the Work panel
showed QUEUED·0 / BLOCKED·0 — pending and blocked work was invisible to the operator.

**Fix.** Count dependency-gated `todo` in QUEUED (or a "waiting on deps" lane) and `blocked` tasks
in BLOCKED. An operator must see the whole board.

### F9 · Cost is flat-rated; dream's per-call `cost_usd` is always 0 — **LOW/cosmetic**
Spend *is* tracked (flat `CHORUS_PRICE_*` default), so the dashboard is not lying — but every model
prices identically and per-call attribution is lost. Add per-model rates when cost attribution
matters.

### F10 · Missing semantics layer (feeds F1/F6 uncertainty) — **BACKLOG**
The formation evaluator couldn't verify profession names because the catalog tool output was
summarized ("did not list names"). No shared company glossary of terms/metrics/entities exists;
goal `metric`/`target` are freeform strings. A semantics layer (the Atlan "what is ARR / a
qualified lead" gap) would remove a class of forced-uncertainty verdicts.

---

## Fix waves (recommended order)

**Wave A — make cross-functional work land (unblocks "runs like a company"):**
F1 (task-typed DoD + pass/fail-contradiction escalation), F5 (orchestration verdicts), F7
(stamp review_state). These three are why correct work gets rejected today.

**Wave B — turn on the brain + the memory:**
F2 (drive horizon's loop, human-gated), F3 (distilled episodes), F4 (auto beat-end consolidation +
warm-start gate). These make the company self-direct and actually learn.

**Wave C — resilience + visibility:**
F6 (escalate on sprint exhaustion), F8 (Work lanes), F9 (per-model cost), F10 (semantics layer).

Wave A is the smallest change with the largest "acts like a real company" payoff: a QA engineer's
tests should pass because the tests exist and run — not fail because they aren't a `findings.md`.
