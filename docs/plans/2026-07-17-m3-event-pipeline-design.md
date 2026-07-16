# M3 — event pipeline: system design

*2026-07-17 · status: proposed · depends on M2 (conductor) · supersedes the M3 sketch in the architecture plan*

The milestone that turns the conductor from "flips a run's status" into "streams the live play-by-play
to the dashboard, replayable across reconnects." This document resolves the open design questions
(seq cursor, event routing, partitioning) with explicit trade-offs.

## 1. Requirements

### Functional
- **FR1** Every chorus `EventBus` event during a live company is durably recorded as an append-only
  row, attributed to the correct podium run (or company-level when it has no run).
- **FR2** Events are totally ordered per company by a monotonic `seq`.
- **FR3** Cursor paging: `GET /v1/runs/{id}/events?after=<seq>` and `?after=` on the company view.
- **FR4** Live per-company SSE: `GET /v1/companies/{id}/stream`, resumable via `Last-Event-ID`.
- **FR5** Reconnect = replay-missed-then-tail with **no gaps and no dupes**.
- **FR6** Typed event names so the dashboard patches its cache surgically.
- **FR7** Transcripts/blobs live out-of-DB (file + pointer); `GET /v1/runs/{id}/logs`. *(M3c)*
- **FR8** Tenant isolation: a client sees only its workspace's events (auth + `decide()` + RLS).

### Non-functional
- **Latency** live event → browser < ~1s; full-run replay (10³ events) < a few seconds.
- **Durability** once a beat's event is persisted it is never lost (transactional outbox). The tap
  onto chorus's bus is best-effort (see §Reliability) — the authoritative run outcome stays the M2
  `runs` table.
- **Scale (initial)** tens of companies, a handful–tens of concurrent viewers per company, a run
  emitting hundreds–thousands of events. No Redis/Kafka — Postgres `LISTEN/NOTIFY` + in-process
  fan-out. Path to Redis when multi-worker fan-out saturates.
- **Cost** one Postgres; SSE over plain HTTP (no WS infra).

### Constraints (from the existing system)
- chorus `EventBus` is **sync-callback pub/sub** (`subscribe(cb: Callable[[Event],None])`) + a
  file-backed `replay(after=<isoformat>)` that re-reads the whole log per call. dream beats can fire
  callbacks **off the event loop** (threads).
- `Event` carries chorus ids: `kind, at, trace_id, task_id, employee_id, run_id, payload`.
  **`event.run_id` is a chorus *beat*, not a podium run.** A podium run ↔ a chorus **task**.
- One `EventBus` per `CompanyGraph`, shared by every directive in that company.
- The conductor hosts one `CompanyGraph` per active company; per-run **lease** = single owner (M2).

## 2. The key decision: what is the stream cursor?

`Last-Event-ID` on a per-company stream must be **one** value, but events belong to runs. Two options:

| | Per-run seq `unique(run_id,seq)` | **Per-company seq `unique(company_id,seq)`** |
|---|---|---|
| Stream cursor | ✗ ambiguous (stream multiplexes runs) → needs a composite cursor | ✓ single value = `Last-Event-ID` |
| Run paging | ✓ native | ✓ filter `run_id`, cursor by same seq (run's events are a monotonic subsequence) |
| Writer | single (per-run lease) | **single iff the mirror is per-company** |
| Ordering | trivial | trivial (single sequential writer) |

**Chosen: per-company `seq`, assigned by a per-company event mirror that is the single writer.**
This is the crux — it only works because the **mirror is a per-company component** (one per live
`CompanyGraph`, owned by the conductor hosting it), not a per-run one. A single writer per company
means: an in-memory monotonic counter (no `max()`/sequence contention), sequential commits (no
out-of-order-commit gap that plagues a global `bigserial` cursor), and one cursor that serves both
the company stream and run-filtered paging. It aligns with "conductor hosts one CompanyGraph per
active company" and the sharding model (one conductor per company at a time).

Rejected — **global `bigserial` cursor**: simplest to assign, but sequence gaps + out-of-order commit
mean a live tailer can advance past a row that commits later and never see it. Would need a
single-writer serializer anyway, so no simpler than the per-company mirror.

## 3. High-level design

```
 CONDUCTOR process (writer)                         API process(es) (readers)
┌──────────────────────────────┐                  ┌─────────────────────────────────┐
│ CompanyGraph (per company)   │                  │ SSE endpoint  /companies/{id}/   │
│   chorus EventBus (sync)     │                  │   stream                         │
│        │ subscribe(cb)       │                  │     │ 1. subscribe broadcaster    │
│        ▼                     │                  │     │ 2. replay: SELECT seq>cursor│
│  call_soon_threadsafe ──► asyncio.Queue         │     │ 3. tail: drain client queue │
│        │ (off beat's path)   │                  │     ▼                            │
│        ▼                     │                  │  Broadcaster (1 per process)     │
│  EventMirror (per company)   │   pg_notify      │   1 LISTEN conn, dynamic LISTEN  │
│   route task→run; seq++;     │◄────────────────►│   per active company;            │
│   INSERT events + NOTIFY  ───┼──► Postgres ─────┤   fan out → per-client queues    │
│   [one tx = outbox]          │   events table   │  (bounded; drop slow client)     │
└──────────────────────────────┘   (RLS, indexed) └─────────────────────────────────┘
        writes                                              reads + streams
```

- **Writer/reader split:** the conductor writes events; api processes only read (replay query) and
  tail (LISTEN). In embedded dev they share a process but stay separate components with their own
  connections.
- **Outbox:** `INSERT events` + `pg_notify` in one transaction → a NOTIFY never precedes its row.

## 4. Deep dive

### 4.1 Data model
```sql
CREATE TABLE events (
    company_id    text NOT NULL REFERENCES companies(id),
    seq           bigint NOT NULL,               -- per-company, assigned by the mirror
    workspace_id  text NOT NULL REFERENCES workspaces(id),
    run_id        text NULL REFERENCES runs(id), -- NULL = company-level event (no task)
    type          text NOT NULL,                 -- EventKind value, e.g. 'run.text'
    employee_id   text NULL,
    payload       jsonb NOT NULL,                -- excerpted for large text events (see 4.5)
    created_at    timestamptz NOT NULL,
    PRIMARY KEY (company_id, seq)
);
CREATE INDEX ix_events_run_id_seq   ON events (run_id, seq);      -- run-scoped paging
CREATE INDEX ix_events_company_seq  ON events (company_id, seq);  -- stream replay (== PK, explicit)
-- RLS: ENABLE + FORCE, USING/WITH CHECK (workspace_id = current_setting('app.workspace_id', true))

ALTER TABLE runs ADD COLUMN engine_task_id text NULL;  -- chorus root task id, for event routing
```
Partitioning is **deferred** (see trade-offs): a `PRIMARY KEY (company_id, seq)` is incompatible with
`PARTITION BY RANGE (created_at)` unless the PK includes `created_at`. The single-writer mirror already
guarantees `(company_id, seq)` uniqueness, so when volume warrants we convert to a partitioned table
with a non-unique `(company_id, seq)` index + monthly ranges.

### 4.2 Event routing (chorus → podium run)
When the executor submits a directive it records `runs.engine_task_id = task.id`. The mirror keeps a
per-company **`task_id → run_id` map**: an event is attributed to a run if its `task_id` equals a
known root task or a descendant (resolved once via the ledger, then cached). Events with no matching
task (e.g. `routine.fired`, company-level) get `run_id = NULL` and still stream. This caching avoids
an N+1 ledger walk per event.

### 4.3 seq assignment + the mirror loop
- **Seed** on mirror start: `SELECT COALESCE(max(seq),0) FROM events WHERE company_id=$1`.
- **Consume — push, never pull.** chorus `replay()` re-reads the whole file per call (O(n²)); the sync
  `subscribe` callback does the minimum: `loop.call_soon_threadsafe(queue.put_nowait, event)` (safe
  from dream's threads). An async **drainer** pulls the queue and per event: route → `seq += 1` →
  ```sql
  BEGIN;
    INSERT INTO events (company_id, seq, workspace_id, run_id, type, employee_id, payload, created_at) …;
    SELECT pg_notify('podium_events', json_build_object('company_id',$c,'seq',$s,'run_id',$r,'type',$t)::text);
  COMMIT;
  ```
  A single global `podium_events` channel (payload carries `company_id`) keeps the api's LISTEN to one
  connection; the broadcaster fans out by company. (Per-company channels are the alternative — better
  isolation, more LISTEN management; revisit if the global channel's wake volume hurts.)

### 4.4 SSE broadcaster + replay/tail
- **Broadcaster** (one per api process): a dedicated asyncpg connection `LISTEN podium_events`; on
  notify, push to each subscribed client queue whose company matches. Clients subscribe/unsubscribe as
  SSE connections open/close; **bounded queues** — a slow client that overflows is disconnected (it
  reconnects and replays). Keepalive comment (`: ping\n\n`) every ~15s so proxies don't reap idle
  streams.
- **The reconnect algorithm** (closes the gap — order matters):
  ```
  1. subscribe to the broadcaster for this company     (start buffering live)
  2. cursor = Last-Event-ID header (or 0)
  3. replay: SELECT … WHERE seq > cursor ORDER BY seq   (inside a tenant_session → RLS)
             emit each as SSE `id: <seq>`; track last_emitted
  4. tail: drain the buffered/live queue,
           emit only events with seq > last_emitted      (dedupe — trivial: seq is monotonic)
  ```
  Subscribing **before** the replay query is what prevents losing an event committed in the window.

### 4.5 Payload discipline ("events in PG, blobs out")
Lifecycle/status events (`run.started`, `task.status`, `run.done`, `run.evaluated`, `agent.status`)
mirror their payload in full. Chatty text events (`run.text`, `run.tool_result`) store an **excerpt**
in `payload`; the full text goes to the durable log store (M3c) with a pointer. Keeps the events table
telemetry-sized, not transcript-sized.

### 4.6 Endpoints
```
GET /v1/runs/{id}/events?after=<seq>&limit=<n>   200 → {data:[event…], meta:{next_after, has_next}}
GET /v1/companies/{id}/stream                    200 text/event-stream; resumes via Last-Event-ID
GET /v1/runs/{id}/logs                           200 → pointer / streamed file        (M3c)
```
Auth+`decide(read, company)`+RLS on all three. The stream verifies `path company_id == actor scope`
before subscribing, so the tail can't leak across tenants. Optional `?run_id=` filter on the stream.

## 5. Scale & reliability

- **Load:** a busy run ~10²–10³ events; N companies × few active runs. Postgres handles this
  comfortably; the hot path is the per-event single-row insert (indexed) + one NOTIFY.
- **Scaling:** api scales horizontally — each worker runs its own broadcaster + LISTEN connection; all
  receive the same NOTIFYs and fan out to their own clients. **This is the point Redis replaces** when
  the per-worker LISTEN fan-out or NOTIFY volume saturates (cross-process fan-out via Redis pub/sub or
  a broadcaster service). Conductor scales by sharding companies (one writer per company preserved).
- **Failover / the tap-crash gap:** the mirror is a best-effort tap. Events emitted-but-not-yet-drained
  when the mirror dies are lost (the async queue handoff keeps beats unblocked — the deliberate
  trade). On restart the mirror reseeds `seq` from `max` and re-subscribes; optional backfill via
  chorus's file `replay(after=<last_at>)`. **Run lifecycle stays exactly-once (M2 reclaim); only some
  telemetry may gap.** Acceptable for a dashboard; documented, not hidden.
- **Monitoring:** mirror queue depth + drain lag, dropped-slow-clients, broadcaster reconnects,
  events/sec per company, replay query latency.

## 6. Trade-off summary & what I'd revisit

| Decision | Choice | Trade-off / revisit trigger |
|---|---|---|
| Stream cursor | per-company `seq`, single-writer mirror | needs one writer per company (holds under sharding); revisit if a company ever needs multi-writer event ingest |
| Consume model | push (subscribe + threadsafe queue) | tap-crash gap on telemetry; add file-`replay` backfill if gaps matter |
| Channel | single global `podium_events` | simpler LISTEN; switch to per-company channels if wake volume hurts |
| Fan-out | in-process per worker | Redis pub/sub when multi-worker saturates |
| Partitioning | **deferred** | add monthly partitions when events table growth degrades queries |
| Payload | excerpt large text, blobs to log store | full transcript only via M3c pointer |
| Transport | SSE | one-way only; if the dashboard ever needs client→server on the same channel, revisit WS |

## 7. Delivery slices (each TDD; gate: ruff+mypy --strict+pytest on real Postgres)
- **M3a — mirror + cursor paging** *(hermetic)*: `events` table + RLS + `runs.engine_task_id`; the
  per-company `EventMirror` (route → seq → outbox insert+NOTIFY), fed by synthetic chorus `Event`s in
  tests (no real model); `GET /v1/runs/{id}/events?after=`. **Exit:** a run yields an ordered,
  gap-free, cursor-paged event log.
- **M3b — SSE stream + replay/tail**: broadcaster (LISTEN → bounded per-client queues), keepalive,
  `GET /v1/companies/{id}/stream` with `Last-Event-ID` replay-then-tail. **Exit (the plan's):** a
  dashboard-shape client replays a full run across a reconnect — no gaps, no dupes.
- **M3c — durable log store**: file writer, fill `runs.log_ref`/`log_sha256`, `GET /v1/runs/{id}/logs`;
  S3 mirror deferred. **Exit:** a run's transcript is tailable via the pointer.
