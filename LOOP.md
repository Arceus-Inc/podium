# LOOP.md — the Arceus overnight loop

How this repo family (podium · chorus · dream · lattice · horizon) is operated with
loop-engineering patterns while the operator sleeps. Reference for what "good" looks like:
paperclip — a company that gets substantial work done across employees with nobody pushing it.

## The active loop

**Company free-runner (L2 — assisted, self-paced).**
One iteration = one full cycle:

1. **constraints** — re-read `loop-constraints.md`; binding.
2. **budget** — check `loop-budget.md` + spend on the live company; over budget → observe-only.
3. **triage** — observe the LIVE company (runs, tasks, routine firings, evaluator notes in
   `docs/evals/`, beat traces) plus repo gates; produce prioritized findings in `STATE.md`.
4. **minimal-fix** — pick the top finding; fix at the root, TDD, smallest diff, in whichever
   Arceus repo owns the cause. Prompts/skills/routines count as code.
5. **verify** — repo gates (ruff, mypy, pytest) + relaunch the stack + observe the live effect.
6. **log** — append the iteration to `loop-run-log.md`; update `STATE.md`; commit checkpoints.
7. Re-arm the next iteration (ScheduleWakeup) and let the live company run between cycles.

## Cadence

Self-paced: fire the next triage when the live company has produced new evidence
(a routine fired, a delegation reached a terminal state) or ~20-30 min, whichever first.

## Handoff (wake the human)

Org-shape decisions, anything crossing a human gate (plan approval is the loop's ONLY allowed
approval action, and only for plans the loop itself provoked on its own playground company),
pushes to remotes, and anything in the constraints denylist.
