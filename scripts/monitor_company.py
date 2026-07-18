"""Drive one full company run end-to-end and record every layer of the ops.

Sequence (the operator, i.e. this script, drives it — horizon does not auto-spawn delegation):
  1. POST /v1/dev/bootstrap                      -> mint {workspace, company, token}
  2. POST /runs execution_mode=formation         -> root goal + CEO proposes a workforce plan
  3. GET  /plans  (wait for status=proposed)     -> AUTO-APPROVE and record the PlanView
  4. POST /runs execution_mode=delegation        -> lead forms a mission team, delegates to ICs
  5. Tail the company SSE spine + poll every read surface throughout
  6. Write a JSONL timeline, a raw events log, and a markdown report grouped by the 7 dimensions

Nothing here mutates the engines directly — it only calls the same governed HTTP doors the cockpit
uses. Run against a live podium (default http://127.0.0.1:8901) with PODIUM_DEV_BOOTSTRAP=1.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import uuid
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

BASE = os.environ.get("PODIUM_BASE", "http://127.0.0.1:8901")
OBJECTIVE = os.environ.get("PODIUM_OBJECTIVE", "Build a markdown app for calm users.")
DELIVERY_BRIEF = (
    "Deliver a working markdown app for calm users. It must be a minimal, distraction-free "
    "markdown editor with live preview, autosave, and a deliberately calming visual design "
    "(soft palette, generous whitespace, no clutter). Decompose the work across your team, "
    "assign each part with a clear definition of done, and integrate their results into one "
    "coherent deliverable."
)

# time budgets (seconds) — LLM beats on a reasoning model are slow; keep these generous
WAIT_PLAN = int(os.environ.get("PODIUM_WAIT_PLAN", "600"))
WAIT_DELEGATION = int(os.environ.get("PODIUM_WAIT_DELEGATION", "1500"))
SNAPSHOT_EVERY = 8.0

RUN_ID = datetime.now().strftime("%Y%m%d-%H%M%S")
OUT = Path(__file__).resolve().parent.parent / ".monitor-runs" / RUN_ID


def now_iso() -> str:
    return datetime.now(UTC).isoformat()


class Monitor:
    def __init__(self) -> None:
        self.ws: str = ""
        self.co: str = ""
        self.token: str = ""
        self.timeline: list[dict[str, Any]] = []
        self.events: list[dict[str, Any]] = []
        self.plans_recorded: list[dict[str, Any]] = []
        self.runs: dict[str, dict[str, Any]] = {}  # run_id -> {kind, directive, final}
        self.latest: dict[str, Any] = {}  # last value of each polled read surface
        self._stop = asyncio.Event()
        self._last_event_id: str | None = None

    # ---- plumbing -------------------------------------------------------
    def note(self, source: str, **kw: Any) -> None:
        rec = {"t": now_iso(), "source": source, **kw}
        self.timeline.append(rec)
        head = " ".join(f"{k}={v}" for k, v in kw.items() if k in ("kind", "status", "type", "msg", "employee", "run"))
        print(f"[{rec['t'][11:19]}] {source:9} {head}", flush=True)

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(base_url=BASE, timeout=30.0)

    @property
    def auth(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"}

    def cpath(self, path: str) -> str:
        return f"/v1/workspaces/{self.ws}/companies/{self.co}{path}"

    async def get(self, c: httpx.AsyncClient, path: str, company_scoped: bool = True) -> Any:
        url = self.cpath(path) if company_scoped else path
        r = await c.get(url, headers=self.auth)
        r.raise_for_status()
        return r.json()

    # ---- 1. bootstrap ---------------------------------------------------
    async def bootstrap(self, c: httpx.AsyncClient) -> None:
        r = await c.post("/v1/dev/bootstrap", json={"name": "calm-markdown"})
        if r.status_code == 404:
            raise SystemExit("dev bootstrap disabled (PODIUM_DEV_BOOTSTRAP != 1) — cannot mint a playground")
        r.raise_for_status()
        d = r.json()
        self.ws, self.co, self.token = d["workspace_id"], d["company_id"], d["token"]
        self.note("bootstrap", msg="minted playground", ws=self.ws, co=self.co)

    # ---- 2/4. runs ------------------------------------------------------
    async def submit_run(self, c: httpx.AsyncClient, *, kind: str, directive: str, **params: Any) -> str:
        body = {"directive": directive, "idempotency_key": str(uuid.uuid4()), "execution_mode": kind, **params}
        r = await c.post(f"/v1/companies/{self.co}/runs", headers=self.auth, json=body)
        r.raise_for_status()
        run = r.json()
        rid = run["id"]
        self.runs[rid] = {"kind": kind, "directive": directive, "final": None}
        self.note("run", kind=kind, run=rid, status=run["status"], msg="submitted")
        return rid

    async def run_status(self, c: httpx.AsyncClient, rid: str) -> dict[str, Any]:
        r = await c.get(f"/v1/companies/{self.co}/runs/{rid}", headers=self.auth)
        r.raise_for_status()
        return r.json()

    async def wait_run_terminal(self, c: httpx.AsyncClient, rid: str, budget: int) -> dict[str, Any]:
        terminal = {"succeeded", "failed", "canceled", "timed_out"}
        deadline = asyncio.get_event_loop().time() + budget
        last = None
        while asyncio.get_event_loop().time() < deadline:
            run = await self.run_status(c, rid)
            if run["status"] != (last or {}).get("status"):
                self.note("run", kind=self.runs[rid]["kind"], run=rid, status=run["status"])
            last = run
            if run["status"] in terminal:
                self.runs[rid]["final"] = run
                return run
            await asyncio.sleep(4.0)
        self.note("run", kind=self.runs[rid]["kind"], run=rid, status="watch-timeout", msg="stopped waiting")
        self.runs[rid]["final"] = last
        return last or {}

    # ---- 3. the human approval boundary --------------------------------
    async def wait_and_approve_plan(self, c: httpx.AsyncClient, budget: int) -> dict[str, Any] | None:
        deadline = asyncio.get_event_loop().time() + budget
        while asyncio.get_event_loop().time() < deadline:
            try:
                plans = await self.get(c, "/plans")
            except httpx.HTTPError as e:
                self.note("plans", msg=f"read error {e}")
                await asyncio.sleep(5)
                continue
            self.latest["plans"] = plans
            proposed = [p for p in plans if p.get("status") == "proposed"]
            if proposed:
                plan = proposed[0]
                self.note("approval", msg="proposed plan detected", plan=plan["id"],
                          employees=len(plan.get("employees", [])), grants=len(plan.get("grants", [])))
                # AUTO-ACCEPT on the human's behalf, but record the full card first.
                self.plans_recorded.append({"decided": "approved", "at": now_iso(), "plan": plan})
                r = await c.post(self.cpath(f"/plans/{plan['id']}/approve"), headers=self.auth)
                if r.status_code >= 400:
                    self.note("approval", msg=f"approve failed {r.status_code}: {r.text[:200]}")
                    return plan
                approved = r.json()
                self.plans_recorded[-1]["result"] = approved
                self.note("approval", msg="APPROVED (auto)", plan=plan["id"], status=approved.get("status"))
                return approved
            await asyncio.sleep(5.0)
        self.note("approval", msg="no proposed plan within budget")
        return None

    # ---- 5. the SSE spine ----------------------------------------------
    async def tail_stream(self) -> None:
        async with self.client() as c:
            while not self._stop.is_set():
                headers = dict(self.auth)
                if self._last_event_id is not None:
                    headers["Last-Event-ID"] = self._last_event_id
                try:
                    async with c.stream("GET", f"/v1/companies/{self.co}/stream", headers=headers) as resp:
                        if resp.status_code != 200:
                            await asyncio.sleep(2)
                            continue
                        buffer = ""
                        async for chunk in resp.aiter_text():
                            if self._stop.is_set():
                                return
                            buffer += chunk
                            while "\n\n" in buffer:
                                frame, buffer = buffer.split("\n\n", 1)
                                self._on_frame(frame)
                except (httpx.HTTPError, asyncio.TimeoutError):
                    if self._stop.is_set():
                        return
                    await asyncio.sleep(2)

    def _on_frame(self, frame: str) -> None:
        data = ""
        for line in frame.splitlines():
            if line.startswith("id:"):
                self._last_event_id = line[3:].strip()
            elif line.startswith("data:"):
                data += line[5:].strip()
        if not data:
            return
        try:
            ev = json.loads(data)
        except json.JSONDecodeError:
            return
        self.events.append(ev)
        self.timeline.append({"t": now_iso(), "source": "event", **ev})
        typ = ev.get("type", "")
        emp = ev.get("employee_id") or "-"
        # keep the console readable: only headline the meaningful transitions
        if typ.startswith(("task.", "run.started", "run.done", "run.evaluated", "employee.", "approval.", "budget.")):
            print(f"[{now_iso()[11:19]}] event     {typ:22} emp={emp} task={ev.get('task_id')}", flush=True)

    # ---- periodic snapshots --------------------------------------------
    async def poll_reads(self) -> None:
        async with self.client() as c:
            while not self._stop.is_set():
                for name, path in (
                    ("snapshot", "/cockpit/snapshot"),
                    ("goals", "/goals"),
                    ("workforce", "/workforce"),
                    ("teams", "/teams"),
                    ("capacity", "/capacity"),
                    ("allocation", "/allocation"),
                    ("status", "/status"),
                    ("artifacts", "/artifacts?limit=100"),
                    ("costs", "/costs?by=employee"),
                    ("report", "/report"),
                    ("direction", "/cockpit/direction-report"),
                ):
                    try:
                        self.latest[name] = await self.get(c, path)
                    except httpx.HTTPError:
                        pass
                snap = self.latest.get("snapshot", {}).get("components", {})
                self.timeline.append({"t": now_iso(), "source": "snapshot", "components": snap})
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=SNAPSHOT_EVERY)
                except asyncio.TimeoutError:
                    pass

    # ---- driver ---------------------------------------------------------
    def pick_lead(self, approved_plan: dict[str, Any], roster: list[dict[str, Any]]) -> tuple[str | None, int]:
        """Map the approved plan's can_lead grant -> employee name -> live workforce slug."""
        grants = approved_plan.get("grants", []) if approved_plan else []
        emps = {e["ref"]: e for e in approved_plan.get("employees", [])} if approved_plan else {}
        by_name = {e["name"].lower(): e["id"] for e in roster}
        roster_by_id = {e["id"]: e for e in roster}
        candidates: list[tuple[str, int, str]] = []
        for g in grants:
            if g.get("can_lead") and g.get("max_team_size", 0) >= 1:
                planned = emps.get(g["employee_ref"])
                slug = None
                if planned and planned["name"].lower() in by_name:
                    slug = by_name[planned["name"].lower()]
                elif g["employee_ref"] in roster_by_id:  # grant on an existing employee (a promotion)
                    slug = g["employee_ref"]
                if slug:
                    role = roster_by_id.get(slug, {}).get("role", "").lower()
                    candidates.append((slug, int(g.get("max_team_size") or 3), role))
        # Prefer the actual mission lead (a promoted specialist), never the CEO — the CEO always
        # carries a can_lead grant for her own reports, but delegating to her hides the org layer.
        non_ceo = [c for c in candidates if c[2] != "ceo"]
        pick = non_ceo or candidates
        if pick:
            return pick[0][0], pick[0][1]
        # No in-org lead with a can_lead management profile. Do NOT fall back to the CEO — that
        # would hide the flat-org bug. Return None so the driver reports the gap loudly.
        return None, 3

    async def drive(self) -> None:
        async with self.client() as c:
            await self.bootstrap(c)
            # start the observers now that we have a company + token
            tail = asyncio.create_task(self.tail_stream())
            polls = asyncio.create_task(self.poll_reads())
            try:
                # baseline
                await asyncio.sleep(1.5)
                # 2. formation
                frid = await self.submit_run(c, kind="formation", directive=OBJECTIVE)
                # 3. approve (runs concurrently with the CEO beat producing the plan)
                approved = await self.wait_and_approve_plan(c, WAIT_PLAN)
                await self.wait_run_terminal(c, frid, 60)  # let the formation run settle
                # 4. delegation
                if approved:
                    roster = await self.get(c, "/workforce")
                    goals = await self.get(c, "/goals")
                    root_goal = goals[0]["id"] if goals else None
                    lead, team_size = self.pick_lead(approved, roster)
                    self.note("delegation", msg="planning", lead=lead, team_size=team_size, root_goal=root_goal)
                    if lead and root_goal:
                        drid = await self.submit_run(
                            c, kind="delegation", directive=DELIVERY_BRIEF,
                            lead=lead, goal_id=root_goal, max_team_size=team_size,
                            spend_limit_cents=500000,
                        )
                        await self.wait_run_terminal(c, drid, WAIT_DELEGATION)
                    else:
                        self.note("delegation", msg="skipped — no lead or root goal available")
                else:
                    self.note("delegation", msg="skipped — plan not approved")
                # settle so late events + final rollups land
                await asyncio.sleep(6)
            finally:
                self._stop.set()
                await asyncio.gather(tail, polls, return_exceptions=True)
            await self.final_capture(c)

    async def final_capture(self, c: httpx.AsyncClient) -> None:
        for name, path in (
            ("snapshot", "/cockpit/snapshot"), ("goals", "/goals"), ("workforce", "/workforce"),
            ("teams", "/teams"), ("capacity", "/capacity"), ("allocation", "/allocation"),
            ("status", "/status"), ("overview", "/overview"), ("report", "/report"),
            ("artifacts", "/artifacts?limit=200"), ("costs_employee", "/costs?by=employee"),
            ("costs_model", "/costs?by=model"), ("plans", "/plans"), ("direction", "/cockpit/direction-report"),
        ):
            try:
                self.latest[name] = await self.get(c, path)
            except httpx.HTTPError as e:
                self.latest[name] = {"error": str(e)}
        # per-employee skills + semantic detail
        skills: dict[str, Any] = {}
        semantic: dict[str, Any] = {}
        for e in self.latest.get("workforce", []) or []:
            eid = e["id"]
            try:
                skills[eid] = await self.get(c, f"/employees/{eid}/skills")
            except httpx.HTTPError:
                pass
            try:
                semantic[eid] = await self.get(c, f"/cockpit/semantic/{eid}")
            except httpx.HTTPError:
                pass
        self.latest["skills"] = skills
        self.latest["semantic"] = semantic
        # durable per-run transcripts (dream harness logs)
        logs: dict[str, str] = {}
        for rid in self.runs:
            try:
                r = await c.get(f"/v1/runs/{rid}/logs", headers=self.auth)
                if r.status_code == 200:
                    logs[rid] = r.text
            except httpx.HTTPError:
                pass
        self.latest["run_logs"] = logs

    # ---- reporting ------------------------------------------------------
    def write(self) -> None:
        OUT.mkdir(parents=True, exist_ok=True)
        (OUT / "timeline.jsonl").write_text(
            "\n".join(json.dumps(r, default=str) for r in self.timeline), encoding="utf-8"
        )
        (OUT / "events.jsonl").write_text(
            "\n".join(json.dumps(e, default=str) for e in self.events), encoding="utf-8"
        )
        (OUT / "final_state.json").write_text(json.dumps(self.latest, indent=2, default=str), encoding="utf-8")
        for rid, text in (self.latest.get("run_logs") or {}).items():
            (OUT / f"runlog-{self.runs[rid]['kind']}-{rid[:8]}.txt").write_text(text, encoding="utf-8")
        (OUT / "REPORT.md").write_text(self.report(), encoding="utf-8")
        print(f"\nWrote report + trace to: {OUT}", flush=True)

    def _events_by_type(self) -> dict[str, int]:
        counts: dict[str, int] = defaultdict(int)
        for e in self.events:
            counts[e.get("type", "?")] += 1
        return dict(sorted(counts.items()))

    def _goal_lines(self, nodes: list[dict[str, Any]], depth: int = 0) -> list[str]:
        out = []
        for n in nodes or []:
            out.append(f"{'  ' * depth}- **{n.get('title','?')}** "
                       f"(`{n.get('level')}` · {n.get('status')}"
                       f"{' · owner ' + n['owner'] if n.get('owner') else ''})")
            out.extend(self._goal_lines(n.get("children", []), depth + 1))
        return out

    def report(self) -> str:
        L = self.latest
        ev = self.events
        by_type = self._events_by_type()
        beats = [e for e in ev if e.get("type", "").startswith("run.")]
        beats_by_emp: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for b in beats:
            beats_by_emp[b.get("employee_id") or "(company)"].append(b)
        llm = [e for e in ev if e.get("type") == "llm.call"]
        hired = [e for e in ev if e.get("type") == "employee.hired"]
        approvals = [e for e in ev if e.get("type") == "approval.decided"]
        task_events = [e for e in ev if e.get("type", "").startswith("task.")]

        p: list[str] = []
        p.append(f"# Company ops trace — {RUN_ID}")
        p.append(f"\n**Objective seeded:** {OBJECTIVE}\n")
        p.append(f"- workspace `{self.ws}` · company `{self.co}`")
        p.append(f"- events captured: **{len(ev)}** · beats (run.*): **{len(beats)}** · "
                 f"llm calls: **{len(llm)}** · tasks touched: **{len(task_events)}**")
        comps = (L.get("snapshot") or {}).get("components", {})
        if comps:
            p.append(f"- final snapshot: {json.dumps(comps)}")
        p.append("\n### Runs")
        for rid, meta in self.runs.items():
            fin = meta.get("final") or {}
            p.append(f"- `{meta['kind']}` {rid[:8]} → **{fin.get('status','?')}**"
                     f"{' — ' + fin.get('error') if fin.get('error') else ''}")

        p.append("\n---\n## 1 · Goals → decisions → tasks")
        p.append("\n**Goal tree (final):**")
        p += self._goal_lines(L.get("goals", [])) or ["- (none)"]
        p.append("\n**Task lifecycle events:**")
        tc = defaultdict(int)
        for e in task_events:
            tc[e.get("type")] += 1
        p += [f"- `{k}`: {v}" for k, v in sorted(tc.items())] or ["- (none)"]
        rep = L.get("report") or {}
        if isinstance(rep, dict) and rep:
            p.append(f"\n**Org rollup:** {json.dumps(rep)}")
        direction = (L.get("direction") or {}).get("markdown", "") if isinstance(L.get("direction"), dict) else ""
        p.append("\n**Horizon direction report (decisions):**")
        p.append("```\n" + (direction.strip() or "(empty — horizon wrote no decision story)") + "\n```")

        p.append("\n---\n## 2 · Human approvals (auto-accepted, recorded)")
        if self.plans_recorded:
            for rec in self.plans_recorded:
                plan = rec["plan"]
                p.append(f"\n**Plan `{plan['id']}`** — decided **{rec['decided']}** at {rec['at']} "
                         f"(proposed_by {plan.get('proposed_by')}, confidence {plan.get('confidence')})")
                p.append(f"> {plan.get('rationale','').strip()}")
                p.append("\nProposed hires:")
                for e in plan.get("employees", []):
                    p.append(f"- **{e['name']}** — `{e['profession']}` reports to `{e['reports_to']}`"
                             f"{f' · budget {e['budget_cents']}c' if e.get('budget_cents') else ''}")
                p.append("\nManagement grants:")
                for g in plan.get("grants", []):
                    p.append(f"- `{g['employee_ref']}` — can_lead={g['can_lead']} "
                             f"subdelegate={g['can_subdelegate']} depth={g['max_delegation_depth']} "
                             f"team≤{g['max_team_size']}")
        else:
            p.append("- No workforce plan reached the human boundary.")
        if approvals:
            p.append(f"\n`approval.decided` engine events: {len(approvals)}")

        p.append("\n---\n## 3 · Employee beats")
        if beats_by_emp:
            for emp, bs in sorted(beats_by_emp.items()):
                kinds = defaultdict(int)
                for b in bs:
                    kinds[b["type"]] += 1
                p.append(f"\n**{emp}** — {len(bs)} beat events: "
                         + ", ".join(f"`{k.split('.',1)[1]}`×{v}" for k, v in sorted(kinds.items())))
        else:
            p.append("- No beats recorded.")

        p.append("\n---\n## 4 · Mission teams formed")
        teams = L.get("teams", [])
        if teams:
            for t in teams:
                p.append(f"- **{t['name']}** (`{t['id']}`) — lead `{t['lead']}`, status {t['status']}, "
                         f"members: {', '.join(t['members']) or '(none yet)'}")
        else:
            p.append("- No durable teams formed.")
        p.append(f"\n`employee.hired` events: {len(hired)}"
                 + (": " + ", ".join(e.get("employee_id") or "?" for e in hired) if hired else ""))
        cap = L.get("capacity", [])
        if cap:
            p.append("\n**Capacity by profession:**")
            for e in cap:
                p.append(f"- `{e['role']}` — eligible {e['eligible']}, running {e['running']}, "
                         f"assigned {e['assigned']}, queued {e['queued']}")

        p.append("\n---\n## 5 · Per-employee harness (dream) logs")
        p.append(f"\nLLM calls (dream harness spend): **{len(llm)}**")
        spend = L.get("costs_employee", [])
        if isinstance(spend, list) and spend:
            for row in spend:
                p.append(f"- `{row['key']}` — {row['cost_cents']}c over {row['events']} calls "
                         f"({row['input_tokens']}→{row['output_tokens']} tok)")
        tool = defaultdict(lambda: defaultdict(int))
        for e in ev:
            if e.get("type") in ("run.tool_use", "run.tool_result", "run.subagent_spawned"):
                tool[e.get("employee_id") or "(company)"][e["type"]] += 1
        if tool:
            p.append("\n**Tool activity per employee:**")
            for emp, d in sorted(tool.items()):
                p.append(f"- {emp}: " + ", ".join(f"{k.split('.',1)[1]}×{v}" for k, v in d.items()))
        p.append("\nDurable per-run transcripts saved alongside this report as `runlog-*.txt`.")

        p.append("\n---\n## 6 · Artifacts generated")
        arts = L.get("artifacts", [])
        if isinstance(arts, list) and arts:
            for a in arts:
                p.append(f"- `{a['type']}` from task `{a['task_id']}` "
                         f"{'(primary) ' if a.get('is_primary') else ''}"
                         f"{a.get('url') or ''} — review {a.get('review_state')}")
        else:
            p.append("- No artifacts landed.")

        p.append("\n---\n## 7 · Definitions of Done (leads & ICs)")
        p.append("\n**Formation contract (CEO DoD, server-prepended):** the workforce plan is 'done' when "
                 "`workforce_plan.json` holds one proposed plan where every hire names a catalog profession, "
                 "a reporting line, and 2–3 responsibilities; each lead holds a bounded management grant; "
                 "budgets are bounded; and `governance-ledger.md` records the proposal.")
        p.append(f"\n**Delegation brief (lead DoD, seeded):**\n> {DELIVERY_BRIEF}")
        # try to surface delegated child DoDs from tool payloads
        delegated = [e for e in ev if e.get("type") == "run.tool_use"
                     and "deleg" in json.dumps(e.get("payload", {})).lower()]
        p.append(f"\nDelegation tool-calls observed (lead → IC hand-offs): {len(delegated)}")

        p.append("\n---\n## Appendix · full event-type histogram")
        p += [f"- `{k}`: {v}" for k, v in by_type.items()] or ["- (none)"]
        return "\n".join(p)


async def main() -> None:
    m = Monitor()
    try:
        await m.drive()
    except Exception as e:  # noqa: BLE001 — always try to persist whatever we captured
        m.note("fatal", msg=repr(e))
        print(f"FATAL: {e!r}", file=sys.stderr, flush=True)
    finally:
        m.write()


if __name__ == "__main__":
    asyncio.run(main())
