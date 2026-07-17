# M4 — the Company Control Plane: system design

*2026-07-17 · status: proposed · supersedes the stale M4 "product doors" bullet · grounds in
chorus-system-architecture-v2.svg + the current facades of dream/chorus/lattice/horizon*

The plan's M4 ("artifacts, proposals, workforce views, export/import") predates the **M8 delegation
merge** and never covered **lattice**. This redesign exposes *every* product-relevant capability of
all four engines over HTTP — through **one seam**, modelled on how the engines already talk to each
other.

## 0. What the architecture SVG tells us (the load-bearing idea)

```
horizon ──(dream.contracts: Strategy + Delegation + Governance ports)──►  chorus
  decides what's next          THE ONLY CROSS-REPO SURFACE               runs the org
      │                              wired by the                          │  embeds:
      └──────────── composition root (chorus_bridge / src/company) ────────┤   dream  (harness: build_harness)
                                                                           └   lattice (chorus_tools: recall · skill_manage)
```
Three facts to copy:
1. **The engines never import each other** — they meet at `dream.contracts` (typed Protocols), wired
   by **one composition root**. podium already respects this: `src/company.build()` is that root.
2. **chorus is the hub at runtime** — it *embeds* dream (every beat runs the harness) and *reaches*
   lattice (beats recall memory + evolve skills through `chorus_tools`). So dream & lattice are not
   peers to surface directly; they're reached **through chorus** (harness/skills) or **beside it**
   (lattice's own facade over the same episodic store).
3. **The "new / thick-border" boxes** — Management authority, Delegation kernel, Teams+leads — are the
   M8 merge. They live in chorus and are already wired in `src/company` (`DelegatedIntakeAdapter`,
   `CapacityAdapter`). The kernel can *run* delegated work today; there's just no HTTP door.

**The design principle M4 adopts:** podium gets **one composition root of its own** — a
`CompanyControlPlane` — that plays the same role for the *product* that `src/company` plays for the
*engine*. It is the single place that composes the four engines' facades into a product surface;
every HTTP router is a thin, tenant-scoped, governed mapping onto it. (podium builds its own; it takes
`src/company` as the pattern, and does not modify it.)

## 1. Requirements

### Functional — expose the real capabilities (audited from the facades)
| Area | Engine facade (real methods) | Product doors (M4) |
|---|---|---|
| **Direction** | `horizon`: list/explain/approve/reject_proposal · seed/decompose/submit_decision · submit/reprioritise/set_priority/archive/goal_view · note_outcome · state | proposals + goal tree + governance |
| **Workforce** | `chorus`: hire · terminate · assign · `workforce`/`status` (list, roles) | roster + hire/fire |
| **Delegation / Teams (M8)** | `chorus.submit(execution_mode=DELEGATION, lead, delegation_max_team_size, spend_limit)` · Teams+leads · Management authority · Delegation kernel (contracts·integrate·verify) · `CapacityAdapter` | delegated intake + teams + capacity + contracts views |
| **Work / runs** | `chorus.submit` · `tick` (already the conductor) | `POST /runs` (widened with execution_mode + delegation params) |
| **Learning** *(internal)* | `lattice`: gate_open · packet · has_fresh_episodes · context (read); `validate·apply·adjudicate·forget` fire **automatically** on the gate / inside beats | **read-only** under Observability: evolved skills + learning status. No product "evolve" door — evolution is not a user action. |
| **Observability** | `chorus.inspect`: task · stuck · scrum_packet · org_report + podium events/stream/logs (M3) | inspect + org report + artifacts index |
| **Portability** | `chorus.workforce.export/import_` · `copy_org` | company export/import |

### Non-functional
- **Thin HTTP, one composition point** — no router reaches into an engine directly (mirrors "one
  composition root"). New engine capability → one facade method, N routers unchanged.
- **Tenant-scoped + governed** — every door: auth → `decide()` → the *company's* ledger, RLS on the
  product DB, and the company-scoped chorus ledger.
- **Reads fast + sync; heartbeat-coupled writes go through the conductor** (see §3.2).
- **Additive** — reuses M1 auth/tenancy, M2 conductor, M3 events/stream/logs untouched.

### Constraints
- The **product DB** (workspaces/companies/runs/events — podium) and the **chorus ledger** (per
  company: tasks/teams/contracts/employees — the engine's own store) are **different databases**.
  Control-plane data (workforce, proposals, teams) lives in the **chorus ledger**, not the product DB.
- The **conductor owns the live `CompanyGraph`** (heartbeat + writes coupled to execution). The **api**
  serves HTTP. They are separate processes in prod → reaching per-company chorus state cross-process
  is the central problem (§3.2).

## 2. High-level design — the Control Plane seam

```
                       ┌──────────────────────────────────────────────┐
   HTTP (M4 routers)   │ podium.control.CompanyControlPlane (per co)   │
   thin + governed  ──►│   .direction   → horizon facade              │
   auth · decide()     │   .workforce   → chorus facade               │
                       │   .delegation  → chorus (M8 teams/authority)  │
                       │   .work        → chorus.submit → podium runs  │
                       │   .learning    → lattice facade              │
                       │   .observe     → chorus.inspect + M3 events   │
                       │   .portability → chorus export/import         │
                       └───────────────┬──────────────────────────────┘
                                       │ built from
                            ┌──────────▼───────────┐
                            │ src/company.build()  │  ← the ENGINE composition root (unchanged)
                            │  CompanyGraph:        │
                            │  org·horizon·gov·     │   + podium reaches lattice + delegation
                            │  factories            │     ports the graph already wires
                            └──────────┬───────────┘
                       ┌───────────────┴───────────────┐
                    chorus ledger (per company)   dream.contracts ports
                    tasks·teams·contracts·         (strategy·delegation·governance)
                    employees·episodes             wired by the root
```

`CompanyControlPlane` **is podium's composition root** — it takes the engine's `CompanyGraph` and
adds the two surfaces the graph wires but doesn't expose as one object: **lattice** (built beside the
graph over the same episodic store) and the **delegation/teams** views. It presents the whole thing as
sub-facades that HTTP routers map to 1:1.

## 3. Deep dive

### 3.1 The facade (blend of `src/company` + podium)
```python
# podium/control/_plane.py  (podium-owned; src/company is the pattern, not modified)
@dataclass
class CompanyControlPlane:
    graph: CompanyGraph          # from src/company.build()
    lattice: Lattice             # built beside the graph over the company's episodic store
    workspace_id: str
    company_id: str

    @property
    def direction(self) -> DirectionFacade: ...   # wraps graph.horizon
    @property
    def workforce(self) -> WorkforceFacade: ...    # wraps graph.org (hire/terminate/list/status)
    @property
    def delegation(self) -> DelegationFacade: ...  # graph.org submit(DELEGATION) + teams/authority/capacity
    @property
    def observe(self) -> ObserveFacade: ...        # graph.org.inspect + podium events + lattice READS
    @property
    def portability(self) -> PortabilityFacade: ... # graph.org.workforce export/import
```
`lattice` is held on the plane but has **no write facade** — like `dream`, it's internal (beats reach
it via `chorus_tools`; evolution fires automatically on the gate). `observe` surfaces its *read-only*
telemetry (evolved skills, learning status); nothing exposes `validate/apply/adjudicate/forget` as a
product action.
Each sub-facade is **pure delegation** to the engine facade + translation to podium DTOs (pydantic) —
no business logic, no leaking of engine internals (`graph.org._ledger` never escapes a router).

### 3.2 The cross-process problem & the CQRS split (the crux)
Control-plane state lives in the **per-company chorus ledger**, which the **conductor** owns live. The
api can't just hold a second live `CompanyGraph` (two heartbeats / two sqlite writers = corruption).
Resolution, by operation kind:

| Kind | Examples | Where it runs |
|---|---|---|
| **Reads** | list workforce · proposals · goal tree · teams · org_report · skills | api opens a **read** `CompanyControlPlane` over the company ledger (no heartbeat) |
| **Ledger writes, execution-independent** | hire · approve/reject proposal · set priority · seed decision | api (short chorus-facade txn on the ledger) |
| **Heartbeat-coupled** | submit directive→run · cancel · delegated intake that spawns beats | **conductor** (already: podium runs + commands mailbox) |

- This is only safe when **api + conductor can share the ledger concurrently** — which is exactly what
  the **Postgres ledger (M5)** provides (schema-per-company, transactional). **So M4's clean form
  depends on M5.**
- **Interim (pre-M5, sqlite):** avoid concurrent sqlite writers — route *all* control-plane ops
  through the conductor via a **request/response control channel** (extend the M2 `commands` mailbox
  with a correlation id + a result row the api awaits). Slower, but correct. Reads too.
- **Recommendation: do M5 before the full M4**, or ship M4 read-only + heartbeat-writes first
  (works today through the conductor) and add ledger-writes when M5 lands.

### 3.3 API surface (v1, all auth + `decide()` + tenant-scoped)
```
# Direction (horizon)
GET/POST /v1/companies/{id}/proposals · POST /v1/proposals/{id}/approve|reject · GET .../proposals/{id}
GET      /v1/companies/{id}/goals            (goal tree)      · PATCH /v1/goals/{id}   (priority/archive)
POST     /v1/companies/{id}/decisions · POST /v1/decisions/{id}/decompose
# Workforce (chorus)
GET/POST /v1/companies/{id}/employees        (roster / hire)  · DELETE /v1/employees/{id} (terminate)
GET      /v1/companies/{id}/roles
# Delegation / teams (chorus M8)
POST     /v1/companies/{id}/runs             {execution_mode, lead, max_team_size, spend_limit_cents}
GET      /v1/companies/{id}/teams · GET .../teams/{id} · GET /v1/companies/{id}/capacity
# Observability + artifacts (chorus.inspect + lattice READS + M3 events)
GET      /v1/companies/{id}/report           (org_report) · GET /v1/runs/{id}/stuck
GET      /v1/companies/{id}/skills · GET .../employees/{eid}/learning   (evolved skills · gate open? · read-only)
GET      /v1/companies/{id}/artifacts · GET /v1/runs/{id}/artifacts
# NOTE: no POST /evolve — skill evolution is automatic (lattice gate), not a product action.
# Portability
POST     /v1/companies/{id}/export · POST /v1/companies/{id}/import
```

### 3.4 Governance is first-class (not an afterthought)
The SVG's **GovernancePort (read·approve·steer)** and **Management authority (grants·staffing·plans)**
map onto podium's `decide()`: approving a proposal, hiring, or granting delegation authority are
`decide(actor, "approve"|"hire"|"grant", resource)` checks *before* the facade call — the same
fail-closed door as everything else. The CEO-beat governance seam (chorus) stays internal; the human
governance door is these endpoints.

## 4. Scale & reliability
- **Reads** are ledger queries (indexed, per-company) — cheap; the api scales horizontally (each
  worker opens its own read connection).
- **Heartbeat-coupled writes** funnel through the conductor (one live graph per company; sharding
  preserved). The control channel is bounded + idempotent (reuse the commands-mailbox discipline).
- **Failure modes**: a control-plane read failing → 503 (ledger unreachable); a routed write timing
  out → the command stays queued (idempotent), the api returns 202/pending. Everything degrades to
  "the ledger is the truth; retry/reconnect."
- **Export/import** are heavy (whole workforce) — 202 + async job (reuse the runs/command pattern),
  not a synchronous request.

## 5. Trade-offs & what I'd revisit
| Decision | Choice | Revisit |
|---|---|---|
| One product composition root (`CompanyControlPlane`) | yes — mirrors `src/company` | if a capability needs to bypass it for perf, add a read model, don't leak `graph.org._ledger` |
| Reach control-plane state | CQRS: api reads the ledger, conductor does heartbeat writes | needs **M5 (Postgres ledger)** for concurrent api+conductor; interim = route through the conductor |
| Expose dream / lattice **writes**? | **No — both are internal.** dream runs inside beats; lattice evolves automatically on the gate. Surface only **read-only** telemetry (skills, TDD-gate, learning status) under `observe`. | if an admin ever needs `forget`/force-adjudicate, add a *narrow governed* door — not a core facade (same bar as a dream "dry-run") |
| Delegation params on `POST /runs` | widen the existing endpoint (one run resource, execution_mode discriminates) | split to `/delegations` if the shapes diverge too far |
| Build order | **M5 then M4**, or M4-read-first | if M5 slips, ship M4 reads + heartbeat-writes now |

## 6. Delivery slices (each TDD; gate: ruff·mypy --strict·pytest on real Postgres)
- **M4a — the seam**: `podium.control.CompanyControlPlane` + sub-facade Protocols + DTOs; a
  `ControlPlaneProvider` that builds a **read** plane over a company ledger (hermetic: a temp sqlite
  ledger seeded via chorus, asserted through the facade — no model).
- **M4b — direction**: proposals + goals + governance endpoints (approve/reject behind `decide`).
- **M4c — workforce + delegation**: roster/hire/terminate; widen `POST /runs` with execution_mode +
  delegation params; teams/capacity views.
- **M4d — observability + portability**: org_report, artifacts index, **lattice read-only** (evolved
  skills + learning status), export/import (async). *(No learning-write slice — evolution is internal.)*

**Exit (M4):** the latch-company demo runs **end-to-end over HTTP** — set direction → approve a
proposal → hire/delegate → run → watch events/logs → inspect the org report + evolved skills → export
— with delegation exposed and learning/skills **observable** (not driven; evolution stays internal).
