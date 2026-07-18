# The Arceus Cockpit — waku-shaped observability over the five-repo engine

*2026-07-17 · status: building · shape reference: waku-agent's ops cockpit (one snapshot API +
SSE + a live clickable architecture SVG) · structure reference:
`~/Downloads/chorus-system-architecture-v2.svg` + the delegation-flow whiteboard · corrects the
program doc: lattice provides **semantic memory (atoms)** AND **procedural skills**; episodic
records come from chorus.*

## 1. Placement & the company refactor

- New package **`src/cockpit`** — outside `src/podium`, beside it. Pure visibility.
  (Revised 2026-07-18: the engine composition root first moved here as `cockpit/company`,
  then to **`podium/conductor/company`** — its only production caller is the conductor, and
  housing the runtime in the cockpit made it read as a second control plane.)
- The cockpit app mounts into podium's FastAPI app (one server, same auth walls:
  actor → decide() → company visibility). podium keeps ownership of control doors (runs, goals,
  hire…); the cockpit adds **visibility only**.

## 2. The visibility rule (from the goal)

Internal components get **no control APIs** — only read models:

| Component | Internal? | Cockpit exposure |
|---|---|---|
| horizon generation/decisions | internal (reasoner-gated) | counts + goal tree (existing /goals) |
| dream.contracts ports | internal seam | edge in the map, lit by traffic |
| Chorus facade / scheduler / ledger | control via podium doors | counts, allocation, lanes |
| Delegation kernel · teams · authority | **internal** | tree view (photo flow) from teams/contracts tables — read-only |
| dream harness (plan→generate→evaluate) | **internal** | beat spans from run events — read-only |
| chorus EpisodicStore (FTS5) | internal store | per-employee episodic counts + retrieval feed (memory.retrieved) |
| lattice gate → **semantic atoms** | **internal** (auto on gate) | atoms per employee {key, activation, sources} — read-only |
| lattice → **skills** (procedural) | internal (skill_manage in-beat) | skill HEADs + revisions (existing ledger read) |
| LLMOps (spend, tokens, evals, watchdog) | telemetry | /costs, counts, run.stalled feed |

## 3. API surface (waku's one-snapshot pattern)

```
GET /v1/workspaces/{ws}/companies/{cid}/cockpit/snapshot
  → { components: { horizon:{goals,proposals}, contracts:{delegated_runs},
      chorus:{employees,open_tasks,running,queued_wakes,blocked},
      delegation:{teams,contracts_active,staffing}, harness:{runs_active,tool_calls_24: n/a→counts},
      episodic:{records,by_employee}, semantic:{atoms,by_employee}, skills:{heads,revisions},
      llmops:{spend_cents,llm_calls,input_tokens,output_tokens,stalled} },
      delegation_tree: [...photo-flow nodes...], generated_at }
```
One endpoint feeds every nav count and section header; section detail reuses the CP-2/4 doors
(/workforce /allocation /costs /artifacts /skills /goals /teams). The SSE stream (M3) stays the
live wire; the UI demuxes it into lane/edge lighting exactly as waku lights its diagram.

Semantic atoms read: via the lattice facade built beside the graph (workdir-local — the same
shared-FS caveat as /logs; fine dev-embedded, read-mirror later).

## 4. UI (zero-build, waku layout)

- Left **nav** with live counts: Overview · Direction · Org · Work · Runs · Memory (Episodic /
  Semantic / Skills) · Delegation · LLMOps · Ops · a **run dock** (directive bar) always present.
- **Overview = the live architecture map**: an SVG mirroring chorus-system-architecture-v2 —
  horizon (funnel→decision→goal tree) → dream.contracts strip → chorus (facade · scheduler ·
  ledger · EventBus · harness factory · delegation kernel · teams) → dream harness box
  (plan→generate→evaluate) → lattice box (episodic → gate → semantic atoms ⊕ skill evolve).
  Every node clickable → its section; edges carry `data-edge` ids and light up as SSE events
  arrive (event-type → edge map), waku-style.
- **Delegation section** renders the whiteboard flow: Founder intent → Horizon evidence/goals →
  CEO workforce proposal → Human approve/revise → Permanent workforce → Goals → Mission Teams →
  Verified completion → Archive, with the staffing-request → CEO-amendment loop — populated live
  from teams/delegation-contract/staffing tables.

## 5. Build order

CK1 this doc · CK2 company→cockpit move + app scaffold · CK3 snapshot API (incl. semantic atoms)
· CK4 UI shell + sections · CK5 live architecture map + delegation flow · CK6 browser-verified.

## 6. The company OS loop (CO1–CO3, feat/company-os)

The T2 formation example productized — "I give an idea; the CEO does most of the thing; at
critical decisions my approval is asked":

- **CO1 formation runs**: `execution_mode: "formation"` routes the directive to a real CEO
  employee (Casey, hired idempotently beside Ace). Her harness carries the ledger-bound
  `workforce_catalog_read` / `workforce_plan_propose` tools; invalid drafts are refused typed
  (the engine names the hireable professions). The conductor now runs the always-on chorus
  heartbeat (`org.start()` at ensure, `stop()` at aclose) — `PODIUM_CONDUCTOR_MAX_TICKS=0`
  means the executor watches forever: **infinite pulses**, the company keeps running.
- **CO2 the human boundary**: `GET /plans`, `POST /plans/{id}/approve|reject` — pure delegation
  to chorus's `WorkforcePlanService`; `decided_by` is the authenticated actor. Approve
  materializes employees + bounded management grants + budgets atomically, audited; reject
  leaves the workforce untouched.
- **CO3 the inbox**: the Delegation section shows pending plan cards (roster, budgets,
  confidence) with Approve/Reject; the "Human approve or revise" whiteboard node glows with a
  waiting count; the dock has a Formation toggle; the nav pill flags pending decisions.

Growth path (not yet built): generic approval gates (`GovernanceFacade.approvals()` inbox for
hire/board/plan gates), staffing-request amendments mid-delivery, and horizon proposal
approval — same doors pattern, same inbox UI.
