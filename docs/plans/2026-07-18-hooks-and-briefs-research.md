# Research: hook surfaces + lean briefs (free-run loop, iteration 6)

*Agent-produced report, 2026-07-18. The build blueprint for H1 (hooks) and P1 (lean briefs).
Sources inline. Key triangulation: Claude Code shows gate mechanics, paperclip shows the
org-level event vocabulary + bounded-recovery doctrine, Polsia shows scheduling + scoped tools
+ metered budgets. "The brief carries character; the hooks carry law."*

## A. The synthesized hook-event table for an org of agents

Design rules: (1) deterministic code decides WHEN to wake; the model decides WHAT once woken;
(2) every event carries an idempotency fingerprint + bounded retry; (3) recovery ladder =
retry-once → blocked+recovery-action → human; (4) state-asserting wakes throttle, event-shaped
wakes always deliver; (5) cheap-model lane for bookkeeping only.

| # | Event | Deterministic vs beat | Arceus status |
|---|---|---|---|
| 1 | message-received / comment | beat; code dedupes, structured mention ≠ plain text | ✅ wakes + inbox-in-brief; hooks v1 turns INSTRUCTION → task (7c7c2e7) |
| 2 | task-assigned | beat, gated by pre-dispatch validation (secrets/workspace refuse-to-fail) | ✅ wake; ❌ pre-dispatch validation |
| 3 | blockers-resolved | deterministic (last blocker only) → beat | ✅ dependencies + unblock |
| 4 | children-completed | deterministic → beat; parentId=structure, blockers=dependency | ✅ CHILDREN_DONE |
| 5 | child-rejected / review-verdict | beat; exactly-once per (issue, revision) fingerprint | ✅ reject routes to manager; corrective machinery |
| 6 | monitor-due (one-shot, re-arm consciously) | scheduler → beat | ✅ monitors table |
| 7 | beat-failed / run-lost | deterministic retry-once → blocked+recovery, NEVER silent reassign; cheap lane for status-only | ✅ ladder; ❌ cheap-model lane |
| 8 | budget-warn / ceiling | warn→beat may descope; ceiling→CODE auto-pauses + notifies board | ✅ policy+incident; ❌ auto-pause wiring check |
| 9 | routine-fired | deterministic firing → beat; beat must END with a durable next path | ✅ cron; ❌ "durable next path" stop-gate |
| 10 | human-response | deterministic pingback → beat; typed interactions only | ✅ approvals; ❌ typed ask_user/suggest_tasks interactions |
| 11 | stalled-work-detected | deterministic fingerprint dedup → scoped verifier beat (authority server-enforced) | ✅ watchdog+recovery; partial |
| 12 | org-changed (hire/pause/terminate) | deterministic bookkeeping; onboarding beat | ❌ onboarding beat; ✅ terminate sweep |

Payload cross-cuts: company_id, event_id, fingerprint, wake_reason, caused_by, billing_code,
model_lane.

Claude Code hook mechanics worth copying into dream/chorus: every gate is pre (deny/rewrite)
or post (annotate/block-next); hooks return verdicts or injected context, never free control;
prompt/agent handler types make the REACTION model-driven while the TRIGGER stays deterministic.
Paperclip liveness doctrine: every non-terminal issue holds exactly one of {active run, queued
wake, typed interaction, monitor, human owner, healthy blocker chain, recovery action}.

## B. Lean briefs — placement rules + anatomy

- Brief (target <~600 tokens): identity/mission, reporting lines, autonomy stance (one
  Codex-style persistence clause), communication contract, 3-5 ranked judgment priorities,
  ending discipline (norm here, ENFORCED by a stop-gate), pointer to procedures. Nothing else.
- Tool descriptions: everything about one action ("when calling X…" belongs ON X).
- Hooks/gates: anything that must hold 100% — prohibitions decay in long contexts
  (arXiv 2604.20911: omission constraints decay, commission persist); "ALWAYS/NEVER" in prose
  = a gate wearing a costume; patch the harness, don't append a rule.
- Event payload: situational facts injected per-wake (paperclip fat-payload), never baked in.
- Evidence: reasoning degrades past ~3k instruction tokens; mid-prompt retrieval ~55%;
  250-375-token instruction blocks beat 1500+ on tool selection; ~150-200 instruction budget.

Key sources: code.claude.com/docs/en/hooks · anthropic.com/engineering/effective-context-
engineering-for-ai-agents · anthropic.com/engineering/writing-tools-for-agents · OpenAI
gpt-5/codex prompting guides · polsia.com + founder write-ups · paperclip doc/SPEC.md +
server/src/services/heartbeat.ts.

## The build queue this creates (added to the loop checklist)

- H2: pre-dispatch validation (row 2) — refuse to dispatch a run guaranteed to fail.
- H3: routine stop-gate (row 9) — a routine beat must end with a durable next path.
- H4: budget-ceiling auto-pause wiring check (row 8) live.
- H5: onboarding beat on hire (row 12).
- H6: typed human interactions (row 10) — request_confirmation / ask_user / suggest_tasks.
- P1: rewrite DELEGATION_BRIEF + backend_engineer brief per anatomy (<600 tokens), move
  prohibitions into gates; measure live on videocursor.
