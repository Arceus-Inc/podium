# The Product Plane Program — M4 control plane × LLMOps/observability × M6 ship shape

*2026-07-17 · status: proposed · combines and supersedes-as-schedule:
[M4 control-plane design](2026-07-17-m4-control-plane-design.md) (the seam + doors),
the LLMOps + Observability Dashboard design v2 (the spine + projections), and the
architecture doc's M6 bullet (packaging). The two source designs stay authoritative for
their internals; this doc is the merged scope, the shared surfaces, and the one build order.*

## 0. Why combine them

The three plans are one product surface seen from three angles:

- **M4** builds the *doors*: `CompanyControlPlane` — podium's composition root over the four
  engines — and the thin, governed HTTP routers that map onto its sub-facades 1:1.
- **OBS v2** builds the *spine and the glass*: a `trace_id` correlation spine + taps at the
  composition root, LLMOps read models, and a dashboard that renders a concurrent actor system
  (workforce lanes, allocation board, delegation trees, run traces) from one SSE stream.
- **M6** *ships* it: Dockerfile, compose api+conductor+postgres(+dashboard), readiness, watchdog.

They overlap on exactly the surfaces where building them separately would duplicate work:

| Shared surface | M4 name | OBS name | One deliverable |
|---|---|---|---|
| Read plane over the company ledger | api-side read `CompanyControlPlane` (CQRS split §3.2) | snapshot endpoints "from ledger + inspector, never event re-derivation" (P6) | the read plane serves BOTH product doors and dashboard snapshots |
| Workforce/teams/capacity views | M4c doors | workforce + allocation + delegation snapshots | same endpoints, one DTO set |
| Observability doors | M4d (`report`, `skills`, `learning`, artifacts) | overview/memory/evals snapshots | one `observe` facade |
| Watchdog | M6 "watchdog surfacing (paperclip thresholds)" | `run.stalled` events + red lanes | one detector emitting one event, surfaced in Ops tab + readiness |
| Exit demo | latch-company over HTTP | 3 concurrent employees, lane-separable replay | one E2E: drive over HTTP, watch it live on the dashboard, `docker compose up` |

And the OBS taps have a hard placement dependency on M4's object: the taps live **at the
composition root**, which after M4a *is* the control plane. Building OBS first would mean
wiring taps into `src/company` and moving them a week later. So: seam first, spine on the seam.

## 1. What M5 already resolved (deltas to the source docs)

Both source docs predate the M5 landing; these points are now settled:

- **The CQRS split's precondition is met.** M4 §3.2's "needs M5" and the whole sqlite-interim
  paragraph (routing reads through the conductor mailbox) are obsolete: api and conductor share
  the Postgres ledger concurrently; the api opens read planes freely. M4a's "temp sqlite ledger"
  test note becomes the real PG test cluster.
- **Run/event ids are uuidv7 already** (`mint_id()` landed in M5.2) — OBS §3's "switch scheduler
  run-id minting" is done.
- **Skills live in the shared engine schema** (`0002_skills`): the dashboard's skill-version and
  evolution views read `ledger.skills`/`ledger.skill_revisions` through the read plane — no
  filesystem access. **Episodic memory stays workdir-local SQLite by design**, so `memory`
  snapshot reads (atoms/retrieval) go through the conductor host or accept the M3c shared-FS
  constraint — same as `/logs`, same later fix (read mirror).
- **`cost_event` is priced source of truth in the ledger** and RLS-scoped per company — the cost
  read models are plain ledger queries on the read plane.

Still true and still the work: `Event.trace_id` is never populated; the facade wires a bare
`EventBus()`; dream spans are trapped in per-task JSONL; horizon discards tokens; lattice logs
nothing; `runs.counts` is never rolled up.

## 2. Combined design invariants

M4's principles and OBS's P1–P10 don't conflict; the merged set, deduplicated:

1. **One composition root per side**: `src/company.build()` for the engine, `CompanyControlPlane`
   for the product. Every router and every dashboard snapshot goes through the plane; no code
   reaches `graph.org._ledger` past it. *(M4 §2 — now also governing OBS snapshots.)*
2. **The company is a concurrent actor system** — every view degrades to "many things at once";
   per-company `seq` is a cursor, not causality; cross-lane causality only via explicit link
   events. *(OBS P1/P3.)*
3. **Every event names its actor and work unit**: `(company_id, seq, trace_id, employee_id,
   task_id, run_id, type, at, payload)`. *(OBS P2.)*
4. **All views are folds over one spine** (+ ledger tables for cold state); a new view is a new
   fold, not new instrumentation. Snapshot endpoints read ledger/inspector state — allocation is
   observable state, never inferred from run text. *(OBS P4/P6.)*
5. **Writes split by coupling**: heartbeat-coupled → conductor (commands mailbox);
   execution-independent ledger writes + all reads → api, behind `decide()`. *(M4 §3.2/§3.4.)*
6. **dream and lattice stay internal** — read-only telemetry through `observe`; no evolve/forget
   doors; retrieval is instrumented at the moment of use (`memory.retrieved`, misses included).
   *(M4 §3.1 + OBS P5.)*
7. **Big text off the spine** (≤2k excerpts + `RunLogStore`), **single writer per company**
   (`EventMirror` assigns seq), **evals ride the spine** async, never in the beat path.
   *(OBS P8–P10 — all already-landed M3 invariants.)*

## 3. The combined surface (routes, one table)

All per-company, auth + `decide()` + tenant-scoped; (M4) / (OBS) marks origin; **bold** = both.

```
# Direction (horizon)                                                         (M4)
GET/POST /v1/companies/{id}/proposals · POST /v1/proposals/{id}/approve|reject
GET      /v1/companies/{id}/goals · PATCH /v1/goals/{id}
POST     /v1/companies/{id}/decisions · POST /v1/decisions/{id}/decompose
GET      /v1/companies/{id}/strategy          # goal tree + health/score      (OBS — reads the same tree)

# Workforce + delegation (chorus)
GET/POST /v1/companies/{id}/employees · DELETE /v1/employees/{id}             (M4)
GET      /v1/companies/{id}/roles                                             (M4)
POST     /v1/companies/{id}/runs   {execution_mode, lead, max_team_size, …}   (M4 — widened)
GET      **/v1/companies/{id}/teams** · GET .../teams/{id}
GET      **/v1/companies/{id}/capacity**
GET      /v1/companies/{id}/workforce         # live lanes snapshot           (OBS)
GET      /v1/companies/{id}/allocation        # queues/wakes/blocks/leases    (OBS)
GET      /v1/companies/{id}/delegation        # active trees                  (OBS)

# Observability (chorus.inspect + lattice reads + M3 events + LLMOps)
GET      /v1/companies/{id}/stream            # exists (SSE replay+tail)
GET      **/v1/companies/{id}/report** · GET /v1/runs/{id}/stuck
GET      **/v1/companies/{id}/skills** · GET .../employees/{eid}/learning
GET      /v1/companies/{id}/overview · /costs?window=&by= · /memory · /evals  (OBS)
GET      /v1/companies/{id}/artifacts · GET /v1/runs/{id}/artifacts           (M4)
GET      /v1/runs/{id}/events · /logs         # exist (M3)

# Portability                                                                  (M4)
POST     /v1/companies/{id}/export · /import  # 202 + async job
```

Event vocabulary: OBS §4 verbatim (execution, allocation, delegation, `llm.call`,
`cost.recorded`, strategy, memory retrieval + learning, `eval.recorded`, `run.stalled`).

## 4. Build order (each slice TDD; gate: ruff · mypy --strict · pytest on real Postgres)

Renumbered as one ladder — **CP-0 … CP-6**. Mapping to source milestones in brackets.

- **CP-0 — the seam** [M4a]. `podium.control.CompanyControlPlane` + sub-facade protocols + DTOs;
  `ControlPlaneProvider` building **read** planes over the Postgres company ledger.
  *Exit:* a seeded company is fully readable through the plane, hermetically, no model calls.
- **CP-1 — the spine** [M-OBS-1]. `trace_id` end-to-end (chorus stamps from beat context; dream
  via `SessionOptions.metadata` + composite Tracer; horizon decorator + observer carrying
  `run_id`/`employee_id`; lattice wrapper incl. `memory.retrieved`); FanoutBus sink at the root;
  allocation-transition events (`work.*`, `budget.denied`); delegation lineage
  (`parent_run_id`, `child_task_id`) on subagent/team events.
  *Exit:* 3 employees running concurrently produce interleaved, lane-separable, replayable
  events with correct per-lane order and delegation links.
- **CP-2 — direction doors** [M4b]. Proposals/goals/decisions endpoints behind `decide()`;
  `/strategy` read model over the same tree.
  *Exit:* set direction → approve → goal tree visible over HTTP.
- **CP-3 — workforce + delegation doors** [M4c]. Roster/hire/terminate; `POST /runs` widened
  with execution_mode + delegation params; teams/capacity views.
  *Exit:* hire and delegated-run over HTTP; teams view shows the staffed branch.
- **CP-4 — read models** [M4d + M-OBS-2]. `observe` complete: report, artifacts, skills/learning
  (ledger reads via `0002_skills`), workforce/allocation/delegation/overview/costs/evals
  snapshots; `cost.recorded` mirror + `runs.counts` rollup at finalize; `eval.recorded` at
  landing; watchdog `run.stalled` (silent-run / stale-lease thresholds); export/import async.
  *Exit:* allocation board and workforce snapshots match ledger truth under concurrent load.
- **CP-5 — the dashboard** [M-OBS-3+4]. Zero-build static SPA served by podium behind JWT; one
  SSE connection, client-side folds. First Org (workforce lanes + component-map strip) + Work
  (allocation board) + Runs (per-run trace with `llm.call` spans and `memory.retrieved`
  markers); then Costs / Strategy / Memory / Ops. Retires the example dashboards.
  *Exit:* reconnect mid-flight with N employees active → N live lanes rebuilt from
  Last-Event-ID; the latch-company demo is *watchable*.
- **CP-6 — ship shape** [M6]. uv multi-stage Dockerfile; compose api + conductor + postgres
  (dashboard = static files off the api); uvicorn workers (conductor 1/shard); readiness gates
  (`/readyz` covers DB, migrations incl. engine deltas, mirror liveness); watchdog thresholds
  configured, surfaced in Ops and as container health.
  *Exit:* `docker compose up` → the full CP-0…CP-5 demo end-to-end on a fresh machine.

**Program exit** (subsumes both source exits): over HTTP only — set direction → approve a
proposal → hire → delegated run with 3 concurrent employees → watch live lanes + allocation +
delegation tree on the dashboard → reconnect and see the lanes rebuild → inspect report, costs,
evolved skills → export — all inside `docker compose up`.

## 5. Sequencing rationale & risks

- **CP-0 before CP-1**: the taps attach to the plane/provider; building the seam first means the
  spine lands once. The cost is that the flashiest part (dashboard) comes late — mitigated by
  CP-1's exit producing inspectable raw streams immediately (curl the SSE endpoint).
- **CP-2/CP-3 can swap or parallelize** — independent door families over the same plane.
- **Memory snapshots** (`/memory`, retrieval feed) carry the episodic shared-FS caveat (§1);
  everything else in CP-4/5 is pure Postgres. If the caveat bites in prod topology, ship the
  memory tab degraded (learning/skills only — those are ledger reads) without blocking CP-5.
- **Deferred, unchanged from both docs**: OTel exporter (events stay OTel-GenAI-shaped), Redis
  broadcaster, S3 log mirror, frontend build step, prompt management, auto-attribution.
