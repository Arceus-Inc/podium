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
