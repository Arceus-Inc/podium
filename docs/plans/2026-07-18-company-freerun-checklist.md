# Company free-run checklist — Arceus runs like paperclip

*2026-07-18 · The bar: submit one founder objective, approve one plan, and the company gets
substantial work done across multiple employees with nobody pushing it. Reference: paperclip
("review what your executives are doing… reprioritize, assign") — adapted, not imitated: our
authority stays typed and human-gated. Every item below was verified or falsified against the
LIVE linkport company (workspace 019f7438-7e99…, company 019f7438-7e9c…); failures cite the
evaluator's own notes.*

## The checklist

| # | Property (repo) | State | Evidence / root cause |
|---|---|---|---|
| 1 | Formation forms the org with responsibilities + bounded grants (chorus+podium) | ✅ | Plan applied; 3 hires each with 2-3 ownership statements |
| 2 | Every hire heartbeats: role routines provision at BOTH hire paths (chorus) | ✅ fixed | `f942cd0` — plan materialization now reconciles declared routines |
| 3 | Routines fire on schedule, coalesce, and can be fired now (chorus+podium+cockpit) | ✅ | 3 dispatched + 1 coalesced live; fire-now 200/409 both verified |
| 4 | A root goal exists from the founder objective — the why-chain has a root (podium) | ❌→fix | goals=0; formation never seeds direction, so the CEO "reviews" an empty tree |
| 5 | The executive review can actually SEE the company: goal tree, reports' work, spend (chorus) | ❌→fix | Evaluator: "goal tree/health, work logs, and spend artifacts are missing" — the ground truth lives in the ledger; the beat's worktree has none of it |
| 6 | DoD commands run on the host they verify on (chorus_employee) | ❌→fix | PM floor + analyst DoD invoke bare `python` → rc=127 on this host; `python_check` already solved this with `sys.executable` |
| 7 | DoD rubrics demand only evidence the verifier can see (chorus_employee) | ❌→fix | Formation re-beat failed 0.0 for "no auditable evidence workforce_catalog_read was called" — tool-call ordering is invisible to the oracle by design |
| 8 | Routine intents are satisfiable under the role's own DoD (chorus_employee) | ❌→fix | PM weekly-planning asks for `plan.md`; the PM floor also demands a recorded, cited `decision.json` the intent never mentions |
| 9 | Coordination verbs exist in LIVE beats, not just materialize-time tests (chorus) | ❌→fix | lp-lead-1's beat: `read_comments` "failed due to tool not in manifest" — the execution-profile path drops the appended verbs |
| 10 | Comments flow: agent↔agent + human↔agent, inbox lands in the brief (chorus+podium) | ✅ engine / verify live after #9 | Tools + brief injection tested; human comment delivered live with a wake |
| 11 | Delegated delivery: goal → lead decomposes → ICs build in parallel → verify → integrate (chorus) | ✅ engine (T3 proven) / re-verify in free-run | Needs #4 (goal) + lead grant from the applied plan |
| 12 | Spend is priced per beat and budgets bound it (chorus+podium) | ✅ | $2.49 attributed live; budget PATCH door + hard-stop |
| 13 | Board can steer without killing: pause/resume, budgets, approve/reject, comment (podium+cockpit) | ✅ | All exercised live |
| 14 | Stalls surface, never silently fix (podium watchdog) | ✅ | Watchdog + Ops recovery from CP-4; timed-out review surfaced as failed run |
| 15 | Memory loop: beats write episodes; lattice consolidates; skills evolve (lattice) | ◻ observe | Episodes write live; consolidation needs ≥5 beats/cluster — observe during free-run |
| 16 | Direction stays horizon's seam: goals mirror locally until horizon ships (horizon) | ✅ by design | Local goal table is the documented mirror |

## Fix plan (first principles, no hardcoding)

- **F1 (#6)**: PM/analyst DoD commands go through the interpreter that runs the oracle
  (`sys.executable`), exactly as `chorus.outcomes._platform.python_check` already does.
- **F2 (#5)**: a beat whose subject is company state receives the company state — the factory
  mirrors a `company_state.json` packet (goal tree, roster with status + last beats, open tasks,
  spend vs budgets) into the worktree for any role that holds `governance_read`. Ledger facts
  become citable files; no rubric relaxation needed.
- **F3 (#7)**: the CEO's formation DoD grades repo-visible artifacts only (plan shape,
  responsibilities, bounded grants) — never tool-call ordering the oracle cannot observe.
- **F4 (#8)**: the PM weekly-planning routine intent asks for what the PM's floor verifies:
  update `plan.md` AND record the week's priority decision (cited) via `record_decision`.
- **F5 (#9)**: the execution-profile path must admit the same universal verbs materialize
  admits — one rollout point, not two.
- **F6 (#4)**: the formation door seeds the root goal from the founder objective server-side
  (idempotent: only when the company has no root goal). Delivery runs then inherit it (OM-2).
- **F7**: relaunch, clean stale duplicate formation work, and run the free-run: routines fire →
  review cites real state → kick one delegated delivery under the root goal → multiple
  employees build in parallel with comments — measure what landed.
