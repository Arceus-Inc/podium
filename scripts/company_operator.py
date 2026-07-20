"""Company operator — keep an autonomous podium company running like a real startup.

Founds one company, then never stops: it auto-approves the CEO's workforce plans and staffing
amendments (so the org GROWS toward a target headcount with real leads), runs several product goals
in PARALLEL — each as its own delegation run under a real (non-CEO) lead who forms a mission team —
and, when a goal finishes, issues the next one from a roadmap (a stand-in for horizon's "next
decision") so work continues sprint after sprint. Everything is tracked to a SQLite ledger and a live
STATUS.md the human can read in the morning.

Actions go through the same governed HTTP doors the cockpit uses; observability reads the engine
ledger (Postgres) directly, as a superuser, for the full org/goal/task/team/spend picture.

Run against a live podium (PODIUM_DEV_BOOTSTRAP=1) with the embedded conductor on MAX_TICKS=0.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import sqlite3
import traceback
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import asyncpg
import httpx

BASE = os.environ.get("PODIUM_BASE", "http://127.0.0.1:8901")
LEDGER_DSN = os.environ.get("OPERATOR_LEDGER_DSN", "postgresql://postgres@127.0.0.1:5432/podium")
OUT = Path(__file__).resolve().parent.parent / ".monitor-runs" / "overnight"
CTX_FILE = OUT / "context.json"
DB_FILE = OUT / "operator.db"
STATUS_FILE = OUT / "STATUS.md"

TARGET_HEADCOUNT = int(os.environ.get("OPERATOR_TARGET_HEADCOUNT", "12"))
MAX_HEADCOUNT = int(os.environ.get("OPERATOR_MAX_HEADCOUNT", "18"))
# Below this, ramp up to form a couple of viable pods; at/above it, only hire on REAL demand
# (an open staffing request from a lead). Growth is PULL, not push.
MIN_VIABLE_HEADCOUNT = int(os.environ.get("OPERATOR_MIN_VIABLE_HEADCOUNT", "8"))
MAX_ACTIVE_GOALS = int(os.environ.get("OPERATOR_MAX_ACTIVE_GOALS", "3"))
DELEGATION_SPEND_LIMIT_CENTS = 5_000_000  # generous per-goal budget

# A goal run is RETIRED (its active-goal slot freed) once it reaches any of these. 'done' is the
# engine's authoritative ledger roll-up; 'stranded' is a delegation that gave up and was shelved.
TERMINAL_GOAL_STATUSES = ("succeeded", "failed", "canceled", "timed_out", "done", "stranded")

MISSION = os.environ.get(
    "OPERATOR_MISSION",
    "Found and operate 'Clipper', building 'Cursor for video editors' — an AI-native video editing "
    "tool where editors work on a timeline with an AI copilot that suggests cuts, edits from the "
    "transcript, and automates tedious editing. Staff the company to design, build, test, and "
    "document the product in parallel, with engineering, AI, and design leads coordinating their "
    "own teams across the editor UI, the AI edit-assist service, and the render pipeline.",
)

# The rolling product roadmap — each becomes a goal + a delegation run. When exhausted, the operator
# generates follow-on iterations so the company never runs out of work (no ultimate DoD). The order
# INTERLEAVES disciplines (frontend / ai-backend / design / marketing / analytics) so the goals that
# are active concurrently fan out to DIFFERENT discipline leads and their teams work in parallel,
# rather than piling three frontend builds onto a single lead.
ROADMAP: list[tuple[str, str]] = [
    ("Timeline editor core",
     "Deliver the core video timeline editor (web): multi-track timeline with clips, a playhead, "
     "trim/split/move on clips, zoom, and a preview pane wired to a sample video. Ship runnable npm "
     "scripts and unit + Playwright e2e tests with captured evidence."),
    ("AI edit-assist service",
     "Deliver the AI edit-assist backend service: an API that takes a video transcript + timeline "
     "and returns suggested cuts (remove filler words / silences) and a tightened edit list, with a "
     "clear request/response schema. Ship runnable npm scripts and unit tests with captured "
     "evidence; stub the model call behind an interface so it runs offline in tests."),
    ("Editor design system",
     "Deliver the editor's design system: a professional dark theme, color + spacing + typography "
     "tokens, and base components (toolbar, panel, timeline track, transport controls) with docs. "
     "Ship runnable npm scripts and unit tests with captured evidence."),
    ("Transcript-based editing",
     "Deliver transcript-based editing: render the video's transcript as editable text where "
     "deleting a sentence removes the matching timeline range, and edits stay in sync with the "
     "timeline. Ship runnable npm scripts and unit + Playwright e2e tests with captured evidence."),
    ("Clipper brand & positioning guide",
     "Deliver a brand and positioning guide (no code) for 'Cursor for video editors': the "
     "positioning statement, tone-of-voice principles, naming, and example taglines and marketing "
     "copy. Produce a well-structured written guide document with concrete examples."),
    ("Privacy-first editor analytics",
     "Deliver a privacy-first editor analytics module: a small event-tracking API for editor actions "
     "(cut/split/export), a localStorage buffer, and a summary view — no third-party network calls. "
     "Ship runnable npm scripts and unit tests with captured evidence."),
    ("Export & render pipeline",
     "Deliver the export/render pipeline: take an edit list + source clips and produce a render plan "
     "(ffmpeg-style command list) plus a progress model; stub the actual encode behind an interface "
     "so it runs offline. Ship runnable npm scripts and unit tests with captured evidence."),
    ("Clipper landing site",
     "Deliver the marketing landing site: hero pitching 'Cursor for video editors', a feature "
     "section per capability (timeline, AI cuts, transcript editing, export), responsive layout, and "
     "strong accessibility. Ship runnable npm scripts and Playwright e2e tests with captured evidence."),
]


def now() -> str:
    return datetime.now(UTC).isoformat()


def hhmmss() -> str:
    return datetime.now(UTC).isoformat()[11:19]


class Operator:
    def __init__(self) -> None:
        self.ws = ""
        self.co = ""
        self.token = ""
        self.http: httpx.AsyncClient | None = None
        self.pg: asyncpg.Pool | None = None
        self.db: sqlite3.Connection | None = None
        self.started = datetime.now(UTC)
        self.cycle = 0
        self._roadmap_i = 0
        self._iteration = 1  # once roadmap is exhausted, we cycle it as "v2, v3, …"
        # goal_id -> {"title","run_id","lead","status"}
        self.goal_runs: dict[str, dict[str, Any]] = {}
        self._approved_plans: set[str] = set()
        self._plan_attempts: dict[str, int] = {}
        self._expansion_inflight = False
        self._founded = False  # set once the first real workforce plan is approved
        self.roadmap: list[tuple[str, str]] = []  # the system's own roadmap (LLM-generated at startup)

    # ---- infra ----------------------------------------------------------
    def setup_db(self) -> None:
        OUT.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(DB_FILE)
        self.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS runs(run_id TEXT PRIMARY KEY, kind TEXT, goal_id TEXT,
                lead TEXT, directive TEXT, status TEXT, error TEXT, created_at TEXT, updated_at TEXT);
            CREATE TABLE IF NOT EXISTS goals(goal_id TEXT PRIMARY KEY, title TEXT, source TEXT,
                status TEXT, created_at TEXT, done_at TEXT);
            CREATE TABLE IF NOT EXISTS approvals(plan_id TEXT PRIMARY KEY, kind TEXT, employees INT,
                grants INT, detail TEXT, at TEXT);
            CREATE TABLE IF NOT EXISTS snapshots(at TEXT, headcount INT, leads INT, teams INT,
                goals_active INT, goals_done INT, open_tasks INT, done_tasks INT, running_beats INT,
                artifacts INT, spend_cents INT, json TEXT);
            CREATE TABLE IF NOT EXISTS oplog(at TEXT, level TEXT, msg TEXT);
            """
        )
        self.db.commit()

    def log(self, msg: str, level: str = "info") -> None:
        line = f"[{hhmmss()}] {level:5} {msg}"
        print(line, flush=True)
        if self.db is not None:
            with contextlib.suppress(Exception):
                self.db.execute("INSERT INTO oplog VALUES(?,?,?)", (now(), level, msg))
                self.db.commit()

    async def api(self, method: str, path: str, *, company_scoped: bool = True,
                  json_body: Any = None) -> Any:
        assert self.http is not None
        url = (f"/v1/workspaces/{self.ws}/companies/{self.co}{path}" if company_scoped else path)
        r = await self.http.request(method, url,
                                    headers={"Authorization": f"Bearer {self.token}"},
                                    json=json_body)
        r.raise_for_status()
        if r.content:
            return r.json()
        return None

    # ---- bootstrap ------------------------------------------------------
    async def ensure_company(self) -> None:
        # reuse a prior company if it's still valid (survives operator restarts)
        if CTX_FILE.exists():
            try:
                ctx = json.loads(CTX_FILE.read_text())
                self.ws, self.co, self.token = ctx["ws"], ctx["co"], ctx["token"]
                await self.api("GET", "/status")  # probe visibility
                self.log(f"reusing company {self.co}")
                return
            except Exception:
                self.log("prior company invalid — bootstrapping fresh", "warn")
        r = await self.http.post("/v1/dev/bootstrap", json={"name": "lumen"})  # type: ignore[union-attr]
        r.raise_for_status()
        d = r.json()
        self.ws, self.co, self.token = d["workspace_id"], d["company_id"], d["token"]
        CTX_FILE.write_text(json.dumps({"ws": self.ws, "co": self.co, "token": self.token}))
        self.log(f"bootstrapped company {self.co}")

    async def submit_run(self, kind: str, directive: str, *, goal_id: str | None = None,
                         lead: str | None = None, max_team_size: int | None = None) -> str | None:
        body: dict[str, Any] = {"directive": directive, "idempotency_key": str(uuid.uuid4()),
                                "execution_mode": kind}
        if kind == "delegation":
            body.update(lead=lead, goal_id=goal_id, max_team_size=max_team_size or 4,
                        spend_limit_cents=DELEGATION_SPEND_LIMIT_CENTS)
        try:
            run = await self.api("POST", f"/v1/companies/{self.co}/runs",
                                  company_scoped=False, json_body=body)
        except httpx.HTTPError as e:
            self.log(f"submit {kind} run failed: {e}", "warn")
            return None
        rid = run["id"]
        assert self.db is not None
        self.db.execute("INSERT OR REPLACE INTO runs VALUES(?,?,?,?,?,?,?,?,?)",
                        (rid, kind, goal_id, lead, directive[:200], run["status"], None, now(), now()))
        self.db.commit()
        self.log(f"submitted {kind} run {rid[:8]}" + (f" goal={goal_id[:8]} lead={lead}" if goal_id else ""))
        return rid

    async def run_status(self, rid: str) -> dict[str, Any] | None:
        try:
            return await self.api("GET", f"/v1/companies/{self.co}/runs/{rid}", company_scoped=False)
        except httpx.HTTPError:
            return None

    # ---- ledger reads ---------------------------------------------------
    async def q(self, sql: str, *args: Any) -> list[asyncpg.Record]:
        assert self.pg is not None
        async with self.pg.acquire() as c:
            return await c.fetch(sql, *args)

    async def org(self) -> dict[str, Any]:
        co = uuid.UUID(self.co)
        emps = await self.q(
            "select e.id,e.name,e.role,e.reports_to,e.status, "
            "coalesce(m.can_lead,false) as can_lead, coalesce(m.max_team_size,0) as team "
            "from employee e left join management_profile m "
            "on m.employee_id=e.id and m.company_id=e.company_id and m.active "
            "where e.company_id=$1 order by e.created_at", co)
        goals = await self.q("select id,title,status,parent_id from goal where company_id=$1", co)
        tasks = await self.q("select assignee_employee_id,status,execution_mode from task where company_id=$1", co)
        teams = await self.q("select id,name,lead_employee_id,goal_id,status from team where company_id=$1", co)
        reqs = await self.q("select id,requested_by_employee_id,status,goal_id from staffing_request where company_id=$1", co)
        arts = await self.q("select id,type,task_id from artifact where company_id=$1", co)
        spend = await self.q("select coalesce(sum(cost_cents),0) c from cost_event where company_id=$1", co)
        return {"employees": emps, "goals": goals, "tasks": tasks, "teams": teams,
                "staffing": reqs, "artifacts": arts, "spend_cents": int(spend[0]["c"]) if spend else 0}

    def leads(self, org: dict[str, Any]) -> list[dict[str, Any]]:
        return [dict(e) for e in org["employees"]
                if e["can_lead"] and e["role"] != "ceo" and e["status"] != "terminated"]

    @staticmethod
    def goal_needs(title: str, brief: str) -> set[str]:
        """Best-effort professions a goal needs, inferred from its title/brief keywords.

        A lead can only decompose work onto its own direct reports, so a goal must be routed to a
        lead whose team actually contains these professions — otherwise the lead's beats fail.
        """
        t = f"{title} {brief}".lower()
        needs: set[str] = set()
        if any(k in t for k in (
            "app", "web app", "ui", "npm", "e2e", "playwright", "editor", "timer", "tracker",
            "component", "package", "module", "localstorage", "frontend", "site", "page",
            "render", "keyboard", "notification", "grid", "dialog",
        )):
            needs.add("frontend_engineer")
        if any(k in t for k in ("storage", "sync", "backend", "api", "persistence", "server", "buffer")):
            needs.add("backend_engineer")
        if any(k in t for k in (
            "design system", "design", "brand", "theme", "typography", "tokens", "spacing",
            "accessible", "accessibility", "ux", "wireframe", "visual",
        )):
            needs.add("designer")
        if any(k in t for k in ("market", "landing", "launch", "content", "seo", "copy", "growth", "campaign")):
            needs.add("marketer")
        if any(k in t for k in ("analytics", "metrics", "research", "analysis", "insight", "survey", "data")):
            needs.add("analyst")
        return needs or {"frontend_engineer"}

    @staticmethod
    def team_roles(org: dict[str, Any], lead_id: str) -> set[str]:
        """Professions among a lead's direct reports (its potential mission-team ICs)."""
        return {e["role"] for e in org["employees"]
                if e["reports_to"] == lead_id and e["status"] != "terminated"}

    @staticmethod
    def bottleneck_professions(org: dict[str, Any]) -> list[tuple[str, int, int]]:
        """Disciplines whose queued delivery work outstrips their IC headcount.

        Returns a ranked list of ``(profession, backlog, ic_count)`` for professions where the
        number of unfinished delivery tasks assigned to that profession is >= 2 per IC. This lets
        growth add capacity where beats are actually queuing up (e.g. a single frontend engineer
        carrying every app build) instead of hiring blindly toward a headcount target.
        """
        by_id = {e["id"]: e for e in org["employees"] if e["status"] != "terminated"}
        ic_count: dict[str, int] = {}
        for e in by_id.values():
            if e["role"] in ("ceo",) or e["can_lead"]:
                continue
            ic_count[e["role"]] = ic_count.get(e["role"], 0) + 1
        backlog: dict[str, int] = {}
        for t in org["tasks"]:
            if t["execution_mode"] != "delivery" or t["status"] not in ("todo", "in_progress"):
                continue
            emp = by_id.get(t["assignee_employee_id"])
            if not emp or emp["role"] == "ceo" or emp["can_lead"]:
                continue
            backlog[emp["role"]] = backlog.get(emp["role"], 0) + 1
        ranked: list[tuple[str, int, int]] = []
        for prof, load in backlog.items():
            heads = ic_count.get(prof, 0)
            if load >= 2 and load >= 2 * max(heads, 1):
                ranked.append((prof, load, heads))
        ranked.sort(key=lambda x: (-(x[1] / max(x[2], 1)), -x[1]))
        return ranked

    def pick_lead_for_goal(self, org: dict[str, Any], leads: list[dict[str, Any]],
                           title: str, brief: str, load: dict[str, int]) -> dict[str, Any] | None:
        """Route a goal to a capable lead, spreading work so teams run in parallel.

        Preference order per lead: (1) does the team FULLY cover the goal's needs — a lead whose
        team has every required profession can execute the goal end-to-end; (2) is it *eligible* at
        all (covers >=1 need — otherwise its beats fail); (3) LEAST-LOADED first so concurrent goals
        fan out to different teams rather than piling onto one; (4) coverage as the final tie-break.
        Preferring full coverage keeps capability correct, while the load term still spreads work
        whenever several leads are equally capable — "a few on one goal, others on others".
        """
        if not leads:
            return None
        needs = self.goal_needs(title, brief)
        scored = []
        for ld in leads:
            roles = self.team_roles(org, ld["id"])
            coverage = len(needs & roles)
            full = 1 if needs and coverage == len(needs) else 0
            eligible = 1 if coverage >= 1 else 0
            scored.append((full, eligible, -load.get(ld["id"], 0), coverage, len(roles), ld))
        scored.sort(key=lambda s: (s[0], s[1], s[2], s[3], s[4]), reverse=True)
        best = scored[0]
        # If nobody can cover ANY need, don't hand it to an incapable lead — wait for a better fit.
        if best[1] == 0:
            return None
        return best[5]

    # ---- daemons --------------------------------------------------------
    async def approvals_daemon(self) -> None:
        while True:
            try:
                plans = await self.api("GET", "/plans")
                headcount = None
                if plans:
                    org = await self.org()
                    headcount = len([e for e in org["employees"] if e["status"] != "terminated"])
                for p in plans or []:
                    if p.get("status") == "proposed" and p["id"] not in self._approved_plans:
                        emps = len(p.get("employees", []))
                        grants = sum(1 for g in p.get("grants", []) if g.get("can_lead"))
                        # Cap the org: once large enough, stop approving plans that would push
                        # headcount past the ceiling (leave them proposed) so it doesn't balloon.
                        if headcount is not None and emps > 0 and headcount + emps > MAX_HEADCOUNT:
                            self._approved_plans.add(p["id"])
                            self._expansion_inflight = False
                            self.log(f"skip plan {p['id'][:8]} — {headcount}+{emps} would exceed max {MAX_HEADCOUNT}")
                            continue
                        self._approved_plans.add(p["id"])
                        try:
                            await self.api("POST", f"/plans/{p['id']}/approve")
                            kind = "amendment" if p.get("revision", 1) > 1 or emps <= 2 else "formation"
                            self._founded = True  # a real org now exists — growth may proceed
                            self._expansion_inflight = False  # this amendment satisfied the ask
                            assert self.db is not None
                            self.db.execute("INSERT OR REPLACE INTO approvals VALUES(?,?,?,?,?,?)",
                                            (p["id"], kind, emps, grants,
                                             json.dumps(p.get("employees", []))[:4000], now()))
                            self.db.commit()
                            self.log(f"APPROVED plan {p['id'][:8]} (+{emps} hires, {grants} leads)")
                        except httpx.HTTPError as e:
                            n = self._plan_attempts.get(p["id"], 0) + 1
                            self._plan_attempts[p["id"]] = n
                            self.log(f"approve {p['id'][:8]} failed (attempt {n}): {e}", "warn")
                            if n < 4:
                                self._approved_plans.discard(p["id"])  # retry a few times
                            else:
                                self.log(f"giving up on plan {p['id'][:8]} after {n} attempts", "warn")
                                self._expansion_inflight = False
            except Exception as e:  # noqa: BLE001
                self.log(f"approvals_daemon: {e}", "warn")
            await asyncio.sleep(5)

    async def growth_daemon(self) -> None:
        # Grow the permanent org: once the company is FOUNDED (first plan approved), whenever
        # headcount is below target or a lead has filed an open staffing request, submit ONE
        # expansion formation run and wait for its amendment to be approved before considering
        # another. Never fire before founding (the mission formation handles that) or while an
        # expansion is already in flight — otherwise formation runs pile up.
        while True:
            try:
                if not self._founded or self._expansion_inflight:
                    await asyncio.sleep(15)
                    continue
                org = await self.org()
                emps = [e for e in org["employees"] if e["status"] != "terminated"]
                headcount = len(emps)
                open_reqs = [r for r in org["staffing"] if str(r["status"]).lower() == "open"]
                bottlenecks = self.bottleneck_professions(org)
                if headcount >= MAX_HEADCOUNT:
                    # at the hard cap — any expansion plan will be rejected on approval, so
                    # don't waste formation beats churning against the ceiling.
                    await asyncio.sleep(30)
                    continue
                # PULL-BASED growth (audit A4): hire only on REAL demand, never on a push signal.
                #  (a) a lead filed an open staffing_request (someone actually asked), or
                #  (b) initial ramp: still below a minimal viable size AND nobody is idle.
                # Never hire while ICs already sit idle with no task — that push-growth (target
                # headcount / bottleneck heuristics) is how the org ballooned to 18 with people who
                # never did anything. bottlenecks still enrich the directive below, just don't trigger.
                assigned = {t["assignee_employee_id"] for t in org["tasks"] if t["assignee_employee_id"]}
                idle_ics = [
                    e for e in emps
                    if e["role"] != "ceo" and not e["can_lead"] and e["id"] not in assigned
                ]
                want_growth = bool(open_reqs) or (
                    headcount < MIN_VIABLE_HEADCOUNT and len(idle_ics) < 2
                )
                if want_growth:
                    self._expansion_inflight = True
                    bottleneck_line = ""
                    if bottlenecks:
                        parts = ", ".join(
                            f"{prof} ({load} queued task(s) / {heads} IC(s))"
                            for prof, load, heads in bottlenecks[:3]
                        )
                        bottleneck_line = (
                            " These disciplines are OVERLOADED and are the current throughput "
                            f"bottleneck: {parts}. PRIORITIZE adding more ICs in these professions "
                            "under their existing discipline lead so the queued work parallelizes "
                            "across several people instead of one."
                        )
                    directive = (
                        "Expand the permanent workforce so the company is fully staffed for its "
                        f"mission and roadmap. Current permanent headcount is {headcount}; the "
                        f"company should be about {TARGET_HEADCOUNT} people. Propose amendments that "
                        "satisfy EVERY open staffing request (use each staffing_request_id), and add "
                        "the ICs the roadmap needs so several CROSS-FUNCTIONAL PODS can each ship a "
                        "goal end-to-end in parallel. Build balanced pods: each pod is ONE lead plus "
                        "a MIX of a designer, one or two frontend engineers, and a QA/test analyst "
                        "(add a backend engineer or marketer where the goals need it) — do NOT "
                        "create single-discipline silo teams. First BALANCE existing pods by adding "
                        "the missing profession UNDER an existing pod lead (set reports_to to that "
                        "lead); reuse existing leads and only add a NEW pod lead when there is enough "
                        "parallel work for another full balanced pod. NEVER leave an IC reporting to "
                        "the CEO. Only pod leads report to the CEO. Keep the org non-flat and at most "
                        "two layers below the CEO."
                        + bottleneck_line
                    )
                    await self.submit_run("formation", directive)
                    # wait (up to ~150s) for the approval daemon to clear the flag, else clear it
                    for _ in range(30):
                        if not self._expansion_inflight:
                            break
                        await asyncio.sleep(5)
                    self._expansion_inflight = False
            except Exception as e:  # noqa: BLE001
                self.log(f"growth_daemon: {e}", "warn")
                self._expansion_inflight = False
            await asyncio.sleep(30)

    async def generate_roadmap(self, done_titles: list[str]) -> None:
        """The system decides its OWN roadmap: an LLM decomposes the mission into product goals.

        No hardcoded feature list — the mission goes in, a structured goal list comes out. Falls back
        to the built-in ROADMAP only if the model call fails, so a run never stalls.
        """
        key = os.environ.get("AZURE_OPENAI_API_KEY")
        base = os.environ.get("AZURE_OPENAI_BASE_URL")
        dep = os.environ.get("AZURE_OPENAI_DEPLOYMENT")
        if not (key and base and dep):
            self.log("no Azure keys in env — using built-in roadmap", "warn")
            self.roadmap = list(ROADMAP)
            return
        done = ""
        if done_titles:
            done = "\n\nAlready delivered (build on these, do not repeat):\n- " + "\n- ".join(done_titles)
        prompt = (
            f"You are the founding CEO of a startup. Your mission:\n{MISSION}\n\n"
            "Produce a product roadmap of 10 concrete, independently-deliverable product goals that "
            "build this product incrementally toward a real, usable v1. Interleave disciplines "
            "(frontend, AI/backend, design, marketing, analytics) so different specialist leads work "
            "in parallel. Each goal has a short title and a 2-3 sentence brief that names the concrete "
            "deliverable and how 'done' is proven (runnable npm scripts + unit/e2e tests + captured "
            "evidence for code; a structured written document for non-code work). Order them so the "
            "foundational build comes before what depends on it." + done +
            '\n\nReturn ONLY JSON: {"goals":[{"title":"...","brief":"..."}]}'
        )
        body = {
            "model": dep,
            "messages": [{"role": "user", "content": prompt}],
            "response_format": {"type": "json_object"},
        }
        async with httpx.AsyncClient(timeout=180.0) as c:
            r = await c.post(
                f"{base.rstrip('/')}/chat/completions",
                headers={"Authorization": f"Bearer {key}"},
                json=body,
            )
            r.raise_for_status()
            content = r.json()["choices"][0]["message"]["content"]
        goals = json.loads(content).get("goals", [])
        self.roadmap = [(g["title"], g["brief"]) for g in goals if g.get("title") and g.get("brief")]
        if not self.roadmap:
            raise ValueError("model returned an empty roadmap")
        self.log(f"CEO/LLM decomposed the mission into a {len(self.roadmap)}-goal roadmap:")
        for i, (t, _) in enumerate(self.roadmap, 1):
            self.log(f"  {i:>2}. {t}")

    def brief_for_title(self, title: str) -> str:
        base = title.split(" (v")[0]
        for t, b in (self.roadmap or ROADMAP):
            if t == base:
                return b
        return (f"Deliver '{title}' to a high, tested standard. Ship runnable npm scripts and "
                "unit + e2e tests with captured evidence.")

    def rehydrate_goals(self) -> None:
        # Restart-safety: rebuild goal_runs from the sqlite tracker so a relaunch resumes the same
        # goals/runs instead of creating duplicates.
        assert self.db is not None
        rows = self.db.execute("SELECT goal_id,title,status FROM goals").fetchall()
        for gid, title, gstatus in rows:
            r = self.db.execute(
                "SELECT run_id,status FROM runs WHERE goal_id=? AND kind='delegation' "
                "ORDER BY updated_at DESC LIMIT 1", (gid,)).fetchone()
            run_id, rstatus = (r[0], r[1]) if r else (None, None)
            self.goal_runs[gid] = {"title": title, "brief": self.brief_for_title(title),
                                   "run_id": run_id, "lead": None,
                                   "status": rstatus or gstatus or "pending"}
        known = {i["title"].split(" (v")[0] for i in self.goal_runs.values()}
        self._roadmap_i = sum(1 for t, _ in ROADMAP if t in known)
        if self.goal_runs:
            self.log(f"rehydrated {len(self.goal_runs)} goals from tracker")

    def next_roadmap_item(self) -> tuple[str, str]:
        rm = self.roadmap or ROADMAP  # the system's own roadmap; built-in only as a fallback
        title, brief = rm[self._roadmap_i % len(rm)]
        cycled = self._roadmap_i >= len(rm)
        self._roadmap_i += 1
        if cycled:
            title = f"{title} (v{self._iteration + 1})"
            brief = (f"Iterate on the prior '{title}': add a meaningful improvement (new feature, "
                     "polish, accessibility, or performance) and keep all tests green. " + brief)
            if self._roadmap_i % len(rm) == 0:
                self._iteration += 1
        return title, brief

    async def create_goal(self, title: str) -> str | None:
        try:
            node = await self.api("POST", "/goals", json_body={"title": title, "level": "team"})
            return node["id"] if node else None
        except httpx.HTTPError as e:
            self.log(f"create_goal failed: {e}", "warn")
            return None

    async def delegation_daemon(self) -> None:
        # Keep up to MAX_ACTIVE_GOALS product goals in flight, each a delegation run under the lead
        # whose team can actually deliver it (capability match). When a run finishes, retire the
        # goal and let goal_daemon queue the next.
        while True:
            try:
                org = await self.org()
                leads = self.leads(org)
                # AUTHORITATIVE completion first: the engine flips the ledger goal to 'done' when its
                # delegation-root task lands done (chorus goal roll-up). Trust that over the product-run
                # status, which can loop or false-pass. asyncpg returns goal ids as UUID objects; the
                # goal_runs keys are strings, so normalise with str().
                ledger_goal_status = {str(g["id"]): g["status"] for g in org["goals"]}
                # A goal whose delegation gave up — root delegation task BLOCKED with an active
                # 'integrate_iteration_exhausted' recovery — is STRANDED (nothing converged, e.g. a
                # subjective deliverable the reviewer kept rejecting). Shelve it and move on rather than
                # letting a stuck goal clog an active-goal slot forever.
                stranded_rows = await self.q(
                    "select distinct t.goal_id from task t "
                    "join recovery_action ra on ra.source_task_id=t.id and ra.company_id=t.company_id "
                    "where t.company_id=$1 and t.execution_mode='delegation' and t.parent_id is null "
                    "and t.status='blocked' and ra.status='active' "
                    "and ra.cause='integrate_iteration_exhausted'",
                    uuid.UUID(self.co))
                stranded = {str(r["goal_id"]) for r in stranded_rows if r["goal_id"]}
                # refresh statuses of in-flight goal runs
                for gid, info in list(self.goal_runs.items()):
                    if info["status"] in TERMINAL_GOAL_STATUSES:
                        continue
                    # (a) ledger goal rolled up to done -> retire it; goal_daemon queues the next
                    if ledger_goal_status.get(gid) == "done":
                        info["status"] = "done"
                        assert self.db is not None
                        self.db.execute("UPDATE goals SET status='done',done_at=? WHERE goal_id=?",
                                        (now(), gid))
                        self.db.commit()
                        self.log(f"goal '{info['title'][:40]}' COMPLETED (ledger roll-up)")
                        continue
                    # (a2) delegation stranded (never converged) -> shelve it, free the slot
                    if gid in stranded:
                        info["status"] = "stranded"
                        assert self.db is not None
                        self.db.execute("UPDATE goals SET status='stranded',done_at=? WHERE goal_id=?",
                                        (now(), gid))
                        self.db.commit()
                        self.log(f"goal '{info['title'][:40]}' SHELVED (delegation stranded — moving on)", "warn")
                        continue
                    # (b) fall back to the delegation product-run terminal status
                    if info.get("run_id"):
                        st = await self.run_status(info["run_id"])
                        if st and st["status"] in ("succeeded", "failed", "canceled", "timed_out"):
                            info["status"] = st["status"]
                            assert self.db is not None
                            self.db.execute("UPDATE runs SET status=?,updated_at=? WHERE run_id=?",
                                            (st["status"], now(), info["run_id"]))
                            self.db.execute("UPDATE goals SET status='done',done_at=? WHERE goal_id=?",
                                            (now(), gid))
                            self.db.commit()
                            self.log(f"goal '{info['title'][:40]}' finished ({st['status']})")
                active = [i for i in self.goal_runs.values()
                          if i["status"] not in TERMINAL_GOAL_STATUSES]
                running = [i for i in active if i.get("run_id")]
                # current load per lead (running goals already assigned to them)
                load: dict[str, int] = {}
                for i in running:
                    if i.get("lead"):
                        load[i["lead"]] = load.get(i["lead"], 0) + 1
                # launch delegation runs for goals that have none yet, routed by capability
                if leads:
                    for gid, info in self.goal_runs.items():
                        if info.get("run_id") is None and len(running) < MAX_ACTIVE_GOALS:
                            lead = self.pick_lead_for_goal(org, leads, info["title"], info["brief"], load)
                            if lead is None:
                                continue  # no capable lead free right now; retry next cycle
                            rid = await self.submit_run("delegation", info["brief"], goal_id=gid,
                                                        lead=lead["id"], max_team_size=int(lead["team"]) or 4)
                            if rid:
                                info["run_id"] = rid
                                info["lead"] = lead["id"]
                                info["status"] = "running"
                                running.append(info)
                                load[lead["id"]] = load.get(lead["id"], 0) + 1
                                self.log(f"delegated '{info['title'][:34]}' -> {lead['name']}")
            except Exception as e:  # noqa: BLE001
                self.log(f"delegation_daemon: {e}\n{traceback.format_exc()}", "warn")
            await asyncio.sleep(20)

    async def goal_daemon(self) -> None:
        # Never stop: keep the pipeline topped up with the next roadmap goal (a stand-in for
        # horizon's "next decision") whenever there is spare capacity.
        while True:
            try:
                active = [i for i in self.goal_runs.values()
                          if i["status"] not in TERMINAL_GOAL_STATUSES]
                if len(active) < MAX_ACTIVE_GOALS:
                    title, brief = self.next_roadmap_item()
                    existing_titles = {i["title"] for i in self.goal_runs.values()}
                    if title in existing_titles:
                        await asyncio.sleep(25)
                        continue
                    gid = await self.create_goal(title)
                    if gid:
                        self.goal_runs[gid] = {"title": title, "brief": brief, "run_id": None,
                                               "lead": None, "status": "pending"}
                        assert self.db is not None
                        self.db.execute("INSERT OR REPLACE INTO goals VALUES(?,?,?,?,?,?)",
                                        (gid, title, "roadmap", "pending", now(), None))
                        self.db.commit()
                        self.log(f"queued goal '{title}' ({gid[:8]})")
            except Exception as e:  # noqa: BLE001
                self.log(f"goal_daemon: {e}", "warn")
            await asyncio.sleep(25)

    async def metrics_daemon(self) -> None:
        while True:
            try:
                self.cycle += 1
                org = await self.org()
                emps = [e for e in org["employees"] if e["status"] != "terminated"]
                leads = self.leads(org)
                tasks = org["tasks"]
                done_tasks = sum(1 for t in tasks if t["status"] in ("done", "DONE"))
                open_tasks = sum(1 for t in tasks if t["status"] not in ("done", "DONE", "cancelled", "rejected"))
                running = sum(1 for t in tasks if t["status"] in ("in_progress", "IN_PROGRESS"))
                goals_done = sum(1 for i in self.goal_runs.values() if i["status"] in ("done", "succeeded"))
                goals_active = len([i for i in self.goal_runs.values()
                                    if i["status"] not in TERMINAL_GOAL_STATUSES])
                assert self.db is not None
                self.db.execute(
                    "INSERT INTO snapshots VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                    (now(), len(emps), len(leads), len(org["teams"]), goals_active, goals_done,
                     open_tasks, done_tasks, running, len(org["artifacts"]), org["spend_cents"], "{}"))
                self.db.commit()
                self.write_status(org, emps, leads, done_tasks, open_tasks, running)
            except Exception as e:  # noqa: BLE001
                self.log(f"metrics_daemon: {e}", "warn")
            await asyncio.sleep(15)

    def write_status(self, org: dict[str, Any], emps: list[Any], leads: list[Any],
                     done_tasks: int, open_tasks: int, running: int) -> None:
        up = datetime.now(UTC) - self.started
        by_id = {e["id"]: e for e in org["employees"]}
        lines: list[str] = []
        lines.append(f"# Lumen — live company status")
        lines.append(f"_updated {now()} · uptime {str(up).split('.')[0]} · company `{self.co}`_\n")
        lines.append(f"- **headcount:** {len(emps)} (target {TARGET_HEADCOUNT}) · **leads:** {len(leads)} "
                     f"· **teams:** {len(org['teams'])}")
        lines.append(f"- **tasks:** {done_tasks} done / {open_tasks} open ({running} running) "
                     f"· **artifacts:** {len(org['artifacts'])} · **spend:** ${org['spend_cents']/100:,.2f}")
        goals_done = sum(1 for i in self.goal_runs.values() if i['status'] in ('done', 'succeeded'))
        lines.append(f"- **goals:** {goals_done} done / {len(self.goal_runs)} total")
        open_reqs = [r for r in org["staffing"] if r["status"] in ("open", "OPEN")]
        if open_reqs:
            lines.append(f"- **open staffing requests:** {len(open_reqs)}")
        # org chart
        lines.append("\n## Org")
        ceo = [e for e in org["employees"] if e["role"] == "ceo"]
        def render(emp_id: str, depth: int) -> None:
            e = by_id.get(emp_id)
            if not e:
                return
            tag = " ★lead" if e["can_lead"] and e["role"] != "ceo" else ""
            lines.append(f"{'  ' * depth}- {e['name']} `{e['role']}`{tag} ({e['status']})")
            for child in org["employees"]:
                if child["reports_to"] == emp_id:
                    render(child["id"], depth + 1)
        for c in ceo:
            render(c["id"], 0)
        # goals + teams
        lines.append("\n## Goals in flight")
        for gid, info in self.goal_runs.items():
            team = next((t for t in org["teams"] if t["goal_id"] and str(t["goal_id"]) == gid), None)
            members = ""
            if team:
                mem = [by_id.get(str(m["employee_id"]), {}).get("name", "?")
                       for m in []]  # team_member fetched separately if needed
                members = f" · team '{team['name']}' ({team['status']})"
            lines.append(f"- **{info['title']}** — {info['status']}"
                         + (f" · lead {by_id.get(info['lead'],{}).get('name', info['lead'])}" if info.get('lead') else "")
                         + members)
        # tasks by assignee
        lines.append("\n## Who's working on what")
        by_assignee: dict[str, list[str]] = {}
        for t in org["tasks"]:
            a = t["assignee_employee_id"] or "(unassigned)"
            by_assignee.setdefault(a, []).append(t["status"])
        for a, sts in sorted(by_assignee.items(), key=lambda kv: -len(kv[1])):
            nm = by_id.get(a, {}).get("name", a)
            done = sum(1 for s in sts if s in ("done", "DONE"))
            lines.append(f"- {nm}: {len(sts)} tasks ({done} done)")
        STATUS_FILE.write_text("\n".join(lines), encoding="utf-8")

    # ---- main -----------------------------------------------------------
    async def run(self) -> None:
        self.setup_db()
        self.http = httpx.AsyncClient(base_url=BASE, timeout=30.0)
        self.pg = await asyncpg.create_pool(LEDGER_DSN, min_size=1, max_size=4)
        await self.ensure_company()
        self.rehydrate_goals()
        # if the company already has a real org, it's founded (growth may proceed on restart)
        try:
            org0 = await self.org()
            if len([e for e in org0["employees"] if e["status"] != "terminated"]) > 1:
                self._founded = True
        except Exception:  # noqa: BLE001
            pass
        self.log(f"MISSION: {MISSION[:80]}…")
        # The system decides its own roadmap from the mission — no hardcoded feature list.
        if not self.roadmap:
            try:
                await self.generate_roadmap([i["title"] for i in self.goal_runs.values()])
            except Exception as e:  # noqa: BLE001
                self.log(f"roadmap generation failed ({e}); using built-in fallback", "warn")
                self.roadmap = list(ROADMAP)
        # kick off formation for the founding org (only for a genuinely fresh company)
        if not self.goal_runs and not self._founded:
            await self.submit_run("formation", MISSION)
        # daemons
        tasks = [
            asyncio.create_task(self.approvals_daemon()),
            asyncio.create_task(self.growth_daemon()),
            asyncio.create_task(self.delegation_daemon()),
            asyncio.create_task(self.goal_daemon()),
            asyncio.create_task(self.metrics_daemon()),
        ]
        self.log("operator running — daemons: approvals, growth, delegation, goals, metrics")
        await asyncio.gather(*tasks)


async def main() -> None:
    op = Operator()
    try:
        await op.run()
    except KeyboardInterrupt:
        op.log("interrupted")
    except Exception as e:  # noqa: BLE001
        op.log(f"FATAL: {e}\n{traceback.format_exc()}", "error")


if __name__ == "__main__":
    asyncio.run(main())
