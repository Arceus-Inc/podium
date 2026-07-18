# Loop Run Log — Arceus free-runner

One entry per iteration. Newest last. Format: what was observed → what changed (repo,
commits) → what ran → outcome → spend.

---

## Iteration 0 — 2026-07-18 ~14:30 (scaffold)

- Installed loop-engineering skills (loop-triage/verifier/constraints/budget/minimal-fix)
  into ~/.claude/skills; scaffolded LOOP.md, STATE.md, loop-constraints.md, this log.
- Carried in from the free-run session (pre-loop, same day): checklist
  docs/plans/2026-07-18-company-freerun-checklist.md; fixes F1-F6 landed
  (chorus aacf012, f942cd0; podium 5c1a1e4 + OM-1..5 series); fresh linkport company
  formed, approved, routines auto-provisioned, delegation kicked under the root goal.
- Live at scaffold time: lp-fe-1 + lp-be-1 building in parallel; lp-pm-1 parked for
  integrate; casey review blocked (investigate in iteration 1).

## Iteration 1 — 2026-07-18 ~15:05

- **Observed (live)**: executive review PASSED 1.0 citing company_state.json (checklist #5
  proven) — then parked BLOCKED behind a board acceptance gate: `ceo_dod` classified the
  routine's own guard sentence ("do not hire, delegate, or spend") as a COMMIT. Formation task
  kept re-beating after its plan was APPLIED (burning ~30¢/attempt on ledger-hygiene nitpicks).
  Delegation: fe module done; be module rejected → lead dispatched a corrective (organic
  coherence loop); root parked for integrate.
- **Changed (chorus 9af7228)**: classify_action strips negated clauses before commit-cue
  matching (+ routine wording avoids noun-'spend'); workforce_plan.proposed_in_task_id
  (delta 0004, propose tool stamps its beat task) and approve completes the origin task.
- **Ran**: chorus governance/tools/ledger + employee suites green; ruff+mypy clean.
- **Live action**: one-time backfill on the loop's playground — approved the stuck acceptance,
  completed the decided formation + passed review tasks. Left the in-flight delegation
  undisturbed (server restart deferred until it reaches terminal).
- Spend so far (fresh company): ~$1.21.

## Iteration 2 — 2026-07-18 ~15:40

- **Observed (live, both companies)**: backend engineer report-only routine beats (SLO watch,
  dependency scan) failed 0.0 under the strict-TDD reviewed build — "repo file access and even
  read-only git/status were denied by a strict TDD gate". videocursor: 5-IC fan-out across
  every profession; marketer + frontend DONE first try; be/analyst/designer rejected (lead's
  paraphrased intents drifted from module framing; analyst missed its findings.md artifact).
  linkport: lead dispatched correctives organically; links.js corrective converging on
  API-mismatch feedback.
- **Changed (chorus dc99521)**: backend_engineer_dod honors the routines' own contract phrase
  ("Report only" / "Report and propose only") → judged-report agent_review that PASSES honest
  "nothing to scan yet" findings; build intents keep the reviewed build.
- **Ran**: 455 employee tests green; ruff+mypy clean. Stack restarted (fix live; recovery
  resumed the one in-flight corrective).
- **Queued**: child-intent/DoD alignment (lead paraphrase drift) if correctives don't converge.

## Iteration 3 — 2026-07-18 ~15:20

- **Directive**: kill linkport, focus videocursor. Linkport quiesced (run canceled, 6 open
  tasks cancelled, 4 wakes dropped, routines paused). Its legacy: fe modules shipped done ×2,
  links.js converged to "API mismatch" quality feedback — the coherence loop worked but the
  module never cleared review before the kill.
- **Observed (videocursor)**: SELF-HEALING — lead re-woke and started its corrective beat
  unaided. Comment channel used organically: 3 ICs escalated blockers to the lead; the lead
  replied with direction (OM-3 in the wild). Marketer's web_search failed 6/6.
- **Root cause found**: chorus/.env has `EMAIL_FROM=Name <mail@x>` — `source` sees a redirect,
  errors, silently abandons the rest of the file → TAVILY_API_KEY never exported. The boot
  script now parses KEY=VALUE lines verbatim (podium 00531ad); all 4 key groups verified set.
- **Ran**: stack restarted with full env after the lead's beat finished; recovery resumes.

## Iteration 4 — 2026-07-18 ~15:50 (the big one)

- **Observed (videocursor)**: podium run "succeeded" FALSELY. Chain: real root exhausted its
  integrate cap → engine correctly opened a `stranded` recovery — but the restart's reclaim
  re-ran execute() and SUBMITTED A DUPLICATE delegation root; the duplicate's kickoff beat
  "passed" its in-beat evaluation (praising the fe module in the shared worktree) without ever
  decomposing, and completed through the delivery path: zero children, contract never verified.
- **Changed**:
  - podium 068673d — RunRef carries engine_task_id; a reclaimed run RESUMES the watch on its
    recorded root (re-attaching the mirror), never re-submits.
  - chorus 0d4e5e2 — a delegation root whose beat passes with zero children parks BLOCKED and
    re-wakes the lead to decompose; it can never land done through the delivery path.
- **Ran**: podium full suite green (24); heartbeat suite green except the 2 known pre-existing
  failures (confirmed on clean tree); ruff+mypy clean both repos. Stack restarted on the fixes.
- **Live action**: retired the exhausted round (false-done duplicate + stranded root + rejected
  children cancelled, recovery resolved); kicked delegation v2 (run 019f74a6-83a6…) with
  verbatim per-IC module briefs, depends_on ordering (marketer after analyst), findings.md
  named for the analyst, and explicit corrective-dispatch instruction. Web tools now live.

## Iteration 5 — 2026-07-18 ~16:15

- **Observed (v2 round)**: verbatim child briefs + depends_on WORKED — analyst done first try
  (web tools live, findings.md named), marketer done grounded in the analyst's research,
  executive review done with NO board gate (it.1 fix verified in production). The one
  persistent flaw: the be build child rejected again with ZERO artifacts — its traces show
  every orientation call (ls/git status/read) refused pre-RED, and the generator never even
  spawned test_author before giving up.
- **Changed (chorus d2715be)**: the TDD gate now admits calls the tool itself classifies
  read-only (dream's vetted bash/git allowlists) before RED — inspection is orientation, not
  production. Mutations stay locked until RED.
- **Ran**: gate + harness suites green; ruff+mypy clean. Restart armed for the next
  between-beats window so the lead's be corrective runs on the fixed gate.

## Iteration 6 — 2026-07-18 ~17:15 (user checklist absorbed)

- **User checklist (verbatim intent)**: 1) hooks in dream+org (message as a hook, delegatory
  reactions, explore surface) 2) lean principled briefs (Codex-style; de-hardcode; research)
  3) smoother delegation 4) routines that truly drive autonomy 5) study paperclip/Polsia/other
  autonomous OS. Method: deep research → TDD build → e2e on videocursor → mind-map gap vs a
  real human company → repeat indefinitely.
- **Discovery**: dream ALREADY ships a full beat-level hook system (spec 13: session/prompt/
  pre+post-tool/compact/subagent/stop events, observer-only executor, plugin loading) — dormant
  in our product. The missing layer was ORG-level hooks.
- **Built (chorus 7c7c2e7)**: `chorus.hooks` — pulse-phase deterministic reactions (idempotent,
  crash-isolated, no model calls): pulse = recover → cron → ORG HOOKS → monitors → dispatch.
  First built-in: delegatory message hook — INSTRUCTION message → real todo task for the
  recipient (fingerprint message:<id>, thread goal inherited), inbox nudge consumed. 4 tests.
- **In flight**: background research agent on (a) hook-surface table for agent orgs (claude
  code hooks, paperclip triggers, Polsia/autonomous-OS scan) (b) lean-brief best practice.
  Videocursor v2: 4/5 modules done (analyst/marketer/designer/fe), lead integrating, be
  corrective pending on the fixed TDD gate; 2 gate-free executive reviews.
- **Next iterations**: consume research → full hook-event table (child-rejected → corrective
  nudge, budget-warn, task-assigned ack) → P1 lean brief rewrite (start: delegation brief +
  backend engineer) → D (delegation smoothing: depends_on default guidance in decompose refusals)
  → R (routine quality pass) → mind-map doc + e2e round 3.

## Iteration 7 — 2026-07-18 ~17:20 (P1 landed)

- **P1 (worktree agent, merged chorus bc209ff)**: backend brief 2764→~871 tokens, frontend
  2460→~799. Enforced prohibitions deleted (TDD gate + evidence ratchet + validators hold
  them); craft procedure moved to skills (§6 structuring-any-service; vite/playwright recipes
  already in skills — duplication deleted, pointers kept); tool call-procedure verified to
  live on the tools. New budget+anatomy tests pin the lean form (≤900 tokens, subagents named,
  manager escalation, deliverable class). 459 employee tests green; ruff/mypy clean.
- Restart armed for next quiet window → videocursor's be corrective becomes the live P1+it.5
  proof (lean brief + read-only-aware gate together).
- Next: H2 pre-dispatch validation, H3 routine durable-next-path gate, H4 budget auto-pause
  check, H5 onboarding beat, H6 typed interactions; then designer/marketer/pm brief pass;
  mind-map doc; e2e round 3.
