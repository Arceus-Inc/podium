# Integration / wiring scan — where the context flow leaks

*2026-07-18 · offline theoretical scan · status: HYPOTHESES to validate against a live run (nothing fixed).*

**Method.** Read the composition root (`podium/src/podium/conductor/company/_build.py`, `_bridge.py`, `tests/test_company_wiring.py`) and then four parallel read-only deep-dives — one per engine (dream, chorus, horizon, lattice) — tracing the actual context flow along the intended loop:

> founder objective → **direction** (horizon) → **work** (chorus) → **beat** (dream) → **outcome/evidence** → **learning** (lattice) → back into direction & the next beat.

Each finding below is an **evidence-backed prediction** of where the whole will feel "not smart" even though each part is high quality. File:line pointers are from the scan and should be re-checked when we instrument the live run.

---

## Thesis (one paragraph)

The system is a **high-quality executor bolted to a disconnected brain and an open memory loop, running each beat myopically.** Concretely: horizon's LLM reasoning is switched **off** (`reasoner=None`), so there is no strategic cognition — the "company" only executes work that was seeded and re-prioritizes it. The learning loop (lattice) is **open** — episodes are captured but consolidation is advisory, the evolved-skills overlay is disabled, and no learning ever reaches strategy. And at the point of work, a beat is handed **only its own narrow intent + DoD** — not the parent goal's narrative, not prior episodes, not learned skills — so every beat, and every delegated child, works context-blind. The seams are individually clean; the *context does not flow through them*.

---

## UPDATE — 2026-07-18 (post-pull of the cofounder's changes)

Pulled podium `origin/main` df7090b..b72bf50 (8 commits). Two directly touch this scan, and they change the status of findings #1 and #6. **Everything else below is unchanged** (those fixes are engine-side — chorus/dream/horizon/lattice — and these commits were podium-only; re-run this scan against the engine repos once he pushes there).

- **#1 reasoner — NOW WIRED, but powered ≠ invoked.** Commit `48c0e7e "activate horizon's direction engine — wire the chat substrate as its reasoner"` replaces `reasoner=None` with a real `OpenAIChatSubstrate(name="horizon-reasoner", api_key, model=config.deployment, base_url)` and passes `model=config.deployment` (`_build.py`). So Scout/Analyst/decompose/generate no longer raise `HorizonError` — the capability is live. **BUT the sharper half of #1 remains open:** grep-confirmed, **nothing in podium ever calls `horizon.start()`, `decompose()`, `generate()`, `scout()`, `recover()`, or `sweep()`** — the conductor (`_chorus_executor.py`) starts `graph.org.start()` (chorus heartbeat) but not horizon, and only reads `horizon.report()` at run-end. Consequences to verify live:
  - horizon's `OutcomeListener` is never `start()`ed → the outcome→score/health→priority **feedback loop is not subscribed in the live runtime** (this was true even at `reasoner=None`).
  - the now-powered `decompose/generate/scout` are **never triggered autonomously** — direction still originates only from the **CEO employee's beats via `HorizonGovernance`** (`approve_proposal → seed_decision + author_goals + submit`) or an operator through the control-plane doors.
  - So: activating the reasoner is necessary but not sufficient — the loop still doesn't *ask the brain to think*, nor start its feedback listener. **New watch item:** does horizon do anything in a live run beyond emitting a (possibly empty) `report()`?
  - Side effect: pyproject moved `dream → dream[openai]` and `_build.py` now imports `dream.api.openai.OpenAIChatSubstrate`. **That module is therefore a real product dependency now** — it must be removed from the massive-review's "`api/` substrate serves only the repl" delete list.

- **#6 goal health isolation — PARTIALLY addressed.** Commit `2272ce6 "land horizon's loop report behind the cockpit — LoopReporter's consumer"` makes the conductor write `Horizon.report()` (decomposition → intake → feedback → current direction) to `<workdir>/<company>/direction-report.md` and serves it at `GET /cockpit/direction-report` (`_chorus_executor.py:write_direction_report`, `cockpit/router.py`). So horizon's own direction *narrative* is now surfaced (degrade-don't-block: empty markdown if no report). **Still open:** the underlying `score/health/metric/target` are still not written to chorus's goal row — the cockpit report is horizon's private view, and (per #1b) it will be near-empty until horizon is actually started/invoked.

- **Wave-1 cleanup (unrelated to context flow, matches the massive-review):** `89b963b/2e3b408/cf84030` delete the dead per-company `_jwt.py` + `jwt_master_secret` + pyjwt dep, trim dead re-exports, and merge cockpit `api()/api2()`. No wiring impact.

---

## The gaps, ranked by how much they'd dumb-down the whole

*(Ranking as originally written; see the UPDATE above for #1 and #6 status changes.)*

### 1. The strategic brain is OFF — `reasoner=None` (horizon) — **biggest** — ⚠️ NOW: reasoner wired (`48c0e7e`), but horizon still never started/invoked in the conductor (see UPDATE)
Horizon is constructed with no LLM reasoner (`_build.py` `reasoner=None`). With that, horizon's entire generative pipeline is inert:

- Goal **decomposition** disabled — `facade.py:135` raises `HorizonError("cannot decompose")`; `_decomposer = ... if reasoner is not None else None` (`facade.py:100`).
- **Opportunity scouting** dark — `Scout(...) if reasoner else None` (`facade.py:133`).
- **Analyst briefs** dark — `Analyst(...) if reasoner else None` (`facade.py:117`).
- **Evidence → proposal** funnel disabled — `generate()` raises `HorizonError("cannot generate")` (`facade.py:188`).
- Main loop is a **pure relay**: `start()` only subscribes to the outcome feed (`_listener.py:60`); each tick folds an outcome into a score and syncs a task priority. No decompose, no generate, no re-plan.

**Prediction for the live run:** the company cannot turn a founder objective into an OKR tree or propose new work on its own — everything must be hand-seeded, and "strategy" is just priority arithmetic. This alone reads as "not smart."

### 2. The learning loop is OPEN — lattice never closes the circle
- **Consolidation trigger is ambiguous / advisory.** At beat-start the harness calls `lattice.adjudicate(employee.id)` (`chorus_harness/_factory.py:~681`) and injects a "consolidate first" teaser directive (`lattice/directive.py`), but the durable consolidation (`lattice.apply()`) has **no production caller outside tests**, and the teaser is a prompt the agent may ignore. *(This is the sharpest disagreement between the two traces — resolve it live: do semantic atoms actually get produced during a run?)*
- **Back-edge to dream is opt-in.** `lattice_context` is a *tool* the agent must choose to call (`chorus_tools/_lattice.py`); nothing pre-injects learnings into the beat.
- **Back-edge to horizon is missing entirely.** Horizon has zero references to lattice/learnings; strategy is fully decoupled from what the company learned.
- **Evolved-skills overlay is disabled in production** — `enable_patches=False` hardcoded (`chorus_tools/_lattice_bridge.py:56`); `overlay_skills.py` is marked "legacy/test path." Agents always run base skills; evolved procedures never materialize.

**Prediction:** the company does not get smarter over time — memory is largely write-only, and strategy never benefits from it.

### 3. Context is not injected at the moment of use — per-beat myopia (dream + chorus agree)
A beat's context is: `task.intent` + verification/DoD + role brief + team roster (`chorus/heartbeat/_scheduler.py:704-712`, `chorus_harness/_factory.py:638-693`). What is **not** injected:

- the **parent goal / OKR narrative** (only `goal_id` is carried structurally),
- the **parent's acceptance criteria**,
- **prior episodic memory** (available only via a `recall` tool the model must call — `_factory.py:751`),
- **lattice semantic atoms / procedural skills** (only via opt-in tools).

Episodic capture is **write-only by default** — `_capture_memory()` runs *after* the beat (`_scheduler.py:827-849`); nothing reads it back in at assembly.

**Prediction:** every beat behaves like it's the first one — no accumulated knowledge, no memory of prior attempts unless the model volunteers a tool call.

### 4. Delegation handoff is lossy — children are narratively orphaned
On decompose, a child inherits `goal_id`, `parent_id`, `depth`, `depends_on` (`chorus/.../_decompose.py:113-115`) but **not** the parent's brief, DoD, or intent narrative — only its own narrow decomposed `intent`. The child's beat sees no pointer to the parent brief.

**Prediction:** multi-employee / team work degrades — sub-agents work with *less* context than the lead had, losing the "why" and the acceptance bar. (STATE.md's "smoother delegation / verbatim-module briefs" work is chasing exactly this.)

### 5. The outcome → strategy feedback signal is degraded and lossy
- After the **system-verifier removal**, `RUN_EVALUATED` is still emitted but carries the *evaluator's dict*, and the **final pass/fail verdict is written to the ledger, not emitted as an event** (`chorus/.../_observer.py:26`, `_scheduler.py:636`). Horizon consumes events, so it may receive a degraded/absent pass|fail.
- The bridge **drops `needs-changes` entirely** — `passed` stays `None` and horizon just increments a `deferred` counter (`_bridge.py` `_translate`, `horizon/.../_listener.py:82-87`).
- **No auto-recovery** — `horizon.recover()` exists but nothing wires the bridge to call it on `needs-changes`.

**Prediction:** even the little strategy that exists is underfed — it reacts to a partial signal and never to "needs-changes."

### 6. Strategy health/score is isolated from the rest of the system — ⚠️ NOW: `horizon.report()` surfaced at `GET /cockpit/direction-report` (`2272ce6`); goal-row health still isolated (see UPDATE)
Horizon computes goal `score/health/metric/target` (`feedback/_health.py`, `feedback/_listener.py:70`) but persists them to its **own** `strategy.json` (`store/_strategy_store.py`); they are **never written to chorus's goal row** and are exposed only read-only through the governance port to the CEO (`governance.py:39`).

**Prediction:** anything reading chorus goals (the cockpit, and the productized UX's "Direction / goal health") won't see health unless it goes through the governance port. Goal health will look empty/stale.

### 7. The org-validation / hooks layer is thin (in-flight)
There is no general pluggable hooks module yet — only hardcoded gates (`_invokability.py`) + injected budget enforcer + role DoD. STATE.md's build queue (H2 pre-dispatch validation, H3 routine stop-gate, H4 budget-ceiling auto-pause, H5 onboarding beat, H6 typed human interactions) is exactly this layer being built.

**Prediction:** bad tasks/briefs aren't caught before dispatch; budget/looping guards are partial.

### 8. Tool-schema noise — every phase sees every tool
All tool schemas are advertised to every phase (`dream/_factory.py:548-567`), so the **planner and evaluator receive irrelevant tool schemas** (shell, file-edit) — token noise and false "I could use this" signals during planning. (Confirms the massive-review's "toolless planner noise was never a model problem.")

---

## Open questions to resolve in the live run (the ambiguities)
1. **Does lattice consolidation actually produce semantic atoms during a run**, or only write teaser files? (adjudicate-auto vs apply-advisory conflict.) → instrument the semantic store; watch for atoms after ≥5 beats/cluster.
2. **What does `RUN_EVALUATED` actually carry to horizon** — the final verdict or just the evaluator dict? → log the OutcomeEvent horizon receives; check `passed`.
3. **Do agents ever call `recall` / `lattice_context` on their own?** → count tool calls per beat; if ~0, the opt-in edges are effectively dead.
4. **Is any goal health/score visible outside horizon's json** during a run? → read chorus goals + governance port and compare.

## How to compare (what to watch, per hypothesis)
- **#1** seed only a founder objective, no tasks → does anything get proposed/decomposed? (expect: no.)
- **#2** run ≥5 beats → do semantic atoms appear? do later beats reference them? does horizon change behavior from a learning? (expect: no on the last two.)
- **#3/#4** inspect the actual prompt a child beat receives → is the parent goal/brief/DoD in it? (expect: no.)
- **#5** force a `needs-changes` outcome → does horizon react at all? (expect: deferred counter only.)
- **#6** compare chorus goal rows vs horizon strategy.json for the same goal → health only in the json. (expect: yes.)

---

*Companion to `2026-07-18-massive-review.md` (that one is code-health; this one is context-flow/wiring). All read-only — no changes made.*
