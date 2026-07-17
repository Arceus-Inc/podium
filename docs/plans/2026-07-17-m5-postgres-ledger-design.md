# M5 — the engine state store (Postgres): system design

*2026-07-17 · status: **built** (chorus ledger; lattice stores stay SQLite by design, below) ·
**folds the old M7 into M5** · unblocks the M4 control plane*

> **Landed** (branches: podium `feat/podium-m5-uuid`, chorus `feat/postgres-ledger`): podium ids →
> uuid (§6.1); chorus uuidv7 ids + dialect-neutral repos + `PostgresLedger` (native
> uuid/timestamptz/jsonb/boolean) + `company_id`+FORCE RLS + composite semantic identity (employee
> slugs) + driver-neutral `LedgerIntegrityError` + savepoint-per-write; 53-test conformance suite ×
> both drivers incl. no-leak, fail-closed, and same-company two-connection concurrency; podium
> migration 0008 (engine tables + grants), `CompanyConfig.ledger_dsn`, conductor `ledger_backend`
> routing, company ownership (`owner_user_id`, §2.5), and the provisioning saga (§3.5).
>
> **M5-L resolved (2026-07-17): skills → Postgres (`0002_skills`); episodic memory STAYS SQLite,
> by design.** The ledger needed Postgres because it is shared operational truth (multi-tenant,
> RLS-walled, read by api + conductor + beats). Episodic memory is the opposite shape —
> single-writer, company-workdir-local, read only by that company's beat processes, riding a
> filesystem that is already host-bound (worktrees, org repo, lattice consolidation, horizon's
> JSON stores) — and it is advisory learning telemetry, never correctness-bearing, with FTS5
> BM25 + snippets native; a Postgres port dissolves no constraint the workdir still imposes.
> The SKILLS store ported because it is the one lattice-side store where the DB is the source
> of truth (evolved skills rematerialize from it every beat): `skill` + `skill_revision` live in
> the shared engine schema (company_id + FORCE RLS) as `0002_skills` — the first delta in the
> authored migration stream (applied-set over the frozen baseline). Models/repos are first-class
> ledger citizens (`ledger.skills`, `ledger.skill_revisions`); `SkillStore(ledger)` is the domain
> facade. podium applies pending engine deltas generically in `migrations/env.py` (owner-role
> apply + record + exact-table grants) — a new chorus delta needs zero podium code. M4d's
> read-only learning doors keep the shared-FS constraint only for episodic reads (same as
> M3c `/logs`, same later fix). **Also open**: object-store log mirror.

The plan staged the chorus ledger port in two milestones: **M5** = PostgresLedger, *schema-per-company*
(zero chorus schema change); **M7** = tenant-aware *shared-schema* (`company_id` + FORCE RLS). This
doc collapses them: **M5 ships the shared-schema shape directly.** Schema-per-company is dropped as a
milestone and kept only as a per-company config escape hatch on the same driver seam.

**Scope: all engine persistence, not just the chorus ledger.** The same per-company shared-schema shape
holds chorus's ledger **and lattice's Memory store and Skill store** — every engine's state lives in the
one shared Postgres DB, isolated per-company by `company_id` + FORCE RLS. (This is a *persistence*
decision; it does not change M4's rule that lattice stays read-only at the product surface — where the
bytes live is orthogonal to which HTTP doors exist.)

**Ownership: a company belongs to a user.** `companies` gains `owner_user_id`; the engine state below a
company is transitively user-owned through `company_id` (see §2.5).

## 0. Why fold (the load-bearing argument)

The end-state was always shared-schema + RLS. So schema-per-company was never the destination — it was
a detour whose *only* benefit is "don't touch chorus's schema yet." That benefit is a debt, not a
saving:

| | schema-per-company first (M5→M7) | shared-schema now (fold) |
|---|---|---|
| when the schema change happens | later, on **N live per-company schemas** | now, on **empty tables** |
| the migration | a consolidation tool: backfill `company_id`, remap ids, FK-safe copy, checksums, per-schema fan-out | none — greenfield DDL |
| pooling | per-txn `search_path` juggling (PgBouncer footgun) | `set_config('app.company_id', …, true)` — same primitive podium already uses |
| tenancy model | two different shapes (podium: shared+RLS; chorus: schema-per-tenant) | **one** shape both contexts share |

Web consensus agrees with the direction: **start shared-schema + RLS unless a hard
compliance/residency/contractual-isolation need forces schemas** — most products never leave it;
schema-per-tenant develops real Postgres pain (catalog bloat, N×-table migrations, pooler `search_path`)
past a few thousand schemas. Podium has no such isolation requirement. Sources:
[PlanetScale](https://planetscale.com/blog/approaches-to-tenancy-in-postgres) ·
[Ali Asghar](https://aliasghar.me/blog/multi-tenant-saas-data-isolation) ·
[Aditya Agrawal](https://www.adiagr.com/blog/07-saas-postgres-multitenancy-patterns/).

**Symmetry is the real win.** The podium product DB is already shared-schema + FORCE RLS by
`workspace_id` under the non-superuser `podium_app` role. Making the chorus ledger shared-schema + FORCE
RLS by `company_id` gives both bounded contexts **one tenancy primitive, one test harness, one pooling
story** — instead of accidental two-shape complexity born only from deferral.

## 1. Requirements

### Functional
- chorus + lattice run against Postgres with **no behavioural change** — same semantics as today, on a
  Postgres-native store (not a SQLite-compatible shim).
- Every company's engine rows are isolated by `company_id`; a query can never see another company's rows
  (DB-enforced by RLS, not app-enforced).
- podium's api process and the conductor process both read/write one company's ledger **concurrently**
  (the thing SQLite-file-per-company cannot do across processes — this is what unblocks M4).
- Provisioning a company adds no DDL (shared schema); it seeds rows only.

### Non-functional
- **One tenancy primitive** shared with the product DB: `set_config` bind param, txn-local, FORCE RLS,
  non-superuser role.
- **Pooler-safe**: no session-level `search_path` state; scoping travels per-transaction.
- **Postgres-native, not a lowest-common-denominator shim.** chorus's ledger and lattice's stores are
  refactored to Postgres proper — `uuid`, `timestamptz`, `numeric`, `jsonb`, `ON CONFLICT`, partial
  unique indexes. **The SQLite ledger backend is retired**: keeping SQLite is exactly what forced the
  `SQLite ∩ Postgres` compromises (random `TEXT` ids, ISO-string times, no `jsonb`). The
  `LedgerConnection` Protocol stays *only* as the schema-isolation escape hatch (§5) — not a SQLite
  mirror.
- Ship behind `companies.ledger_backend` for rollout, but **Postgres is the only forward backend**; new
  companies are Postgres, existing ones migrate. Prove one company end-to-end before flipping default.

### Constraints
- The **two bounded contexts stay two schema families in one cluster/one database** (podium `public`
  RLS-by-workspace; engine tables RLS-by-company). **No cross-context FK** — engine rows link to podium
  by soft reference (`company_id`, no FK). CQRS via the conductor's runs/events mirror (M3), unchanged.
- chorus/lattice own their (now Postgres-native) DDL. podium *orchestrates* provisioning; it does not
  author engine tables.
- **`uuid` everywhere, both contexts.** podium's own ids are `sa.String()` today (M0–M3); doing Postgres
  properly means migrating them to `uuid` too, so `company_id uuid` in engine tables references a real
  `uuid` `companies.id`. That podium-side migration is an explicit prerequisite slice (§6.1), not a
  silent assumption — it reworks the merged 0001–0007 migrations, models, RLS casts, and `tenant_session`.

## 2. The shape

```
one Postgres database
├── podium product tables      RLS by app.workspace_id   (role: podium_app, FORCE RLS)   ── unchanged
│   workspaces · users · companies(+owner_user_id) · runs · events · commands · api_keys
└── engine state tables        RLS by app.company_id      (role: podium_app, FORCE RLS)   ── NEW in M5
    chorus ledger : tasks · teams · contracts · employees · goals · proposals · …
    lattice memory: episodes · recall index                (per-company Memory store)
    lattice skills: skills · versions                      (per-company Skill store)
    every table: id uuid PK (uuidv7) · company_id uuid NOT NULL · timestamptz · numeric · jsonb
                 · company_id-leading indexes · company-scoped unique · RLS (USING+CHECK)
```

Read/write path (identical to `tenant_session`, second axis):

```
conductor/api ──► engine txn ──► SET LOCAL app.company_id = :cid ──► chorus/lattice SQL ──► RLS filters to cid
```

The company is the finest tenancy grain for engine state; `company_id` is both the RLS key and the
Citus-ready distribution key. User ownership lives one level up, on `companies` (§2.5) — engine rows
inherit it through their `company_id`, so no per-user axis is needed on the engine tables.

## 2.5 Company ownership — user-level

A company belongs to the user who created it. `companies` gains `owner_user_id → users.id` (NOT NULL).

**The default I'm taking, and why:** the **workspace stays the hard tenant / RLS boundary** (a
workspace's data can never leak to another workspace — unchanged), and **user-ownership is an
authorization filter *within* the workspace**, not a second FORCE-RLS axis. Concretely:

- create: `owner_user_id = actor.user_id`.
- read/list/act on a company: allowed if `owner_user_id == actor.user_id` **or** the actor is a
  workspace admin. Enforced in `decide()` + a `WHERE owner_user_id = :uid` on non-admin list/reads.
- runs/events inherit ownership through `company_id`; the owner check happens at the company gate (a
  run is only creatable/readable for a company you own), so no `owner_user_id` denormalised onto
  runs/events yet — add it only if a hot query needs it.

Why authz, not a second RLS axis:
- RLS-by-workspace already gives the *hard* isolation guarantee; user-scoping within a workspace is a
  softer "whose is it" question that a `WHERE` + `decide()` answers cheaply.
- A workspace admin must see *all* members' companies — a per-user FORCE RLS policy fights that and
  forces bypass plumbing. Ownership-as-filter handles admin naturally.
- It avoids a second `set_config` axis on every product-DB transaction.

**The one fork (revisit if I've read the product wrong):** if users of the same workspace must be
*hard-isolated* from each other (a member physically cannot read another member's company rows even via
a bug), then user needs its own FORCE RLS policy (`app.user_id`) on `companies`/`runs`/`events`, and
admin cross-user views become an explicit elevated path. That's a heavier build; I've defaulted to the
lighter authz model because nothing stated requires per-user hard isolation. Say the word and I flip it.

> Actor→user note: this assumes the authenticated `Actor` carries `user_id`. API keys are
> workspace-scoped today; a key used for company ops must resolve to an owning user (key's creator, or
> a key bound to a user). That binding is the one auth-model touch this needs — surfaced, not yet built.

## 2.6 chorus ground truth — the refactor surface (read 2026-07-17)

I read chorus before deciding the shape. What's actually there, and why it forces a real port (not a shim):

- **`mint_id` = `"<prefix>_<12 hex of uuid4>"`** — random, **48-bit**, code comment: *"ample for
  per-org entity ids."* Not sortable, and **48 bits is unsafe as a global PK** (birthday collision
  across orgs ~16M rows). → replaced by `uuid` (`uuidv7()`): 128-bit, globally unique, time-sortable.
- **No `company_id` / `org_id` anywhere** in ~38 ledger tables — fully tenant-blind, isolated only by
  the SQLite file. → add `company_id uuid NOT NULL` + RLS to every table.
- **`id TEXT PRIMARY KEY`, single-column FKs** (`parent_id REFERENCES task(id)`). → `id uuid`,
  `uuid` FKs. Single-column still works — uuid is global, so no composite PK.
- **Uniqueness is company-local, some employee-local** — `routine(employee_id, routine_key)`,
  `artifact_revision(artifact_id, revision)`, and enum/fingerprint partial-uniques like
  `task(origin_kind, origin_fingerprint) WHERE origin_kind='horizon_intake'`. → id-anchored ones become
  global for free (uuid); the non-id-anchored ones get `company_id` prepended.
- **Timestamps are `TEXT` ISO strings** (`created_at TEXT NOT NULL`). → `timestamptz`.
- **The migration runner is SQLite-bound** — `sqlite3`, `PRAGMA foreign_keys`, `BEGIN IMMEDIATE`,
  table-rebuild `legacy_alter_table`. → rewritten to a Postgres migration mechanism.
- **Repo SQL is SQLite-dialect** — `?` params, `INSERT … OR IGNORE`, SQLite datetime, integer-bool. →
  ported to `$n`/`%s`, `ON CONFLICT DO NOTHING`, Postgres functions, `boolean`, `jsonb`.

This is a substantial refactor of chorus's data layer. That's the cost of "correct Postgres," and it's
accepted. lattice's Memory/Skill stores get the same treatment.

## 3. The load-bearing work (a real port, not a shim)

Paid **once, on empty tables, before any company runs on Postgres**.

1. **Postgres-native port.** Rewrite chorus's SQLite migration runner → Postgres migrations; port the
   ~38-table DDL and every `ledger/repos/*` SQL site off SQLite dialect (see §2.6). Do the same for
   lattice's stores. The `LedgerConnection` Protocol stays as the §5 escape-hatch seam — not to keep
   SQLite alive.
2. **Schema — Postgres-native types.** Every engine table: `id uuid PRIMARY KEY DEFAULT uuidv7()` (PG18
   native; global *and* time-sortable → index locality); `company_id uuid NOT NULL DEFAULT
   (current_setting('app.company_id', true))::uuid` (auto-stamped on write from the session GUC — a
   Postgres feature, validated by RLS `WITH CHECK`, so ported INSERTs need no company_id column);
   `uuid` FKs; `timestamptz` times; `numeric` money/budget; `jsonb` blobs (`env`, `config`, payloads);
   enums `text` + `CHECK`. Uniqueness: keep id-anchored natural keys as-is (global once uuid); prepend
   `company_id` to non-id-anchored partial-uniques (`task(company_id, origin_kind, origin_fingerprint)
   WHERE …`). Single-column uuid PK/FK throughout — no composite PK.
3. **Scoping audit (the risk).** Reads are RLS-filtered; writes are auto-stamped + `WITH CHECK`-validated.
   The audit verifies every engine table has the policy and **no** SQL path bypasses it (no stray
   superuser/`SET ROLE`, no cross-company join that widens scope). Per-table "no-leak" test (§6).
   lattice's recall path is highest-risk — a mis-scoped similarity search would surface another
   company's episodes.
4. **RLS + role + grants** — per table: `ENABLE` + `FORCE ROW LEVEL SECURITY`, one policy with **both**
   `USING (company_id = (current_setting('app.company_id', true))::uuid)` **and** the matching
   `WITH CHECK` (reads *and* writes scoped). Unset GUC → NULL → zero rows (fail closed; `NULL::uuid` is
   NULL, never an invalid-uuid error). Explicit `GRANT SELECT, INSERT, UPDATE, DELETE … TO podium_app`
   per table; engines run under `podium_app` (NOSUPERUSER NOBYPASSRLS) so FORCE RLS is never bypassed.
   *Perf:* on hot tables wrap as `= (SELECT (current_setting('app.company_id', true))::uuid)` so it's an
   InitPlan (once), not per-row.
5. **Provisioning does no per-company DDL.** Engine tables are created **once** by a migration at deploy.
   Per-company provisioning is only a saga (keyed on `companies.state`, per M4 §3): flip `provisioning →
   idle` and seed default rows, scoped by `company_id`; idempotent; retried on failure; a
   half-provisioned company is never `idle`. No schema is created when a company is born.

## 4. Cross-process story (why this is the M4 unblock)

SQLite gives one writer per file — api and conductor in separate processes can't share it. Postgres +
`company_id` + RLS gives both processes concurrent, isolated access to the same company's ledger over
the shared cluster. M4's control-plane reads (roster, proposals, org report) run in the api process;
writes that must couple to a live beat still route through the conductor. Same CQRS/saga model as M3 —
M5 just makes the ledger a first-class shared store.

## 5. Escape hatch (keep the seam, drop the milestone)

If a customer contract ever demands physical/schema isolation, or a company needs DDL divergence: run
*that* company in its own schema via the driver's `search_path` hook, as a per-company exception. It's a
config flag on `companies`, not a milestone. Everyone else stays shared-schema. This is why the
`LedgerConnection` seam is worth keeping even though schema-per-company is no longer a phase.

## 6. TDD slices

Each slice is RED→GREEN on real Postgres (ephemeral PG18 harness, `--encoding=UTF8`), gated by
`ruff format` + `ruff check` + `mypy --strict` + `pytest`.

1. **podium ids → uuid (prerequisite).** Migrate 0001–0007 tables (workspaces/companies/users/runs/
   events/commands/api_keys) from `sa.String()` ids to `uuid` (`uuidv7()` default), FKs to `uuid`; RLS
   policies cast the GUC (`… = (current_setting('app.workspace_id', true))::uuid`); `tenant_session`
   passes a `uuid`. Test: full M0–M3 suite green on uuid ids; a bad-uuid GUC → 0 rows (fail closed).
2. **Postgres-native port.** chorus's ledger contract suite passes on Postgres — uuid PKs, timestamptz,
   jsonb, `ON CONFLICT` — one company, RLS off, green. (Proves the dialect/runner port in isolation.)
3. **uuid + company scoping.** `uuidv7()` PKs; two companies each insert a task with
   `origin_fingerprint='default'` → **both persist** (company-scoped partial-unique); a *within*-company
   duplicate is rejected; a `timestamptz` and a `jsonb` blob round-trip.
4. **Ledger session primitive** — `ledger_session(cid)` = `SET LOCAL app.company_id`; auto-stamp DEFAULT
   fills `company_id` on INSERT. Test: mirrors `test_tenant_session` for the company axis; an INSERT with
   no company_id lands in the session's company; `WITH CHECK` rejects a wrong explicit company_id.
5. **No-leak suite (the important one)** — per engine table (chorus ledger **+ lattice episodes +
   skills**): company A writes, company B's session reads → 0 rows; A's session reads → A's rows. FORCE
   RLS + non-superuser proven (a superuser-bypass negative test, mirroring `test_rls`). Explicit
   lattice case: a recall/similarity query under B never returns A's episodes.
6. **Concurrent access** — api-role session and conductor-role session, same company, interleaved
   read/write in separate connections → consistent, isolated. (The M4 unblock, tested.)
7. **Company ownership** — user U1 creates company C; U1 reads C (ok), workspace-peer U2 reads C → 404
   (not owner), workspace-admin reads C → ok. Runs under C are creatable by U1, 403/404 for U2. RED
   first: today's workspace-only authz lets U2 see C.
8. **Provisioning saga** — `state: provisioning → idle` seeds a company's default rows idempotently (no
   DDL — tables already exist); retried on failure; a half-provisioned company is never `idle`.
9. **End-to-end** — a Postgres-backed company runs a real directive (ledger writes + a memory recall + a
   skill read, all company-scoped) green. Flip `ledger_backend` default to postgres only after this.

## 7. Build order (confirmed)

**M5 before the full M4 control plane** — M4 needs concurrent api+conductor ledger access, which only the
Postgres ledger provides. M5 slices 1–5 are the hard part; land them behind the flag, validate one
company, then M4 builds on a ledger it can actually share.

## 8. What I'd revisit as it grows

- **`company_id`-leading indexes are baseline, not optional.** RLS injects `company_id = …` into every
  query, so every query-supporting index must lead with `company_id` or the planner can't combine it
  with the policy predicate. That leadership is a given; what to *measure before adding* is which
  further columns follow it (equality-then-range, per the composite-index rule).
- **Citus / distribution** — shared-schema + `company_id` is already the distribution key shape. Only
  reach for Citus when a single Postgres node is provably the ceiling; the schema doesn't change.
- **Physical isolation demand** — the §5 escape hatch handles the first such customer without a
  re-architecture. If *many* customers demand it, reconsider database-per-tenant for that tier only.

## 9. Postgres-patterns conformance

| Pattern | Status |
|---|---|
| FORCE RLS + non-superuser NOBYPASSRLS role | ✅ reuses proven `podium_app` (0002) |
| Txn-local GUC via `SET LOCAL` → pooler(PgBouncer transaction-mode)-safe | ✅ same as `tenant_session` |
| Fail-closed on missing GUC (`(current_setting(…, true))::uuid` → NULL → 0 rows) | ✅ |
| RLS policy has **both** `USING` and `WITH CHECK` | ✅ |
| Per-table `GRANT … TO podium_app` | ✅ |
| **`uuid` PKs (`uuidv7()`)** — global + time-sortable, replacing chorus's random 48-bit `TEXT` hex | ✅ this rev |
| **`uuid` everywhere incl. podium's own ids** (0001–0007 migrated) | ✅ prerequisite slice §6.1 |
| **`timestamptz`** for all time cols incl. engine tables (chorus `TEXT` ISO ported) | ✅ this rev |
| **`numeric`** money/budget, **`jsonb`** blobs, `text`+`CHECK` enums | ✅ this rev |
| Company-scoped unique constraints (non-id-anchored keys prefixed with `company_id`) | ✅ this rev |
| Postgres-native migration runner (SQLite `sqlite3`/`PRAGMA`/`BEGIN IMMEDIATE` runner retired) | ✅ this rev |
| SQLite ledger backend | ⛔ **retired** — its intersection with Postgres is what forced the compromises |
| `company_id`-leading indexes cover the RLS predicate | ✅ baseline |
| Provisioning runs no per-company DDL (shared schema) | ✅ |
| InitPlan-wrapped predicate on hot tables | ✅ specified for hot tables |
