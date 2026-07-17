# Product Plane Program — delivery record (CP-0 → CP-6)

*2026-07-17 · status: **delivered** · program: [product-plane-program.md](2026-07-17-product-plane-program.md)
· branch: `feat/product-plane` (one merge per CP, one sub-branch per CP, one commit per checkpoint;
every checkpoint commit body carries its four-way review — verify with
`git log --grep="Reviewed" feat/product-plane`, 18 hits at delivery time).*

## Protocol held (per the goal's instructions)

- **Branching**: `feat/product-plane` off `main`; each CP on its own branch off it
  (`cp0-control-plane-seam`, `cp1-trace-spine` (+ chorus `feat/trace-spine`), `cp2-direction-doors`,
  `cp3-workforce-runs`, `cp4-read-models`, `cp5-dashboard`, `cp6-ship-shape`, `cp-pricing`), merged
  `--no-ff` so the CP boundaries stay visible in first-parent history.
- **Checkpoints**: sub-tasks tracked per CP; one commit per checkpoint (see the `checkpoint N`
  commit-subject convention below).
- **Reviews**: clean-python, functional, security, and ponytail findings applied *in* each
  checkpoint commit and recorded in its body ("Reviewed (4-way): …"). Representative applied
  findings: DSN repr-suppression (security, CP-0), goal-tree cycle guard (functional, CP-0),
  spend-grouping SQL whitelist pinned by a hostile-input test (security, CP-4), `_require_ledger`
  deletion (ponytail, post-CP-0), engine-vocabulary DTOs over invented fields (clean, CP-2/4).
- **TDD**: RED-first throughout — each checkpoint's tests were written and shown failing before
  implementation (podium suite grew 42 → 72 tests; chorus gained trace-spine/allocation/watchdog
  suites).
- **Skills**: backend-patterns (doors/facades), frontend-patterns (dashboard), postgres-patterns +
  system-design (the program doc itself and each CQRS/storage decision).
- **E2E with LLM keys**: a live stack (ephemeral PG18 → `bootstrap_db` → api+conductor with real
  Azure model keys) ran a real directive end-to-end. The deliverable was committed in the
  employee's worktree; the spine recorded 3,688 events — 100% trace-stamped — incl. 16 `llm.call`,
  119 tool calls, subagent + verifier lanes; `runs.counts` folded them at finalize; the dashboard
  connected live and rendered per-employee lanes.

## What each CP shipped

| CP | Checkpoints | Substance |
|---|---|---|
| CP-0 | 3 | `podium.control`: provider + read planes over the RLS-scoped engine ledger; Workforce/Delegation/Observe/Direction facades, frozen DTOs |
| CP-1 | 7 chorus + 1 podium | `trace_id` spine (lineage-root stamping at the observer choke point), `cost_event.trace_id` (delta `0003`), allocation + intake events, `memory.retrieved`, `llm.call`, `run.stalled` watchdog; podium events envelope (`0010`) + trace-first run routing; exit pinned: 3 concurrent employees, lane-separable |
| CP-2 | 4 | governed doors: `GET/POST /goals`, `PATCH /goals/{id}`, five read doors (workforce/teams/capacity/status/skills) — doubling as OBS-2 snapshots |
| CP-3 | 2 | hire/terminate through the real engine facade (invariants unre-implemented); `POST /runs` delegation widening (`0011`, `_submit_kwargs`) |
| CP-4 | 5 | `/allocation`, `/costs` (grouped whitelist), `/overview` (both DBs), `/report`; `runs.counts` rollup at finalize |
| CP-5 | 1 + live fixes | zero-build cockpit (one SSE, five tab projections, lane auto-create) — live-verified in a browser |
| CP-6 | 2 + live fix | uv multi-stage image; compose postgres→migrate→api+conductor; `/readyz` proves engine deltas; conductor control DSN |

## The live e2e's three findings (all fixed, tested, merged same-session)

1. **Conductor control DSN** — under `podium_app` alone, RLS (fail-closed, by design) hid every
   queued run from cross-tenant discovery; compose now wires the M2 control connection.
2. **Lane auto-creation** — employees hired after the workforce snapshot (the conductor's default
   worker) emitted into the void; lanes now create on first sight.
3. **Unpriced ledger** — `llm.call` carried `cost_usd` while `cost_event` stayed empty;
   `company.build` now wires `TokenPricing` into both factories.

Plus one unplanned exercise: killing a mid-beat server verified the crash/lease-reclaim path
against a live run.

## Environment-gated residue (recorded, not hidden)

- The first `docker compose up` needs a docker host (CLI absent in the build environment); the
  topology was proven piecewise live (bootstrap → delta-aware readyz → api+conductor+control DSN).
- The horizon reasoner tap (OBS §3) activates when a reasoner is wired (`reasoner=None` today);
  the decorator seam is documented in the program doc.
- Engine-behaviour observation for tuning: a small directive grew into an engine marathon
  (decomposition + verification + follow-up intake) that outlived the executor's 60-tick budget —
  `max_ticks` / follow-up-intake policy is a product decision to take before GA.
