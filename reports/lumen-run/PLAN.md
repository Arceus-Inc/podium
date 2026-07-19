# Lumen — Fix Plan for the Audit Findings

**Companion to:** `AUDIT.md`
**Repos in scope:** `chorus` (engine, governance, CEO/lead briefs), `podium` (the company operator)
**Goal:** make the company delegate, decompose, converge, close goals, and advance the roadmap — the way a real company does.

Work is grouped into 4 phases by **leverage**. Phase 0 (goal roll-up) unblocks the single biggest cascade; do it first.

> **Status (2026-07-19, handover to Divyansh):** Phase 0 ✅ done+validated · Phase 1 ✅ done+validated
> (cap-accept + feedback brief; a standalone re-decomposition guard 1.1 was not separately needed) ·
> Phase 2 ✅ done+validated (dedup/names/pull-growth/IC-under-CEO) · Phase 3 🟡 partial (report health
> panel done; CEO formation reliability + honest `status` lifecycle still open). Plus two operator
> additions beyond the original plan: **stranded-goal shelving** and **prose-goal reframe** (so
> written-deliverable goals target the roles' verifiable DoD artifacts). See `AUDIT.md` → *Handover status*
> for the per-item table, commits, and live-run evidence.

---

## Phase 0 — Close the loop: goal roll-up & completion  *(highest leverage)*

> Audit refs: **B1** (goals never complete), **D1** (never advances), **D2** (stalled).
> **Root cause (verified):** the engine has `goals.update(...)` (`chorus/src/chorus/ledger/repos/goals.py:53`) but **nothing in the scheduler or governance ever transitions a goal to `done`**. A delegation task can finish via the `children_done` → integrate path, but the goal it belongs to stays `active` forever, so the operator never queues the next roadmap item.

**0.1 — Engine-native goal completion.**
- When a goal's **root task** (the depth-0 delegation task, `parent_id is None`, `goal_id = G`) reaches `DONE`, transition goal `G` → `done`.
- Location: `chorus/src/chorus/heartbeat/_scheduler.py` in `_land_passed` (where a task lands terminal). After setting the task `DONE`, if it's a goal-root task, call `ledger.goals.update(...)` to `done` and emit a `goal.completed` activity.
- Guard: only the goal-root task (not every child) flips the goal.

**0.2 — Operator reacts to goal completion.**
- `podium/scripts/company_operator.py::goal_daemon` / `delegation_daemon`: treat goal `status='done'` (ledger) as the completion signal — not just the product-run status. On completion: mark the operator's SQLite row done, then **queue the next roadmap goal** and delegate it.
- Removes reliance on the flaky "delegation product-run reaches terminal" path.

**0.3 — Validation.**
- A fresh run must show at least one goal reach `done`, and the operator must queue goal #4 (Pomodoro) automatically.
- Query: `select title,status from goal where company_id=... ` shows a `done`; operator log shows "queued goal 'Pomodoro…'".

---

## Phase 1 — Converge, don't loop: decomposition & feedback

> Audit refs: **B2** (work redone 3×), **B3** (loop → cap → fail), **B4** (false "succeeded"), **C1** (needs-changes not actioned), **C3** (rejected work abandoned).

**1.1 — Re-decomposition guard (stop redoing done work).**
- **Root cause:** each delegation re-beat calls the decompose tool fresh and creates *new* equivalent children instead of continuing the existing subtree. Evidence: the notes app was implemented 3×.
- Fix: in the lead's decompose path (`chorus/src/chorus_tools/_decompose.py` + the integrate/re-invocation path in `_scheduler.py`), before creating children, **load existing children for the task**; if a matching child already exists (`done` or `in_progress`), do **not** recreate it. Re-invocation should *integrate* existing children, not re-fan-out.
- Reinforce in the lead/delegation brief: "You are continuing an existing sprint. Read the current subtasks and worktree first; extend them — never recreate work that is already `done`."

**1.2 — Actionable failure feedback into the next beat.**
- **Root cause:** on `needs-changes`/`rejected`, the next beat only gets a vague recall hint ("failed a check — avoid repeating that approach", `chorus/src/chorus_tools/_recall_render.py:17`). The evaluator's *specific* change list isn't threaded into the retry prompt.
- Fix: capture the DoD/evaluator verdict detail (the concrete failing checks) on the run outcome, and inject it into the next beat's context in `chorus/src/chorus/heartbeat/_beat_context.py` as an explicit **"Fix these specific items to pass:"** block — not just an "avoid" hint.
- Effect: a failed beat gives the next beat enough to actually pass (the user's core requirement).

**1.3 — Integrate-iteration cap: converge or accept, don't silently block.**
- **Root cause:** at the cap, a `DELEGATION` task is marked `FAILED`/`BLOCKED` (`_scheduler.py:~1224` `_accept_capped_subtree`), which is what killed the design system at `iteration=4, cap=3`.
- Fix options (do both):
  - Raise the effective cap for delegation when children are *making progress* (fewer blocked children each iteration).
  - At the cap, if all children are terminal and ≥1 produced an artifact, **accept** the subtree (mark the delegation `done`) instead of `blocked`, and record a "capped-accept" note — the roll-up (Phase 0) then closes the goal. Only hard-block when nothing converged.

**1.4 — Honest beat status.**
- **Root cause:** lead delegation beats are recorded `succeeded` while `steps_blocked:1 / sprint_outcomes:["fail"]`.
- Fix: a delegation beat is only `succeeded` when its landed children are actually `done`; otherwise `needs_changes`/`blocked`. Location: the delegation landing in `_scheduler.py`.

**Validation:** design-system goal converges to `done` within the cap; the notes app is implemented **once** (no duplicate "implement…" subtasks); a rejected subtask is retried with the specific fix list and passes.

---

## Phase 2 — Org integrity: dedup, names, pull-based growth

> Audit refs: **A1** (duplicate employees), **A2** (placeholder names), **A3** (stale/idle), **A4** (over-hiring push), **A5** (IC under CEO).

**2.1 — Identity dedup in governance.**
- **Root cause:** `chorus/src/chorus/governance/_workforce_plan.py::_validate` rejects duplicate **refs** (slugs) only (`~line 285`), so a re-hire with a *new* slug but the *same* `name + role + reports_to` is applied as a new row. Evidence: Avery/Jordan/Riley/Morgan each exist twice.
- Fix: in `_validate`, reject any proposed employee whose `(name, role, reports_to)` matches an **existing active** employee — that's a re-hire. Expansion must reference the existing `employee_id` to add capacity under a lead, not clone a person.
- Reinforce in the CEO expansion brief: "To add capacity, add **new distinct people** with **real names** under an existing lead; never re-list someone who already exists."

**2.2 — Require real person names; ban role-as-name.**
- Reject/placeholder-name hires ("Pod Lead (Delivery)", "QA/Test Analyst (Pod A)"). Governance validation: a hire's `name` must not equal its role/title; CEO brief must assign real given names.

**2.3 — Growth = pull, not push.**
- **Root cause:** `podium/scripts/company_operator.py::growth_daemon` fires on a computed "bottleneck" signal and keeps hiring even when there's no open work (no `staffing_request` was ever filed). Evidence: 6 expansion rounds, 11 idle hires.
- Fix:
  - Only expand when there is **real unstaffed queued work**: an open `staffing_request` from a lead, OR ≥1 active goal with no capable pod.
  - Stop hiring once active goals are staffed and headcount ≥ work; drop the pure "bottleneck per IC" push, or gate it behind "queued tasks exist AND no idle IC of that profession already exists."
  - Never hire while ≥N employees of the needed profession are idle (0 tasks/0 beats).

**2.4 — No IC under the CEO.**
- Governance: the CEO's only direct reports are leads (already in brief); add a hard validation that a non-lead's `reports_to` is never the CEO. Evidence: Quinn (pm) under CEO.

**Validation:** a fresh run ends with **no duplicate identities**, **no placeholder names**, **0 idle hires** beyond a small buffer, and every IC under a lead.

---

## Phase 3 — Reliability & observability

> Audit refs: **C2** (CEO formation fails 7×), **D3** (`status` meaningless), **D4** (empty `decision_record`).

**3.1 — CEO formation reliability.**
- **Symptom:** Casey's formation/executive beat is `steps_blocked:1` almost every time (7 fails before 1 pass; one `TimeoutError`). Because it never lands, **`decision_record` stays empty**.
- Action: instrument the formation beat's DoD to log *why* it blocks; fix the failing gate (likely the directive-file DoD or a governance read). Add a longer timeout / retry-with-context for the `TimeoutError` path.

**3.2 — Honest employee `status` lifecycle.**
- **Root cause:** every employee shows `idle` (created `EmployeeStatus.IDLE` in `_workforce_plan.py` and never updated).
- Fix: drive `status` from live state — `working` while a beat holds a lease, `blocked` when its tasks are blocked, `idle` when truly free, `terminated` when offboarded. Lets the report surface ghosts/stalls directly.

**3.3 — Report upgrades (fold findings back in).**
- Add to `build_lumen_report.py`: a **health panel** that flags duplicates, idle employees, goals-never-closed, redone tasks, and cap-failed delegations automatically — so each future run self-audits.

**Validation:** formation lands on the first or second beat; `decision_record` is non-empty; the report's health panel shows green.

---

## Sequencing & ownership

| Phase | Repos | Why this order |
|---|---|---|
| **0** goal roll-up + operator queue-next | chorus, podium | Unblocks "never stops"; everything downstream depends on goals closing |
| **1** converge/feedback/cap/status | chorus | Makes goals actually *reach* done instead of looping |
| **2** dedup/names/pull-growth | chorus, podium | Stops the org bloat that Phase 0/1 would otherwise keep feeding |
| **3** CEO reliability + status + report health | chorus, podium | Observability + the last correctness gap |

**Method for each change:** branch off `dev/overnight-autonomy`, implement, then **run a fresh clean company** (reset DB → bootstrap → server → operator) and re-generate the report; confirm the specific audit item is gone before moving on. Keep `AUDIT.md` as the regression checklist — every fixed item must stay fixed across runs.

## Risks / watch-outs
- **Roll-up over-eager:** only the goal-root task should flip the goal; a mid-tree `done` must not.
- **Cap-accept masking failure:** only accept a capped subtree that produced real artifacts; otherwise a broken goal would look "done".
- **Dedup false-positives:** two genuinely different people can share a first name — dedup on `(name, role, reports_to)` together, and prefer requiring expansion to reference existing `employee_id`s.
- **Pull-growth starvation:** ensure leads actually *file* staffing requests when short-handed, or growth will never fire; verify the lead brief emits `staffing_request` on a capability gap.
