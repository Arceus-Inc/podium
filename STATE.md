# Loop State — Arceus free-runner

Last run: iteration 6 — 2026-07-18 ~17:15

## Live context

- Stack: scripts/cockpit-dev.sh on :8901, PG :55444. Branches: podium feat/org-mode,
  chorus feat/trace-spine.
- Free-run company: ws 019f745e-1c2b-746f-bd9c-dc02a299ad8c ·
  co 019f745e-1c2b-7fa7-b7f5-76cbe2927dcd ("linkport", fresh, on fixed code).
- Root goal seeded from founder objective ✅. Plan approved; routines auto-provisioned ✅.
- In flight: delegation run 019f7461-9703… (lead lp-pm-1; children lp-fe-1 + lp-be-1 were
  in_progress in parallel; lead parked). casey: executive review fired (blocked ⇠ investigate),
  formation task cycling in_progress/blocked.
- Checklist: docs/plans/2026-07-18-company-freerun-checklist.md (#4-#9 fixed this session).

## High priority (act) — the user's five-item checklist drives everything now

1. HOOKS: org layer v1 shipped (chorus.hooks). Research LANDED →
   docs/plans/2026-07-18-hooks-and-briefs-research.md — READ IT FIRST each iteration.
   Build queue: H2 pre-dispatch validation, H3 routine durable-next-path stop-gate,
   H4 budget-ceiling auto-pause check, H5 onboarding beat, H6 typed human interactions.
2. LEAN BRIEFS (P1): anatomy + placement rules in the research doc (<600 tokens/brief;
   prohibitions→gates because omission constraints decay). Start: DELEGATION_BRIEF +
   backend_engineer brief; verify live on videocursor.
3. SMOOTHER DELEGATION: v2 proved verbatim-module briefs + depends_on work — encode those as
   engine affordances (decompose refusal teaching, default child-brief scaffold), not prompt
   ritual.
4. ROUTINES: verify pm weekly (record_decision) + slo/dependency scans PASS under the new
   report DoD on the next firings.
5. RESEARCH/MIND-MAP: paperclip re-read + Polsia + autonomous-OS scan (agent running);
   write docs/plans/mind-map comparing a real human company's loop to ours; add checklist
   items from gaps. Videocursor v2: be corrective is the last module; run e2e round 3 after
   the brief/hook changes land.

## Watch list

- PM weekly-planning under the new intent (record_decision) — next firing.
- Lattice consolidation once ≥5 beats/cluster accumulate.
- Rate limiter vs cockpit polling (dev nuisance; 429s in views).

## Recent noise (ignored)

- Pyright cross-repo import diagnostics in the editor — environment mismatch, not real.

## Design decision (operator dialogue, it.9)

- Evidence protocol RATIONALE: self-graded agents false-done (live receipts: vacuous root
  pass, links.js export mismatch). Keep forever: kernel-run objective test floor + cheap
  independent review. Make the RED ratchet a TRUST DIAL, not a default: strict for new/
  recently-failing employees, floor+review once lattice shows a verified track record,
  rejection knocks trust back. Plug point = the EvidenceProtocol object (agent in flight).
  QUEUED: trust-tiered evidence after the protocol refactor merges.

## OVERRIDING design decision (operator, it.10) — supersedes the it.9 trust-dial note

- NO system verifier, NO kernel-based verification. The employee's own in-beat evaluation IS
  the verdict. Surgery in flight (worktree agent): reviewed_build DoDs → self-judged
  agent_review (rubrics keep substance, drop evidence-file demands); TDD gate unwired;
  SYSTEM_VERIFIER beat removed from leaf + delegation-parent close. Deterministic integrate
  floor + descendants checks kept for now (flagged as open question).
- The evidence-protocol refactor (stopped agent) is MOOT — discard its worktree, do not merge.
