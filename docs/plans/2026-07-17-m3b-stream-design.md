# M3b — live wiring + SSE stream: system design

*2026-07-17 · status: proposed · depends on M3a (events mirror) · child of [the M3 design](2026-07-17-m3-event-pipeline-design.md)*

M3a built the durable log + the mirror + cursor paging. M3b makes it **live**: the mirror ingests the
real chorus `EventBus`, and browsers stream events over SSE with reconnect-safe replay. This delivers
the M3 exit — *a dashboard-shape client replays a full run across a reconnect, no gaps, no dupes.*

## 1. Requirements

### Functional
- **FR1** The mirror ingests every chorus `EventBus` event for a live company, attributes it to the
  right podium run (or company-level), and writes it (M3a mirror) — from the *real* engine.
- **FR2** `runs.engine_task_id` records the chorus root task so events route to runs.
- **FR3** `GET /v1/companies/{id}/stream` streams events live over SSE (`text/event-stream`).
- **FR4** Reconnect resumes via `Last-Event-ID` (= per-company `seq`): **replay missed, then tail** —
  no gap, no dupe.
- **FR5** Keepalive so idle streams survive proxies; graceful teardown on client disconnect.
- **FR6** Tenant isolation: a stream carries only the caller's workspace (auth + `decide` + RLS).

### Non-functional
- **Latency** event committed → browser < ~1s.
- **Durability/consistency** the table is the source of truth; the stream is a live view over it. A
  client can always reconnect and reconstruct exact state by replay. No exactly-once delivery over the
  wire is required — **at-least-once with client-side dedupe by seq**.
- **Scale (initial)** tens of companies, tens of viewers/company, a run emitting 10²–10³ events. One
  Postgres, `LISTEN/NOTIFY`, in-process fan-out per api worker. Redis is the later lever.
- **Backpressure** a slow viewer must not grow memory unbounded or stall others.

### Constraints
- chorus `EventBus.subscribe(cb)` is **sync** and may fire **off the event loop** (dream beats run in
  threads). `Event` carries `kind, at, task_id, employee_id, run_id (a beat, not our run), payload`.
- The mirror is the **single writer per company** (M3a). `pg_notify('podium_events', {company_id,seq})`
  fires in the same tx as the row.
- FastAPI/uvicorn + asyncpg + SQLAlchemy; api scales to multiple workers; conductor is separate.

## 2. High-level design

```
 CONDUCTOR process (ingest + write)                     API process(es) (read + stream)
┌───────────────────────────────────┐                 ┌──────────────────────────────────────┐
│ CompanyGraphHost                  │                 │ Broadcaster (1 / process)             │
│   graph.org.EventBus              │                 │   1 dedicated asyncpg conn            │
│      │ subscribe(sync cb)         │                 │   LISTEN podium_events                │
│      ▼                            │                 │   on notify {company_id,seq} →        │
│  call_soon_threadsafe ─► Queue    │                 │     fan out to per-client queues      │
│      │ (bounded; drop+count)      │  Postgres       │   reconnect w/ backoff → resync       │
│      ▼                            │  events(RLS,    │        │                              │
│  Drainer → route(task→run)        │  company_id,seq)│        ▼                              │
│    → EventMirror.record()  ───────┼──► INSERT+NOTIFY┼──► SSE  GET /companies/{id}/stream    │
│      [seq; outbox; single writer] │◄───────────────►│   subscribe→replay(seq>cursor)→tail  │
└───────────────────────────────────┘                 │   keepalive; disconnect→unsubscribe  │
        writes the log                                 └──────────────────────────────────────┘
```

**Two independent halves, connected only by the `events` table + one NOTIFY channel:**
- **Ingest** (conductor): chorus bus → threadsafe queue → drainer → mirror (writes rows + NOTIFY in
  one tx — the atomic outbox).
- **Egress** (api): Broadcaster (`LISTEN` → fan-out) + the SSE endpoint (replay + tail). Kept behind a
  thin seam so a `RedisBroadcaster` is a drop-in **later** when multi-worker fan-out saturates
  Postgres NOTIFY — not now (see §5).

The table is the contract; neither half calls the other. Everything degrades to *reconnect and replay
from the durable table*.

## 3. Deep dive

### 3.1 Ingest: chorus EventBus → mirror
- **Subscribe** once per live company graph. The sync callback does the *minimum*:
  `loop.call_soon_threadsafe(queue.put_nowait, event)` (loop captured at subscribe time — beats fire
  from threads). It must not block the beat and must not touch async DB directly.
- **Bounded ingest queue** (e.g. 10k): on `QueueFull`, **drop-newest + increment a counter** and log.
  We *cannot* backpressure the producer (a beat is chorus's critical path), so an extreme burst drops
  telemetry — consistent with the M3a "best-effort tap" boundary (run lifecycle stays exactly-once via
  M2; only some telemetry rows may be lost under overload).
- **Drainer** (one async task per company): `await queue.get()` → translate `Event` →
  `mirror.record(type=kind.value, payload, task_id, employee_id, at)`. Per-event insert (one NOTIFY
  each) for a responsive stream; batch insert is a later throughput lever.
- **Routing** `task_id → run_id`: the mirror holds an in-memory `register_run(root_task_id, run_id)`
  map; the drainer resolves an event's `task_id` to a root by walking parents via the ledger **once**,
  memoizing the whole chain (avoids an N+1 ledger walk). Unknown lineage → `run_id NULL`
  (company-level).
- **`engine_task_id` — write AND read (migration 0007).** The in-memory map is *not* the durable
  record; `runs.engine_task_id` is. The loop is:
  - **write:** when the executor submits a directive it persists `runs.engine_task_id = task.id` (a
    short tenant_session write) and calls `register_run(task.id, run_id)`.
  - **read (rehydration) — the reason the column exists:** when the conductor (re)hosts a company
    (fresh start, crash recovery, or company migrating to another shard), the in-memory map is empty.
    Before draining events it **rebuilds the map from the column**:
    `SELECT id, engine_task_id FROM runs WHERE company_id=$c AND status NOT IN (terminal) AND
    engine_task_id IS NOT NULL` → `register_run` each. Without this, events for a run that was in
    flight at restart resolve to `run_id NULL` and are mis-attributed. (Uses the existing
    `ix_runs_company_id_status` index.)

**Ownership:** the *conductor* owns a `dict[company_id → CompanyStream]` = `{mirror, unsubscribe,
drainer_task, queue}` (the conductor has the app sessionmaker; the host provides the graph+bus). The
stream is created on the company's first run, torn down on graph eviction.

### 3.2 Egress: the Broadcaster (LISTEN → fan-out)
- **One per api process**, owning a **dedicated asyncpg connection** (not the SQLAlchemy request
  pool). `LISTEN podium_events`. On notify, parse `{company_id, seq}` and `put_nowait` onto every
  subscriber queue for that company; on a subscriber's `QueueFull`, flag it **overflowed** (the SSE
  loop will end that stream).
- **Registry** `dict[company_id → set[Subscription]]`; `subscribe(company_id) → Subscription(queue,
  unsubscribe)` with a **bounded** queue.
- **Reconnect** (the one place backoff belongs): a supervisor reconnects the LISTEN connection with
  exponential backoff on drop and re-`LISTEN`s. On reconnect it pushes a **resync** signal to all
  subscribers → each SSE loop does one catch-up query from `last_emitted`. Between drops, Postgres
  delivers NOTIFY reliably to a connected listener, so **no steady-state polling is needed** — resync
  covers the only gap window.
- Started/stopped in the api **lifespan**.
- **Kept behind a thin `Broadcaster` interface** (`publish`/`subscribe`) so a `RedisBroadcaster` can
  drop in when multi-worker fan-out actually saturates Postgres NOTIFY (§5) — Postgres already crosses
  processes and keeps the atomic outbox, so Redis is a *future* lever, not a now cost.

### 3.3 The SSE endpoint + replay/tail algorithm
`GET /v1/companies/{id}/stream` → `StreamingResponse(gen(), media_type="text/event-stream")`.
```
auth: require_actor → decide(read, company) → verify path company_id == actor scope
gen():
  sub = broadcaster.subscribe(company_id)          # 1. buffer live FIRST (closes the gap)
  cursor = last_emitted = int(Last-Event-ID or ?after= or 0)
  try:
    # 2. REPLAY — chunked so a 10^4-event run doesn't load into memory
    while True:
      rows = SELECT … WHERE company_id=$c AND seq>cursor ORDER BY seq LIMIT 500   # tenant_session → RLS
      for r in rows: yield sse(r); last_emitted = r.seq
      cursor = last_emitted
      if len(rows) < 500: break
    # 3. TAIL
    while True:
      try: msg = await wait_for(sub.queue.get(), timeout=15)
      except TimeoutError: yield ": ping\n\n"; continue        # keepalive
      if sub.overflowed: break                                 # slow client → end; it reconnects+replays
      if msg is RESYNC or msg.seq > last_emitted:
        rows = SELECT … WHERE company_id=$c AND seq>last_emitted ORDER BY seq   # range read; covers coalesced notifies
        for r in rows: yield sse(r); last_emitted = r.seq
  finally:
    sub.unsubscribe()                                          # 4. disconnect → free the queue
```
- **`sse(r)`** = `id: {seq}\nevent: {type}\ndata: {json(payload+ids)}\n\n`. `id:` = seq → the browser
  resends it as `Last-Event-ID` on reconnect.
- **Dedupe is trivial** because seq is per-company monotonic and single-writer: emit only `seq >
  last_emitted`. The subscribe-before-replay ordering + the range read on tail guarantee no event
  falls between replay and tail.
- **RLS** scopes every read to `actor.workspace_id`; the `company_id` filter + RLS both apply.
- Optional `?run_id=` filters emitted events server-side (still cursored by company seq).

### 3.4 API contract
```
GET /v1/companies/{id}/stream            200 text/event-stream
  headers: Last-Event-ID: <seq>  (or ?after=<seq>)   → resume point
  frames:  id: <seq> / event: <type> / data: <json>  ;  ': ping' keepalives
GET /v1/runs/{id}/events?after=<seq>     (M3a) — the poll-mode twin, same event shape
```

## 4. Scale & reliability

- **Load:** per api process = 1 LISTEN conn + Σ(subscribers). Fan-out is O(subscribers/company) per
  notify — trivial at tens of viewers. Ingest = one insert + notify per event; a busy run 10²–10³
  events is comfortable for one Postgres.
- **Scaling & the real bottleneck:** every api process `LISTEN`s the **single global channel**, so
  each processes *all* companies' notifies even for companies it has no local viewers for. Fine now;
  at high event-rate × many workers it's wasted work. **Levers, in order:** (a) per-company channels +
  dynamic `LISTEN` (listen only companies with a local viewer); (b) Redis pub/sub with per-company
  topics (cross-process fan-out). Conductor scales by sharding (single writer per company preserved).
- **Failure modes — everything degrades to reconnect+replay:**
  | Failure | Effect | Recovery |
  |---|---|---|
  | Broadcaster LISTEN drops | live notifies missed | reconnect+backoff → **resync** → SSE catch-up query |
  | api process crashes | its streams drop | clients reconnect to another worker → replay from table |
  | SSE client slow | its queue overflows | flagged → stream ends → client reconnects + replays |
  | Ingest queue overflow | some telemetry rows dropped | best-effort tap; lifecycle safe (M2); metric emitted |
  | Mirror/conductor crash | tap gap (M3a) + routing map lost | reseed seq, resubscribe, **rehydrate routing from `runs.engine_task_id`**; lifecycle exactly-once via M2 |
- **Auth lifetime:** validated at connect; a long stream isn't re-checked. Trade-off — cap stream
  lifetime (force periodic reconnect+re-auth) later; deferred.
- **Monitoring:** active SSE connections, subscribers/company, ingest queue depth + drop counter,
  broadcaster reconnects, notify→emit latency, replay-query latency, dropped-slow-clients.

## 5. Trade-off summary & what I'd revisit
| Decision | Choice | Revisit trigger |
|---|---|---|
| Fan-out | Postgres LISTEN/NOTIFY, in-process per worker | per-company dynamic LISTEN, then Redis, when NOTIFY volume/worker hurts (already crosses processes today) |
| Rate limiter | M1 in-memory (per-process) | grants N× budget across N workers — a Postgres- or Redis-backed limiter when precise multi-worker limits matter |
| Ingest overload | bounded queue, drop-newest + count | batch inserts / larger buffer if telemetry loss shows up |
| Liveness gap | resync-on-reconnect (no steady polling) | add a per-company poll floor if NOTIFY proves lossy under load |
| Tail payload | NOTIFY = wake+cursor; SSE re-reads rows | inline small payloads in NOTIFY if the extra read is hot (mind 8KB) |
| Replay memory | chunked (LIMIT 500 loop) | server-side cursor if chunk overhead matters |
| Auth | connect-time only | capped stream lifetime + re-auth for revocation-sensitive tenants |
| Mirror placement | per-company, conductor-owned | revisit with the M2 per-run-tick executor if same-company concurrency grows |

## 6. Delivery slices (TDD; gate: ruff+mypy --strict+pytest on real Postgres)
- **M3b-1 — SSE stream (hermetic, = the M3 exit).** Broadcaster (LISTEN → bounded per-client queues,
  reconnect+resync); `GET /v1/companies/{id}/stream` (subscribe-first → chunked replay → tail →
  keepalive → disconnect cleanup). **Test with synthetic events via `EventMirror.record()`** (real
  `LISTEN/NOTIFY` on ephemeral PG, an httpx streaming client) — no chorus/model. **Exit:** replay a
  full run across a reconnect, no gaps, no dupes; slow-client disconnect; keepalive.
- **M3b-2 — live chorus wiring (integration).** `runs.engine_task_id` (migration 0007), **written on
  submit and read to rehydrate the routing map on (re)host**; per-company subscription in the
  host/conductor (sync cb → threadsafe queue → drainer → mirror), lineage routing. **Exit:** the
  real-model integration run produces a queryable + streamable event log; **plus a hermetic test that a
  new mirror rehydrates `register_run` from `engine_task_id` and correctly attributes a subsequent
  event** (proves the column is read, not write-only).
