# Lumen Run — Anti-Pattern Audit

**Company:** `019f78e6-e1a1-7bc0-a48b-43b170529cfb` ("Lumen")
**Source:** ledger DB (the same data behind `company-report.html`)
**Date:** 2026-07-19

**Headline:** 27 beats, **$21.89** spent, **0 of 3 goals completed**, and only **7 of 18 "employees" ever did any work**. The report looks healthy at the top (18 employees, 14 artifacts) but the ledger underneath shows a company that over-hired, redid the same work repeatedly, never closed a goal, and then stalled.

> The body of this document is the **original run5 audit** (the 16 anti-patterns we found). The
> **Handover Status** below tracks what has since been fixed, validated, and what remains — read it first.

---

## Handover status (2026-07-19) — for Divyansh

**Where it stands:** the original 16 anti-patterns are largely resolved and validated across live runs
(run7 → run9). A clean **run9** (`019f7a93-761f-7c46-8aab-532d687ac433`) now founds a textbook
cross-functional pod company and executes multiple goals in parallel:

```
Casey (CEO)
├─ Alex  (lead/pm) ─ Maya (designer) · Owen (frontend) · Priya (analyst)
└─ Diego (lead/pm) ─ Samira (designer) · Jordan (frontend) · Noah (analyst)
```

Real human names, zero duplicate identities, only leads report to the CEO, cross-functional pods,
**pull-based** growth (9 people, not 18). Branch for all work: `dev/overnight-autonomy` (pushed).

| # | Anti-pattern | Status | Fix + evidence |
|---|---|---|---|
| **B1/D1/D2** | Goals never complete / never advances / stalled | ✅ **Fixed + validated** | Engine goal roll-up: a delegation **goal-root** task reaching `DONE` flips the goal `done` (`chorus` `_ledger.py::_complete_goal_if_root`, commit `2618c88`). Operator reacts and queues the next roadmap goal (`podium` `e3b68a8`). Deterministic test + live run7/run8 (5 goals rolled up). |
| **B3/C1** | Delegation loop → cap → fail; `needs-changes` not actioned | ✅ **Fixed + validated** | Honest cap-accept: at the integrate cap, **accept** if ≥1 child is `done` and the command floor passes; otherwise **strand** honestly (never fabricate a pass). Manager brief now carries the evaluator's exact failing checks into the corrective task. (`chorus` `7a7fbbf`.) Live run7: notes-app converged at iteration 4>cap 3 → `parent_verified {capped_accept:true}`. |
| **A1** | Duplicate employees (ghost re-hires) | ✅ **Fixed + validated** | Governance `_workforce_plan._validate` rejects a hire whose name matches an existing active employee or repeats within the plan (`chorus` `7850643`). run8/run9: **zero** duplicate identities. |
| **A2** | Placeholder people ("Pod Lead (Delivery)") | ✅ **Fixed + validated** | CEO brief requires a **real given name** per person, never a job title; governance rejects empty names (`chorus` `7850643`). run9 roster: Casey/Alex/Diego/Maya/Samira/Jordan/Owen/Noah/Priya. |
| **A5** | An IC reports straight to the CEO | ✅ **Fixed + validated** | Governance rejects a non-lead whose `reports_to` is the CEO (`chorus` `7850643`). run9: only the two leads report to Casey. |
| **A3/A4** | Stale idle hires / over-hiring (push growth) | ✅ **Fixed + validated** | Operator `growth_daemon` is now **pull-based**: expand only on an open `staffing_request` or when below a min-viable headcount with <2 idle ICs (`podium` `7f6b5f7`, `MIN_VIABLE_HEADCOUNT`). run8/run9: settled at 9–11, not 18. |
| **D1 (stuck-goal variant)** | A goal that can never converge clogs the pipeline | ✅ **Fixed + validated** | Operator **shelves a stranded goal** (delegation root `blocked` + active `integrate_iteration_exhausted` recovery) and advances the roadmap (`podium` `42f1d7b`). Live run8b: 2 stranded prose goals shelved → next goal queued. |
| **(root of B3 for prose goals)** | Written-deliverable goals strand on a DoD filename mismatch | ✅ **Fixed — validating in run9** | Root cause: the designer/marketer DoDs are intent-agnostic and demand fixed artifacts (`DESIGN.md`+`design_spec.md` / `content_draft.md`), but the roadmap asked for `BRAND_GUIDE.md`/free-form files → the deterministic floor could never pass. Reframed the brand & GTM roadmap goals to target the roles' real verifiable artifacts (`podium` `4913e2b`). run9 is exercising this now. |
| **D4/C2 (partial)** | Empty `decision_record`; CEO formation fails repeatedly | 🟡 **Improved, not closed** | Formation now succeeds (chronic 7×-fail gone; occasional single retry then success — run9 failed once, retried, succeeded). Remaining: intermittent "Executive review" routine failures + `decision_record` still thin. **Owner action:** instrument the formation/executive-review beat DoD to log *why* it blocks. |
| **D3** | `status` is always `idle` (no lifecycle) | 🔴 **Open** | Employee `status` still not driven from live lease/task state. Non-blocking for behavior; needed for honest observability. |
| **B2/B4** | Same work redone 3×; false "succeeded" | 🟢 **Not reproduced since** | Not observed in run7–run9 (roll-up + cap-accept removed the re-decompose churn). Keep as a regression watch; a dedicated re-decomposition guard (PLAN 1.1) was **not** separately implemented. |

**Known deep items still open (documented, deferred as risky-to-fix-blind):**
1. **CEO formation / executive-review reliability (C2, D4)** — intermittent `steps_blocked:1`; thin decision log.
2. **Liveness gap** — occasional multi-hour stalls from wake starvation (a hung child beat under a long lease left the manager with no re-wake until a periodic CEO routine fired). Deep engine lease/monitor-timing issue.
3. **Employee status lifecycle (D3)** — observability only.

**How to verify / continue** (recipes live in the repo, not here — no secrets in this doc):
- Reset DB → bootstrap → start `podium` server (embedded conductor) → start `podium/scripts/company_operator.py`.
- Watch: `select name,role,status,reports_to from employee where company_id=<co>` (real names, no dupes, leads-only under CEO); `select title,status from goals` (goals reaching `done`/`stranded`); operator log for "COMPLETED (ledger roll-up)" / "SHELVED (delegation stranded — moving on)".
- Regenerate the report: `podium/reports/lumen-run` (health panel in `build_lumen_report.py` auto-flags dupes, idle, stranded delegations, redone work).

**Commits (branch `dev/overnight-autonomy`, pushed to `origin`):**
`chorus`: `2618c88` (roll-up) → `7a7fbbf` (cap-accept + feedback brief) → `7850643` (dedup/names/IC-under-CEO).
`podium`: `e3b68a8` (roll-up operator) → `7f6b5f7` (pull-growth) → `42f1d7b` (strand-shelving) → `4913e2b` (prose-goal reframe).

---

## A. Org integrity — the headcount is a mirage

### A1. Duplicate employees (ghost re-hires)
Expansion amendments re-hired people who *already existed*. Every "hire" got a brand-new row instead of referencing the existing person.

| Person | Founding row | Duplicate row | Duplicate did |
|---|---|---|---|
| Avery (designer/lead) | `emp-lead…` 05:44 | `hire-le…` 05:46 | 0 tasks, 0 beats |
| Jordan (frontend) | `emp-fe-1` 05:44 | `hire-fe…` 05:46 | 0 tasks, 0 beats |
| Riley (analyst) | `emp-qa-1` 05:44 | `hire-qa…` 05:46 | 0 tasks, 0 beats |
| Morgan (backend) | `emp-lead…` 05:44 | `hire-be…` 05:46 | 0 tasks, 0 beats |
| Pod Lead (Delivery) | `nh-podl…` 05:59 | `nh-podl…` 06:00 | 0 tasks, 0 beats |

A real company never clones an existing employee to "add capacity."
**Root cause:** the growth/expansion beat proposes hires by *name/role*, and governance applies them as new `employee` rows with **no dedup** against existing `name + role + reports_to`.

### A2. Placeholder people
Later hires aren't people — they're job titles: **"Pod Lead (Delivery)", "Frontend Engineer (Delivery Pod)", "QA/Test Analyst (Pod A)", "QA/Test Analyst (Pod B)"**. Real companies hire *named* individuals.

### A3. 11 of 18 are stale — zero tasks, zero beats
Entire late-created pods (Blake, the whole "Delivery" pod, both QA analysts) are dead weight. Real headcount that did anything: **7** (Casey, Avery, Morgan, Jordan, Sam, Riley, Taylor).

### A4. Over-hiring with nothing to give
6 expansion rounds pushed to 18 people, but `MAX_ACTIVE_GOALS=3` meant only 2 pods ever had work. **No `staffing_request` was ever filed** — growth is *push* (a daemon that keeps firing on a "bottleneck" signal) instead of *pull* (hire because a lead asked for a specific missing skill). So it kept hiring into a company that had no open work.

### A5. An IC reports straight to the CEO
Quinn (pm) hangs off Casey with no pod and never worked — the exact "flat org" leak we tried to kill.

---

## B. Delegation & decomposition — work is redone, never closed

### B1. No goal ever completes (roll-up is broken)
All 3 goals are still `active`. Six depth-1 subtasks are `done`, but **every depth-0 delegation task stays `blocked`** — children finishing never rolls up to the parent, so the goal never closes, so the operator never queues the next roadmap items (Pomodoro, analytics, GTM, habit tracker, landing page **never started**).

### B2. The same work is done 3 times
The notes app was implemented as three separate subtasks:
- 05:45 "Implement the distraction-free markdown notes app" → **done**
- 06:05 "Implement distraction-free markdown notes app" → **done** (again)
- 06:24 "Implement split-pane markdown editor…" → **in progress** (a third time)

Each delegation iteration **re-decomposes the goal from scratch** instead of reading the prior worktree and building on it. In a real team, sprint 2 continues sprint 1's code — it doesn't rewrite it.

### B3. Delegation loop → cap → fail
The design system lead (Morgan) beat it 4 times: `fail → needs-changes → needs-changes → needs-changes`, then hit the iteration cap (3) and the whole delegation **failed**. It never converged.

### B4. "Succeeded" that isn't
Lead delegation beats are marked `succeeded` while their payload says `steps_blocked: 1, sprint_outcomes: ["fail"]`. The status lies about what happened.

---

## C. Failure → recovery — "a failed beat should give the next beat enough info to pass"

### C1. `needs-changes` isn't actioned
The design system got `needs-changes` three iterations in a row and each next beat did **not** fix what was flagged — it just looped until the cap killed it. The evaluator's specific change list isn't being carried into the next beat's prompt as actionable instructions.

### C2. The CEO fails formation 7 times in a row
Casey's formation/executive beat failed at 05:43, 05:46, 05:49 (`TimeoutError`), 05:54, 05:55, 05:59, 06:01 — all `steps_blocked: 1` — before one finally succeeded at 06:04. Seven identical failures = the retries carried **no** recovery information. (This is also why `decision_record` is **empty** — the CEO never got to record a decision.)

### C3. Rejected work is abandoned
Riley's "Add Playwright e2e tests" and Taylor's "Write repository docs" were both **rejected** and never re-driven to green.

---

## D. Lifecycle & data integrity

### D1. "Never stops" is actually "never advances"
Because no goal closes (see B1), the roadmap never advances past the first 3 goals. The loop is stuck, not perpetual.

### D2. The company stalled
Everyone is `idle`, last beat was 06:24, no forward progress.

### D3. `status` is meaningless
Every employee — CEO, active workers, ghosts — shows `idle`. There's no working/blocked/terminated signal, so you can't tell from status who's doing anything.

### D4. `decision_record` is empty (0 rows)
The report's "Decisions & executive directives" section is hollow because the CEO's decision beats kept failing.

---

## E. What a real company would do (fix directions for the next iteration)

1. **Dedup hiring** — expansion must reference an existing `employee_id`, never re-create someone with the same name/role/manager. Governance should reject a plan that duplicates an existing identity.
2. **Fix roll-up** — parent delegation task → `done` when all children `done`; goal → `done` when its delegation task is done; then the operator queues the next roadmap goal. This alone unlocks "never stops."
3. **Build-on, don't redo** — a lead re-delegating a goal must read the prior subtasks/worktree and continue, not re-decompose from zero.
4. **Actionable feedback** — when a sprint returns `needs-changes`/`rejected`, feed the evaluator's exact change list into the next beat's prompt; reconsider the hard iteration cap so convergence isn't guillotined.
5. **Growth = pull, not push** — only hire when a lead files a real `staffing_request` for a specific missing skill; stop firing once the active goals are staffed. Require real person names.
6. **CEO formation reliability** — investigate why formation beats are `steps_blocked: 1` every time; that's the root of the empty decision log.
7. **Honest status** — make `status` reflect working/blocked/idle/terminated so ghosts and stalls are visible.

**Biggest single lever:** **#2 (roll-up)** — once goals actually close, the operator advances the roadmap, growth pressure drops, and most of the over-hiring/redo cascade goes away.

---

## Appendix — evidence snapshot

**Employees by work done** (name · role · manager · tasks · beats):

```
Jordan   frontend_engineer  Avery              0  0   <- ghost dup
QA/Test Analyst (Pod A)     Avery   analyst    0  0   <- placeholder, idle
Riley    analyst            Avery              0  0   <- ghost dup
Morgan   backend_engineer   Avery              0  0   <- ghost dup
Avery    designer           Casey              0  0   <- ghost dup
Pod Lead (Delivery)  pm     Casey              0  0   <- placeholder dup
Pod Lead (Delivery)  pm     Casey              0  0   <- placeholder dup
Blake    pm                 Casey              0  0   <- idle
Quinn    pm                 Casey              0  0   <- IC under CEO, idle
QA/Test Analyst (Pod B)     Morgan  analyst    0  0   <- placeholder, idle
Frontend Engineer (Delivery Pod)   Pod Lead    0  0   <- placeholder, idle
Taylor   analyst            Morgan             1  1
Riley    analyst            Avery              2  2   <- real
Sam      frontend_engineer  Morgan             3  3   <- real
Avery    designer           Casey              2  4   <- real
Jordan   frontend_engineer  Avery              4  4   <- real
Morgan   backend_engineer   Casey              1  5   <- real
Casey    ceo                (CEO)              3  8
```

**Beat timeline (selected):**

```
05:43:50  Casey   FAILED   formation (steps_blocked=1)
05:45:09  Avery   succ*    notes-app delegation (steps_blocked=1)
05:45:10  Morgan  succ*    design-system delegation (sprint=["fail"])
05:45:36  Jordan  succ     implement notes app UI (done)
05:45:36  Sam     succ     create design system package (done)
05:46:56  Casey   FAILED   formation
05:49:12  Casey   FAILED   formation (TimeoutError)
05:53:25  Taylor  FAILED   write repo docs (rejected)
05:54:13  Casey   FAILED   formation
05:55:15  Morgan  succ*    design-system (needs-changes)
06:04:07  Morgan  succ*    design-system (needs-changes)
06:14:29  Morgan  succ*    design-system (needs-changes)
06:16:16  Morgan  FAILED   design-system (cap=3, iteration=4)
06:00:10  Riley   FAILED   playwright e2e (rejected)
06:05:09  Jordan  succ     implement notes app AGAIN (done)
06:24:20  Jordan  running  implement notes app a THIRD time
```
`succ*` = beat marked succeeded while its steps were blocked / sprint failed.

**Hiring timeline (dedup source):**

```
05:44:56  founding    Avery, Morgan (leads), Quinn, Jordan, Sam, Riley, Taylor   (emp-*)
05:46:28  expansion   Avery, Morgan, Jordan, Riley  AGAIN                         (hire-*)  <- dupes
05:48:47  expansion   Blake                                                       (nh-*)
05:59:46  expansion   Pod Lead (Delivery)                                         (nh-*)
06:00:49  expansion   Pod Lead (Delivery) AGAIN, Frontend Engineer (Delivery Pod) (nh-*)   <- dupe + placeholder
06:02:03  expansion   QA/Test Analyst (Pod A), QA/Test Analyst (Pod B)            (nh-*)   <- placeholders
```

**Goals:** all 3 `active` (none `done`).
**Tasks:** depth-0 → 5 `blocked`, 1 `done`; depth-1 → 6 `done`, 2 `rejected`, 1 `blocked`, 1 `in_progress`.
**Workforce plans:** 6 `applied`, 1 `proposed`. **Staffing requests:** 0. **Decision records:** 0.
