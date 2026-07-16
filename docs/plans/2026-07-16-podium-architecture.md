# Podium — product backend architecture plan (v2)

*2026-07-16 · status: proposed (awaiting review) · repo: podium (Arceus monorepo is v0)*

Podium is the stage the AI-company engine performs on: a **multi-tenant-native** backend fronting
the four engine repos (dream, chorus, lattice, horizon) with a REST API, Postgres, live event
streaming, and a managed lifecycle for running companies. `company.build()` (in this repo, pinned
by the wiring-parity suite) is the kernel; podium wraps it in a server.

v2 supersedes v1 after a deep read of **paperclipai/paperclip** (/tmp/paperclip — the most mature
open control plane in this exact problem space) and the decision to run the **chorus ledger on
Postgres** via its built-in adapter seam.

## Research inputs

1. **Stack consensus (2025-26)**: FastAPI, domain-package layout (zhanymkanov style); SQLAlchemy
   2.0 async + asyncpg + Alembic (naming conventions); pydantic-settings; structlog; SSE over
   WebSockets for one-way feeds; Postgres LISTEN/NOTIFY fan-out; plain `uvicorn --workers`;
   uv multi-stage Dockerfile.
2. **Agent-platform API convergence** (LangGraph Platform, OpenAI/Anthropic batch, Temporal,
   Inngest): container → run → append-only events; statuses
   `queued → running → (paused) → succeeded|failed|canceled|timed_out`, async `canceling`;
   artifacts separate from status; resumable streams keyed by event seq. Strong precedent
   (DBOS, Hatchet, AutoGPT Platform) for keeping our own Postgres-backed orchestration — no
   Temporal/celery.
3. **Paperclip patterns adopted** (verified in the clone):
   - **Per-company JWT signing keys**: HS256 key = `HMAC-SHA256(master, "jwt:{instance_id}:{company_id}")`
     — a leaked token cannot cross tenants or instances (`server/src/agent-auth-jwt.ts`).
   - **Tenant-prefixed composite indexes on every table** + every query tenant-scoped; isolation
     is structural first, RLS as backstop.
   - **Central pure `decide()` authorization** over a typed resource union — allow/deny, never
     throws, called by every handler. Paperclip's flagged weakness (actor middleware fails OPEN
     to `type:none`) is the anti-pattern podium inverts: **fail closed at the middleware**.
   - **Append-only `run_events` with per-run monotonic `seq`, indexed `(run_id, seq)`** — exactly
     the shape research 2 converged on; validates our design.
   - **Transcripts/logs do NOT live in Postgres**: durable run-log store = local file for fast
     tail + object-store mirror, DB keeps `log_ref/log_bytes/log_sha256` pointers + excerpt.
     Podium adopts this split: events table = telemetry; blobs = artifact store with pointers.
   - **CAS run lifecycle discipline** (`claimQueuedRun`, `setRunStatusIfRunning`, compare-and-clear
     lock finalization, "a 409 is a real owner — stop, don't retry", stale-lock recovery is crash
     recovery not a retry loop; `doc/execution-semantics.md §5`). chorus's scheduler already
     implements this family; podium's runs table mirrors — never second-guesses — those states.
   - **Per-company live-event channel + typed invalidation events** (`heartbeat.run.status|log`,
     `agent.status`, `activity.logged`) so the dashboard patches caches surgically. Podium keeps
     SSE (not WS) but adopts the channel-per-company + typed-event-name scheme.
   - **Per-company sliding-window rate limits** keyed `(company, actor_type, actor_id)`.
   - **Company portability** (export/import with secret scrubbing) — chorus already ships
     `export_workforce`/`import_workforce`; podium exposes it as an endpoint in M4.
   - Watchdog calibration worth copying: silent-run suspicion 1h / critical 4h, bounded transient
     retry ladder `[2m,10m,30m,2h]` + jitter, process-loss auto-retry bound = 1.

## The Postgres decision (revised from v1)

**Both databases are Postgres, one cluster, two isolation models:**

- **Podium product DB** (shared schema): workspaces, users, api_keys, companies, runs index,
  events mirror, commands. Every table carries `workspace_id`, every index is
  workspace-prefixed-composite, RLS policies as defense-in-depth from M1
  (`SET LOCAL app.workspace_id` propagated into the conductor's DB sessions).
- **Chorus ledger — Postgres driver via the existing adapter seam**: chorus repos already speak
  the "SQLite ∩ Postgres intersection over a DB-API connection" (spec 12, `repos/_base.py`);
  `psycopg[binary]` has been the reserved optional dep since spec 00 §7. The driver work is:
  a `LedgerConnection` Protocol replacing the `sqlite3.Connection` type hints (29 files, hints
  only), a paramstyle shim (`?` → `%s`), Postgres DDL for the migration files (byte-parity test
  becomes per-dialect), and connection setup. Isolation = **schema-per-company**
  (`search_path=company_<id>`) — the exact semantic of today's file-per-company SQLite, no
  chorus schema changes, drop-schema = today's rm-file. Podium selects the backend per company
  (`LEDGER_BACKEND=sqlite|postgres`), so dev/local stays zero-infra and the parity suite runs
  against both. **Schema-per-company is the on-ramp, not the end state**: M7 (committed, spec
  below) makes the ledger tenant-aware and consolidates everything into one shared schema,
  paperclip-style, with RLS enforcement.

## Architecture

```
┌──────────────┐   HTTPS + SSE   ┌───────────────────────────┐
│ arceus web / │ ──────────────► │ podium api (uvicorn xN)   │───┐
│  dashboard   │                 │ FastAPI · domain packages │   │ SQLAlchemy async
└──────────────┘                 └───────────────────────────┘   ▼
                                     ▲ LISTEN/NOTIFY      ┌─────────────────────┐
                                 ┌───┴──────────────────┐ │      Postgres       │
                                 │ podium conductor     │ │ public: workspaces, │
                                 │ (worker, 1/shard)    │ │  companies, runs,   │
                                 │ hosts CompanyGraphs  │ │  events*, commands  │
                                 │ = company.build()    │ │ company_<id>: chorus│
                                 └──────────────────────┘ │  ledger (per-tenant │
                                   │                      │  schema, spec 12)   │
                                   ▼                      └─────────────────────┘
                              per-company workdir: worktrees, episodic store,
                              lattice, run logs (file + object-store mirror)
```

- **api**: REST + SSE; never touches the engine in-process. Fail-closed actor middleware →
  central `decide()` → tenant-scoped queries. Commands to the conductor are rows + NOTIFY.
- **conductor**: hosts one live `CompanyGraph` per active company; runs the chorus heartbeat
  (unchanged — it IS the durable-execution layer); mirrors every EventBus event into
  `events (run_id, seq)` in the same transaction as NOTIFY; honors cancel/pause commands;
  writes run logs to the durable log store (file + pointer).
- Dev mode: conductor embeds in the api lifespan (single worker), sqlite ledger backend,
  compose Postgres for the product DB. One command to run.

## Data model (product DB; all tables workspace-scoped + composite-indexed)

- `workspaces` · `users` · `api_keys` (hashed, workspace+company scoped)
- `companies` — engine instance: config, state (`provisioning|idle|running|stopped`),
  ledger backend + schema name, workdir ref, per-company signing-key derivation inputs
- `runs` — directive execution: paperclip/LangGraph status vocab, `idempotency_key` unique per
  company, counts rollup, error, `log_ref` pointer
- `events` — append-only mirror: unique `(run_id, seq)`, `type`, `employee_id`, `payload jsonb`,
  monthly partitions; NOTIFY channel per company
- `artifacts` — landed deliverables index (kind, ref); blobs in the object store, never in PG
- `commands` — api→conductor mailbox, idempotent consumption

## API surface (v1)

```
POST   /v1/workspaces/{ws}/companies              create → provisions (ledger schema + workdir)
GET    /v1/workspaces/{ws}/companies/{id}         state + workforce + goal-tree summary
POST   /v1/companies/{id}/employees               hire {name, role}
POST   /v1/companies/{id}/runs                    {directive, idempotency_key} → 202 queued
GET    /v1/companies/{id}/runs/{run_id}           status + counts + error
POST   /v1/runs/{id}/cancel                       → canceling (heartbeat honors)
GET    /v1/runs/{id}/events?after=<seq>           page the mirror (same shape as stream)
GET    /v1/companies/{id}/stream                  SSE per-company channel; Last-Event-ID = seq
GET    /v1/runs/{id}/artifacts · /logs            pointers into the durable stores
GET    /v1/companies/{id}/proposals               horizon funnel
POST   /v1/proposals/{id}/approve|reject          human governance door (same seam as the CEO)
POST   /v1/companies/{id}/export · /import        chorus workforce portability
GET    /healthz · /readyz
```

## Repo layout

```
src/company/            # composition root (exists; unchanged)
src/podium/
  settings.py  main.py  db/  auth/           # auth: jwt (per-company keys), api-keys, decide()
  workspaces/  companies/  runs/  events/    # domain packages: router/schemas/models/service
  conductor/                                 # worker entrypoint, CompanyGraph host, event mirror,
                                             # command loop, durable run-log store
tests/                  # wiring-parity (exists) + per-domain API tests + conductor tests
docker-compose.yml      # postgres + api + conductor
Dockerfile              # uv multi-stage; api and conductor share the image
```

## Milestones (each TDD; gate: ruff format+check · mypy --strict · pytest)

- **M0 — foundations, tenant-shaped from day one**: compose Postgres, settings, async
  engine/session, Alembic (naming conventions), structlog, `/healthz`+`/readyz`, workspaces
  table + tenant-scoped session helper (`SET LOCAL app.workspace_id`). Exit: green gate against
  real Postgres.
- **M1 — auth + tenancy hard walls**: users, api_keys, JWT with per-company derived signing
  keys, fail-closed actor middleware, central `decide()`, RLS policies on all tenant tables,
  per-company rate limit; companies CRUD (config only). Exit: cross-tenant access attempts fail
  in tests at BOTH the decide() and RLS layers.
- **M2 — conductor**: worker entrypoint hosting CompanyGraph per company; commands mailbox
  (start/cancel/stop, idempotent); runs mapped onto horizon directives/chorus goals; embedded
  dev mode. Exit: run goes queued→running→succeeded against a stub deployment; cancel honored.
- **M3 — event pipeline**: EventBus→events mirror (same-tx NOTIFY), per-company SSE with
  Last-Event-ID replay + tail, typed event names, durable run-log store (file + pointer).
  Exit: dashboard-shape client replays a full run after reconnect. Retires the hand-rolled
  dashboard servers in horizon/examples.
- **M4 — product doors**: artifacts index, proposals/governance endpoints, workforce views,
  company export/import (chorus copy_org through HTTP). Exit: latch-company demo end-to-end
  through HTTP only.
- **M5 — chorus PostgresLedger** (chorus repo, spec 12 made real): `LedgerConnection` Protocol,
  paramstyle shim, Postgres DDL migrations + per-dialect parity test, schema-per-company
  provisioning; podium `LEDGER_BACKEND` flag; chorus full suite green on both backends.
  Exit: a company runs end-to-end with its ledger in Postgres.
- **M6 — ship shape**: uv multi-stage Dockerfile, compose api+conductor+postgres, uvicorn
  workers (conductor 1/shard), readiness gates, watchdog surfacing (paperclip thresholds).
- **M7 — tenant-aware chorus ledger** (committed, chorus repo; full spec below): company_id on
  every ledger table, all companies in ONE shared schema, RLS-enforced isolation; retires
  schema-per-company (kept one release as rollback). Exit criteria in the spec.
- **Deferred**: Redis/broadcaster if LISTEN/NOTIFY saturates; OTel; object-store mirror for
  logs (pointer schema ships in M3, S3 wiring later); multi-shard conductor placement.

## M7 spec — tenant-aware chorus ledger (paperclip-style shared schema)

**Status: committed phase, not a maybe.** Lands in the chorus repo after M5/M6, behind
`LEDGER_BACKEND=postgres_shared`; schema-per-company (`postgres`) stays available for one
release as the rollback. dream/horizon/lattice untouched; podium drops schema provisioning and
`search_path` pinning when it flips.

### Why (recap)
Shared schema + `company_id` is the unbounded-scale shape: PgBouncer transaction mode without
`search_path` juggling, one migration run, trivial cross-company analytics, Citus-ready
(distribution column). Schema-per-company (M5) is the zero-engine-change on-ramp, not the
end state.

### ID strategy — global surrogate IDs (Option B, chosen)

**IDs are never human-readable.** Every ledger row gets a globally-unique generated ID —
Stripe-style prefixed ULIDs (`emp_01J…`, `task_01J…`, `run_01J…`, `goal_01J…`): sortable by
creation time (ULID), self-describing in logs and API payloads (prefix), collision-free across
companies by construction. PKs and FKs stay single-column; no composite-key ripple.

The scope is narrower than it looks: chorus's task/run/artifact/goal IDs are **already
generated strings** — the only human-chosen IDs today are employees (`"ceo"`, `"bex"`) and the
company id. Those become:

- `employees.id` = generated `emp_…`; new `handle` column, unique per `(company_id, handle)`
  (`"ceo"`, `"bex"` live here). `hire()` generates the id and takes the handle.
- `companies` in podium get `cmp_…` ids + a `slug` for URLs.
- **Handle→id resolution happens at the boundaries only**: podium API accepts either
  (`/employees/emp_01J…` or `?handle=ceo`), briefs/AGENTS.md keep speaking handles (they're
  prose for models), and the factory resolves handle→id once at materialize. Kernel internals
  (`BeatContext.employee_id`, capability tools, delegation contracts, landers) carry the
  generated id — they already treat it as an opaque string, so they're unchanged.
- Worktree/asset directory names use the handle (readable on disk), not the id.

Uniques that encode business identity stay composite where they must
(`(company_id, handle)`, `(company_id, origin_fingerprint)`, `(company_id, routine_key)`);
plain PKs don't need company_id since IDs can no longer collide — but **every table still
carries `company_id`** for scoping, indexes, and RLS.

Rejected alternative — composite keys `(company_id, readable_id)`: preserves current IDs with
zero rename work, but ripples composite FKs through all 27 tables and leaks per-company ID
semantics into every join; surrogate IDs are the industry norm (paperclip: uuid PKs) and what
the podium API should expose anyway.

### RLS policies — hard requirement of this route

- Every ledger table: `ENABLE ROW LEVEL SECURITY` + `FORCE ROW LEVEL SECURITY` (applies to the
  owner too); one policy per table:
  `USING (company_id = current_setting('app.company_id')) WITH CHECK (same)`.
- The Postgres ledger handle sets `SET LOCAL app.company_id = <id>` at the top of every
  `transaction()` — automatic, not caller-visible; a session without the setting matches zero
  rows (fail closed).
- Red-team tests ship with the phase: a deliberately unscoped query through a scoped session
  returns only that company's rows; a cross-company INSERT/UPDATE is rejected; two companies
  run the latch demo concurrently in one schema with zero cross-talk.
- SQLite backend has no RLS: the handle scoping + this test suite is its guard (dev-only).

### Scoping audit (the real risk surface)

Every kernel SQL site gains `company_id` scope + a pinning test; RLS catches misses at runtime.
Checklist to enumerate at spec-execution time: scheduler dispatch ordering, liveness sweeps
(stranded todo / blocked-no-blocker), recovery + monitors, budget scans, routine firing,
decomposition claims, capability idempotency lookups. Rule: a query without company scope is a
review-blocking defect even though RLS would mask it.

### Migration path from per-company schemas (M5 → M7)

1. **v2 baseline migration** (applies to BOTH fresh shared schemas and existing per-company
   schemas): add `company_id NOT NULL` to all ~27 tables, rewrite PKs/uniques/FKs composite,
   rebuild indexes company-prefixed. In a per-company schema the backfill is trivial —
   `UPDATE t SET company_id = '<the schema's company>'`.
2. **Consolidation tool** (`podium conductor migrate-company <id>`): pause the company →
   mint new prefixed-ULID ids for rows still carrying human-chosen ids (employees: old id
   becomes the `handle`) and build the old→new map → parents-first
   `INSERT INTO shared.t SELECT … FROM company_<id>.t` with FKs remapped through the map
   (FK-safe order; the `_parents_first` codec order already exists in chorus workforce) →
   verify row counts + per-table checksums → flip `companies.ledger_ref` to shared → resume →
   `DROP SCHEMA` after a soak window. One company at a time; idempotent; abort = nothing
   flipped.
3. **Flag lifecycle**: `postgres_shared` ships dark → migrate internal companies → default for
   new companies → migrate remainder → remove schema-per-company mode next release.

### Exit criteria

- chorus full gate green on `sqlite` AND `postgres_shared` backends.
- RLS red-team suite passes; scoping-audit checklist 100% with per-site tests.
- Two companies concurrently on one shared schema through podium, zero cross-talk (extends the
  latch demo).
- Consolidation tool round-trips a real per-company schema (counts + checksums verified).

### Scale path (for the record)
Partitioned events + blobs-out (M3) → read replicas → Citus on `workspace_id`/`company_id`
(enabled by this phase) → multi-cluster sharding via `companies.cluster_url` routing with the
conductor-per-shard seam. Scale-out is routing work, not redesign.

## Decisions locked by v2

1. **Multi-tenant native**: workspace_id + composite indexes + RLS + per-company signing keys +
   per-company channels/rate limits from the start — not bolted on.
2. **Chorus ledger runs on Postgres** via the spec-12 seam: schema-per-company at M5 as the
   zero-engine-change on-ramp, then **tenant-aware shared schema at M7 (committed)** — the
   paperclip end-state with RLS enforcement, globally-unique prefixed-ULID ids (handles for
   humans), and a per-company consolidation migration. SQLite remains the dev/local backend
   throughout.
3. Events in Postgres, blobs out (file/object store + pointers) — paperclip's split.
4. SSE (not WebSockets), resumable via `(run_id, seq)`; per-company channels with typed events.
5. No Temporal/celery/arq: chorus's idempotent ledger + heartbeat is the durable-execution
   layer; podium mirrors and commands, never re-orchestrates.
6. Fail-closed actor middleware + central pure `decide()` — inverting paperclip's flagged
   fail-open weakness.
