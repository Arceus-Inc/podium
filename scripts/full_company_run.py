"""One end-to-end company run from a SINGLE line of input — the one-mind, one-ledger showcase.

Input (the ONLY thing a human provides):

    "create a markdown editor for calm users"

From that one line the company does EVERYTHING itself:

  1. DECISION + PLAN — the CEO (the one mind) runs a formation beat: it reasons a workforce AND
     authors a roadmap (a proposed decision whose goals each carry a measurable metric + target),
     held deterministically in horizon (the one ledger). No hardcoded roadmap.
  2. HIRING — the CEO proposes a workforce plan; the operator approves it (the governed door), so
     real people with real names are hired and pod leads get management grants.
  3. WORK — the operator reads the CEO's *own* authored goals out of horizon and issues a delegation
     run per goal under the capable pod lead. Each lead forms a mission team; the ICs build, test,
     and produce artifacts.
  4. MANAGEMENT — leads decompose work onto their reports, file staffing requests when short-handed
     (the operator grows the org on that pull), and gate each deliverable on its Definition of Done.
  5. REPORT — when the CEO's roadmap has run to terminal (or a deadline hits), we render one rich,
     self-contained HTML report of the whole thing (org, goal tree, task tree, beats, decisions,
     approvals, artifacts, DoDs, spend + the files the company actually produced).

This REUSES the proven autonomous executor (``company_operator.Operator`` — approvals, capability-
routed delegation, pull-based growth, metrics) and only swaps ONE thing: the source of the roadmap is
the CEO's authored goals, not a hardcoded list. That is exactly the "operator consumes CEO-authored
roadmaps" step of the refactor.

Run against a live podium (embedded conductor, MAX_TICKS=0) — same env as e2e_one_mind.py.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path

# --- the single line of input -------------------------------------------------------------------
MISSION = "create a markdown editor for calm users"

# --- tune the executor for a company that ships its GOALS IN PARALLEL. The CEO reasons ~3 goals from
#     the mission, so the org must be able to form ~3 cross-functional pods (a lead + a discipline mix
#     of ICs) that each own a goal at once. Head targets are sized for that: the delegation daemon fans
#     one goal per pod and growth pulls in a new pod for any held goal, up to the cap. -----------------
os.environ.setdefault("OPERATOR_TARGET_HEADCOUNT", "12")
os.environ.setdefault("OPERATOR_MIN_VIABLE_HEADCOUNT", "7")
os.environ.setdefault("OPERATOR_MAX_HEADCOUNT", "15")
os.environ.setdefault("OPERATOR_MAX_ACTIVE_GOALS", "3")

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT / "scripts"))

import company_operator as co_mod  # noqa: E402
from company_operator import Operator  # noqa: E402

# Redirect the operator's bookkeeping to a FRESH, run-scoped directory so this is a clean run (the
# stock operator reuses a prior company from context.json — we always want a brand-new company here).
RUN_DIR = _ROOT / ".monitor-runs" / "full-run"
co_mod.OUT = RUN_DIR
co_mod.CTX_FILE = RUN_DIR / "context.json"
co_mod.DB_FILE = RUN_DIR / "operator.db"
co_mod.STATUS_FILE = RUN_DIR / "STATUS.md"
co_mod.MISSION = MISSION

WORKDIR = Path(
    os.environ.get("PODIUM_WORKDIR", str(_ROOT / ".podium" / "workdir"))
)
REPORT_DIR = _ROOT / "reports" / "calm-markdown-run"
DEADLINE_S = int(os.environ.get("FULL_RUN_DEADLINE_S", str(90 * 60)))  # 90 min safety cap
MAX_DELEGATED_GOALS = int(os.environ.get("FULL_RUN_MAX_GOALS", "5"))


def _now_hhmmss() -> str:
    return time.strftime("%H:%M:%S")


class FullRun(Operator):
    """A bounded, CEO-roadmap-sourced run: formation -> hire -> execute the CEO's goals -> report."""

    def __init__(self) -> None:
        super().__init__()
        self._seeded = False  # have we read the CEO's roadmap into the goal pipeline yet?

    # Always start a brand-new company (never reuse a prior one) so the report is a clean, single run.
    async def ensure_company(self) -> None:  # type: ignore[override]
        assert self.http is not None
        r = await self.http.post("/v1/dev/bootstrap", json={"name": "calm-md"})
        r.raise_for_status()
        d = r.json()
        self.ws, self.co, self.token = d["workspace_id"], d["company_id"], d["token"]
        co_mod.CTX_FILE.write_text(json.dumps({"ws": self.ws, "co": self.co, "token": self.token}))
        self.log(f"bootstrapped company {self.co}")

    # ---- the ONE swap: source the roadmap from the CEO, not a hardcoded list --------------------
    def _read_ceo_roadmap(self) -> list[dict]:
        """Read the CEO's authored, decision-linked goals out of horizon's ledger (strategy.json).

        Each entry is a goal the CEO reasoned from the single mission line, with a measurable metric
        + target and its rationale — everything a pod lead needs as a Definition of Done.
        """
        strat = WORKDIR / self.co / "strategy.json"
        if not strat.exists():
            return []
        try:
            records = json.loads(strat.read_text(encoding="utf-8"))
        except Exception:
            return []
        out: list[dict] = []
        for rec in records.values():
            decision_id = rec.get("decision_id") or ""
            if not decision_id or not rec.get("title"):
                continue
            # Only the CEO's ROADMAP goals (roadmap_propose mints ``dec_<hex>`` decision ids). Skip
            # podium's F2 root-objective seed (``dec-<uuid>``): it mirrors the whole raw mission as one
            # coarse goal to activate horizon's outcome listener — it is NOT the CEO's decomposition.
            if not decision_id.startswith("dec_"):
                continue
            out.append(
                {
                    "title": rec["title"],
                    "metric": rec.get("metric") or "",
                    "target": rec.get("target") or "",
                    "rationale": (rec.get("evidence") or [""])[0] if rec.get("evidence") else "",
                    "score": rec.get("score"),
                }
            )
        # Highest-priority (score) first, so the most important outcomes get delegated first.
        out.sort(key=lambda g: (g.get("score") or 0.0), reverse=True)
        return out

    def _brief_from_ceo_goal(self, g: dict) -> str:
        """Assemble the delegation directive from the CEO's authored goal fields ONLY.

        No prompt prose is added here — HOW to build/test is the delivering employee's own brief
        (it lives with the employee, not in this script). This carries only the CEO-authored WHAT +
        the measurable Definition of Done (metric + target) + the CEO's rationale.
        """
        parts = [g["title"].strip().rstrip(".") + "."]
        if g.get("rationale"):
            parts.append(g["rationale"])
        if g.get("metric"):
            parts.append(f"Definition of Done (metric): {g['metric']}.")
        if g.get("target"):
            parts.append(f"Target: {g['target']}.")
        return " ".join(parts)

    async def _seed_from_ceo_when_ready(self) -> None:
        """Poll horizon until the CEO has authored its roadmap, then queue those goals ONCE.

        This is the ``goal_daemon`` replacement: instead of cycling a hardcoded ROADMAP forever, we
        take the CEO's own decision as the company's roadmap and stop (bounded run).
        """
        deadline = time.time() + 20 * 60
        while time.time() < deadline and not self._seeded:
            await asyncio.sleep(8)
            roadmap = self._read_ceo_roadmap()
            if not roadmap:
                continue
            self.log(f"CEO authored a roadmap of {len(roadmap)} goal(s) — consuming it as the company's plan")
            for g in roadmap[:MAX_DELEGATED_GOALS]:
                # Create the executable (team-level) goal the delegation run will hang its task tree
                # under; its title + brief are the CEO's, so WHAT gets built is CEO-decided.
                gid = await self.create_goal(g["title"])
                if not gid:
                    continue
                self.goal_runs[gid] = {
                    "title": g["title"],
                    "brief": self._brief_from_ceo_goal(g),
                    "run_id": None,
                    "lead": None,
                    "status": "pending",
                }
                if self.db is not None:
                    self.db.execute(
                        "INSERT OR REPLACE INTO goals VALUES(?,?,?,?,?,?)",
                        (gid, g["title"], "ceo-roadmap", "pending", co_mod.now(), None),
                    )
                    self.db.commit()
                self.log(f"queued CEO goal '{g['title'][:48]}' ({gid[:8]})")
            self._seeded = True

    def _all_goals_terminal(self) -> bool:
        terminal = ("succeeded", "failed", "canceled", "timed_out", "done", "stranded")
        return bool(self.goal_runs) and all(
            i["status"] in terminal for i in self.goal_runs.values()
        )

    async def _completion_watcher(self) -> None:
        """Signal done once the CEO's roadmap has run to terminal (or the safety deadline hits)."""
        while True:
            await asyncio.sleep(15)
            if self._seeded and self._all_goals_terminal():
                self.log("all CEO-authored goals reached a terminal state — run complete")
                self._done = True
                return

    async def run(self) -> None:  # type: ignore[override]
        import httpx

        self._done = False
        self.setup_db()
        self.http = httpx.AsyncClient(base_url=co_mod.BASE, timeout=30.0)
        self.pg = await co_mod.asyncpg.create_pool(co_mod.LEDGER_DSN, min_size=1, max_size=4)
        await self.ensure_company()
        self.log(f"MISSION (the only input): {MISSION!r}")

        # 1) DECISION + PLAN: one formation beat — the CEO authors workforce AND roadmap.
        await self.submit_run("formation", MISSION)

        # The proven daemons do the rest; we swap goal_daemon for the CEO-roadmap seeder and add a
        # completion watcher so the run is bounded and then reports.
        daemons = [
            asyncio.create_task(self.approvals_daemon()),      # HIRING: approve the CEO's plan
            asyncio.create_task(self.growth_daemon()),         # MANAGEMENT: grow on lead pull
            asyncio.create_task(self.delegation_daemon()),     # WORK: run each goal under a lead
            asyncio.create_task(self.metrics_daemon()),        # observability + STATUS.md
            asyncio.create_task(self._seed_from_ceo_when_ready()),
            asyncio.create_task(self._completion_watcher()),
        ]
        self.log("running — daemons: approvals, growth, delegation, metrics, ceo-seed, completion")

        deadline = time.time() + DEADLINE_S
        try:
            while not self._done and time.time() < deadline:
                await asyncio.sleep(10)
            if not self._done:
                self.log(f"deadline reached ({DEADLINE_S}s) — reporting on progress so far", "warn")
        finally:
            for t in daemons:
                t.cancel()
            await asyncio.gather(*daemons, return_exceptions=True)

        await self._final_summary()
        self._build_report()

    async def _final_summary(self) -> None:
        try:
            org = await self.org()
        except Exception as e:  # noqa: BLE001
            self.log(f"final summary read failed: {e}", "warn")
            return
        emps = [e for e in org["employees"] if e["status"] != "terminated"]
        done_goals = sum(1 for i in self.goal_runs.values() if i["status"] in ("done", "succeeded"))
        self.log("================ RUN SUMMARY ================")
        self.log(f"company:    {self.co}")
        self.log(f"headcount:  {len(emps)} ({len(self.leads(org))} leads, {len(org['teams'])} teams)")
        self.log(f"goals:      {done_goals}/{len(self.goal_runs)} CEO goals done")
        done_tasks = sum(1 for t in org["tasks"] if t["status"] in ("done", "DONE"))
        self.log(f"tasks:      {done_tasks}/{len(org['tasks'])} done")
        self.log(f"artifacts:  {len(org['artifacts'])}")
        self.log(f"spend:      ${org['spend_cents']/100:,.2f}")
        self.log("=============================================")

    def _build_report(self) -> None:
        self.log(f"building HTML report for {self.co} -> {REPORT_DIR}")
        env = dict(os.environ)
        env["LUMEN_REPORT_DIR"] = str(REPORT_DIR)
        env.setdefault("OPERATOR_LEDGER_DSN", co_mod.LEDGER_DSN)
        env["PODIUM_WORKDIR"] = str(WORKDIR)
        try:
            r = subprocess.run(
                [sys.executable, str(_ROOT / "scripts" / "build_lumen_report.py"), self.co],
                env=env,
                cwd=str(_ROOT),
                capture_output=True,
                text=True,
                timeout=600,
            )
            if r.returncode == 0:
                self.log(f"report written under {REPORT_DIR}")
                if r.stdout.strip():
                    self.log(r.stdout.strip().splitlines()[-1])
            else:
                self.log(f"report build failed rc={r.returncode}: {r.stderr[-800:]}", "warn")
        except Exception as e:  # noqa: BLE001
            self.log(f"report build error: {e}", "warn")


async def _main() -> None:
    run = FullRun()
    try:
        await run.run()
    except KeyboardInterrupt:
        run.log("interrupted — building report on partial progress")
        run._build_report()
    except Exception as e:  # noqa: BLE001
        import traceback

        run.log(f"FATAL: {e}\n{traceback.format_exc()}", "error")


if __name__ == "__main__":
    print(f"[{_now_hhmmss()}] full_company_run starting — single input: {MISSION!r}", flush=True)
    asyncio.run(_main())
