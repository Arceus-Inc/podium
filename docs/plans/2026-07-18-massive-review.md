# The Massive Review — five repos, six lenses, one fix plan

*2026-07-18. Method: six parallel review agents, each through a skill lens (ponytail-audit ×2,
agent-harness-construction, backend/postgres/security, python-review, external-codebase
research over openai/codex, NousResearch/hermes-agent, OpenHands, Aider, Claude Code docs).
Full per-lens reports live in the session scratchpad `review/` folder; this document is the
consolidation. Testing is PAUSED by operator order — the plan below sequences cleanup and
correctness first, testing resumes at Wave 4.*

## 1. Executive verdict

The system's foundations are healthy where it matters — RLS discipline is clean, SQL is
parameterized, the beat loop is bounded everywhere, refusal messages teach recovery, and the
small repos (lattice, podium) are genuinely lean. The debt is concentrated and nameable:

- **~15,000 deletable lines** across the five repos, dominated by two clusters: dream's
  never-used runtime/repl/api substrate (~10.3k) and chorus's now-dead reviewer/verifier stack
  (~2.25k + ~840 test lines) orphaned by the self-verification decision.
- **~5,800 lines of FINISHED subsystems never switched on** — dream's always-on runtime,
  horizon's entire LLM direction engine (podium passes `reasoner=None`), lattice's skill-patch
  overlay. These need activate-or-delete decisions, not more code.
- **A dozen real correctness bugs** found by reading, not testing: a cross-company authz hole,
  three torn-transaction windows, a duplicate-runtime race, a cancel wedge, a reconnect-less
  event listener.
- **Two harness-level root causes** that explain most live beat failures we witnessed: the
  wire protocol advertises every tool schema to every phase (the "toolless planner noise" was
  never a model problem), and — after the verifier removal — the sole judge has no evidence
  schema and a pass-biased prompt.

## 2. The bloat ledger (delete list, ranked)

| Repo | Cut | Lines |
|---|---|---|
| dream | runtime/wake/coordination/state/channels/daemon/ctl stack (zero product callers) — OR activate (see §5) | ~4,377 |
| dream | repl/ (duplicated by chorus_cli; keep _ansi.py) | ~2,632 |
| dream | api/ substrate/failover/credentials (serves only the repl) | ~1,078 |
| dream | swarm runtime-only machinery (product uses TeammateSpawnConfig + _handoff only) | ~1,650 |
| dream | config from_file/from_env/from_dict Settings layer (zero consumers) + dead services + stubs | ~500 |
| chorus | dead reviewer/verifier stack: scheduler review machinery (~490), _tdd_gate.py (215), reviewer role (183), SubmitVerdictTool + record_verdict (165), ReviewedBuild types (90), factory verifier surface (70), verification.py + dead errors (44), contracts/ (68) | ~1,325 |
| chorus | dead reviewer-path tests | ~840 |
| chorus | chorus_tools/_registry.py (headline function, zero callers) + 54 dead __all__ re-exports + _rejected/write_json/asdict dedup | ~210 |
| podium | dead auth/_jwt.py + jwt settings, dead re-exports, api()/api2() merge | ~90 |
| lattice | lattice_cli (≡ cat cursor.json), dead errors.py, directive triple-indirection, 3 single-impl Protocols | ~170 |
| horizon | LoopReporter (test-only), dead _jsonio branch, redundant example + chorus dev-dep | ~130 |
| **Total safe now** | | **~13,000** |

Verified live and NOT cut: TestEvidence/TestRed/EvidenceScan tools (self-verification path),
cms/delivery backends (real 2-impl seams), swarm web_research_orchestrator, _http.py.

## 3. Correctness bugs (fix regardless of any design debate)

**Security/authz**
- P-H1 podium: runs cancel/logs + events doors authorize with no company on the Resource — a
  company-scoped key can cancel runs and read event transcripts of sibling companies in the
  same workspace. Fix: fetch run → decide() against run.company_id.
- P-H2 podium: nothing asserts the API runs as `podium_app` — a DSN misconfig silently
  bypasses FORCE RLS. Fix: lifespan assertion (not superuser, no BYPASSRLS).

**Concurrency/lifecycle**
- P-H3: CompanyGraphHost.ensure() check-then-act race → duplicate runtimes (two heartbeats,
  seq PK collisions). Fix: per-company asyncio lock.
- P-H5: canceling a QUEUED run wedges in `canceling` forever. Fix: queued→canceled directly;
  reclaim covers lapsed CANCELING.
- P-H4: broadcaster's LISTEN connection never reconnects — one DB blip silently kills every
  SSE stream forever. Fix: supervised reconnect loop.
- C-3/C-4/C-5 chorus: three torn-transaction windows — monitor fire vs wake enqueue,
  fire_routine's edge-advance before its writes (a crash loses a cron window), facade.submit's
  four separate commits (crash yields a dispatchable task with no DoD). Fix: wrap each in
  ledger.transaction().
- C-6: timeout-vs-strand decided by `"TimeoutError" in str(outcome)` substring. Fix: typed
  fault field on BeatOutcome.
- C-9: tasks repo spells the terminal set inline ×5 and one site omits 'rejected' — a rejected
  routine-spawned task suppresses that routine forever via skip_if_active.
- P-H6: the cockpit 429s — one 100/min bucket shared by ~12 polled doors, Retry-After always
  60s. Fix: read/write buckets (600/min reads), true Retry-After, snapshot+SSE over polling.

## 4. Harness truths (dream + the self-verification consequence)

- D-1 **Wire schema never narrows per phase**: every session gets all ~26 tool schemas with
  tool_choice=auto; role allow-lists only refuse at dispatch. The toolless-planner noise,
  the PLANNER_TOOLLESS_NOTE, the "tool-not-in-role-manifest" churn — all one bug. Fix:
  intersect the wire `tools` with role_allowed; omit when empty. This is the single
  highest-leverage harness fix.
- D-2 **The sole judge has no evidence schema**: EvaluationRecord = outcome/score/notes; no
  per-criterion verdicts; rubric-only DoDs have no oracle; the evaluator prompt says not to
  withhold a pass for unrun commands. Fix: criteria:[{id, holds, evidence}] with resolvable
  citations required when no command ran.
- D-3 Evaluator can be refused the artifact it judges (read_offloaded missing from the
  read-only surface). One-line fix.
- D-4 **12-13 memory-ish tools across 5 systems per agent** (memory_*, working_memory_*,
  recall/get_run, lattice_*, todo_write) — the anti-pattern the harness skill names first;
  briefs compensate with directive blocks. Fix: one memory facade per concern tier.
- D-5 Prompt assembly: all three phase overlays carry the full generator brief + volatile
  per-beat data (inbox/roster) in the SYSTEM prompt across up to 8 LLM sessions per sprint —
  defeats prefix caching and hands wrong-phase guidance to the judge. Fix: tiered
  stable→role→volatile assembly (see §6 lesson 5).
- D-6 request_capability is advertised in refusals but isn't a callable tool — dead-end hint.
- D-7 The planner plans blind (toolless) and re-plans from scratch every beat.

## 5. Dormant-but-finished: activate or delete (the operator's call, per component)

| Component | Lines | Activate looks like | Delete if |
|---|---|---|---|
| horizon's LLM direction engine | ~1,170 | podium passes a real reasoner (Azure key exists); horizon starts proposing goal revisions off outcomes — the missing "direction" leg | goals stay human-seeded |
| dream runtime/wake stack | ~4,377 | replace chorus's hand-rolled loop pieces where they overlap | chorus remains the only loop owner (recommended) |
| lattice skill-patch overlay | ~150 | enable_patches=True → evolved skills actually patch role bundles | skills stay read-only |
| dream.hooks (beat-level) | wired, unused by product | chorus factory registers org-aware hooks (observability, evidence mirroring) | — (free to keep) |
| podium 5 uncalled doors (/overview /report /artifacts /export /capacity) | ~120 | cockpit renders them | UI never will |

## 6. External lessons (codex · hermes-agent · OpenHands · Aider · Claude Code)

1. **"Done" is a harness decision gated on evidence, never a model claim** — every system
   that works has a non-advisory finish gate (critic threshold, Stop hooks, proof ledger).
2. **Verify against real execution artifacts fed back raw, bounded retries, then escalate to
   a peer/manager** — Aider's 3-iteration loop over real tool output beats elaborate
   self-review; escalation is an org primitive.
3. **Review is a separate role with a rubric and machine-readable findings** — all five
   systems converge here. ⚠ Tension with our self-verification decision — see §7.
4. **Capability/policy enforcement lives in the engine on two axes (can-do sandbox vs
   ask-human approval), non-overridable from below; every widening is a logged escalation.**
5. **Prompt economy**: shrinking identities (hermes SOUL.md = 72 words), tiered cache-stable
   assembly, skills as one-line indexes with on-demand bodies, budgets spent on COMPUTED
   context (Aider's ranked repo-map) instead of longer instructions.

## 7. The one open design question, stated honestly

Operator removed all external verification; every studied system keeps a separate judge of
some kind. The reconciliation that fits both: keep ZERO second LLM beats, but make the finish
gate deterministic — the engine accepts an employee's "done" only when the beat's own
evidence exists (diff non-empty when code was asked, its recorded commands exited 0), and the
in-beat evaluator gets the D-2 evidence schema. That is self-verification with a spine, not
re-introduced review. Escalation on repeated failure goes to the MANAGER (an org peer), not a
system principal — matching lesson 2 and paperclip.

## 8. The fix plan (waves, in order)

- **Wave 1 — Delete (this week, no behavior change)**: §2 ledger. Chorus dead stack + tests;
  dream repl/api/config/swarm-extras; the small-repo trims. Gates: ruff/mypy + suites green.
- **Wave 2 — Correctness**: §3 in order P-H1, P-H2, C-3/4/5, P-H3, P-H5, P-H4, C-6, C-9, P-H6.
- **Wave 3 — Harness**: D-1 (wire narrowing), D-2 (+§7 evidence schema), D-3, D-6; then D-5
  prompt assembly and D-4 memory-facade consolidation.
- **Wave 4 — Testing resumes**: full suites + a fresh company e2e on the cleaned engine.
- **Wave 5 — Activation decisions**: §5 table, one component at a time, operator call first
  (horizon reasoner is the highest-value candidate).

*Net effect if all waves land: ~13k fewer lines, five repos that all do something, an engine
whose every live failure mode we witnessed this week has a named fix, and a self-verification
model with deterministic teeth.*
