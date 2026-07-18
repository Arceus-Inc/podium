# Loop Constraints — Arceus overnight loop

> Read at the start of EVERY iteration. Binding.

## Secrets & credentials
- Azure creds come from `/Users/divyansh/chorus/.env` only; never echo, log, or commit them.
- Playground API tokens are local-dev-only; never put one in a commit or a doc.
- Never edit `.env`, `.env.*`, or anything under a `secrets/`-like path.

## Git
- Never push to any remote — pushes are the human's call.
- Never `git add -A` in chorus; add named files only. Never commit the operator's untracked
  chorus artifacts (standup-app/, reports/, docs/research, .omx/, CLAUDE.md,
  examples/*probe*.py, pm_web_e2e.py, flow-report.html).
- Commit checkpoints per fix on the current working branches (podium feat/org-mode,
  chorus feat/trace-spine). No attribution trailers.
- Never merge to main.

## Code
- TDD: no production change without a failing test first (prompt/text-only changes are exempt
  but must be verified live).
- Fix roots, not symptoms; smallest coherent diff; no drive-by refactors.
- No hardcoding company/employee names in engine or product code — triggers must be structural
  (a tool held, a status, a mode), never a name.
- Gates before every commit: ruff + mypy + the affected test suites. Never weaken a test to
  pass it; updating a pinned expectation for an intended behavior change is fine and must say so.
- Max 3 fix attempts per finding; then log it as blocked in STATE.md and move on.

## Live company
- The loop may mint playground companies, submit runs, approve/reject plans on companies IT
  created this session, fire/pause routines, comment, and cancel ITS OWN stale runs.
- Never terminate employees; never delete data.
- If total live spend across loop-created companies exceeds $25, stop launching new runs and
  switch to observe-only (fixes + tests may continue).

## Communication
- Every iteration appends to `loop-run-log.md` (what changed, why, what ran, what it cost).
- The final morning summary must be reconstructible from the run log alone.
