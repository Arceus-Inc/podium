"""Generate a rich, self-contained HTML report for a Lumen company run.

Pulls the full picture from the ledger DB (org, goal tree, task tree, beats,
decisions, approvals/workforce plans, artifacts, spend), gathers the files the
company actually produced from its git worktrees, embeds any screenshots found
in ``screenshots/`` next to the output, and renders everything into one HTML
file styled after ``reports/frontend-engineer-artifacts/board/flow-report.html``.

Usage:
    uv run python scripts/build_lumen_report.py <company_id>
"""

from __future__ import annotations

import asyncio
import base64
import html
import json
import os
import sys
from datetime import datetime
from pathlib import Path

import asyncpg

# Nothing hardcoded: the DSN comes from the same env var the operator uses (localhost dev default),
# and every path is derived relative to this script so the report builds on any machine/checkout.
_PODIUM_ROOT = Path(__file__).resolve().parent.parent
DSN = os.environ.get(
    "OPERATOR_LEDGER_DSN",
    os.environ.get("PODIUM_ENGINE_LEDGER_DSN", "postgresql://postgres@127.0.0.1:5432/podium"),
)
WORKDIR = Path(os.environ.get("PODIUM_WORKDIR", str(_PODIUM_ROOT / ".podium" / "workdir")))
OUT_DIR = Path(os.environ.get("LUMEN_REPORT_DIR", str(_PODIUM_ROOT / "reports" / "lumen-run")))
SHOTS_DIR = OUT_DIR / "screenshots"

# ---- file gathering ---------------------------------------------------------

SKIP_DIRS = {"node_modules", ".git", ".dream", "test-results", ".playwright"}
TEXT_EXT = {
    ".html", ".js", ".ts", ".tsx", ".cts", ".mts", ".jsx", ".css", ".md",
    ".json", ".txt", ".yml", ".yaml", ".mjs", ".cjs",
}
# files that are large / generated and not worth inlining in full
SKIP_FILE = {"package-lock.json", "registry.json"}
MAX_LINES = 320


def _iter_repo_files(root: Path):
    for p in sorted(root.rglob("*")):
        if not p.is_file():
            continue
        parts = set(p.relative_to(root).parts)
        if parts & SKIP_DIRS:
            continue
        if p.name in SKIP_FILE:
            continue
        if p.suffix.lower() not in TEXT_EXT:
            continue
        rel = p.relative_to(root).as_posix()
        # skip harness bookkeeping
        if rel.startswith("docs/exec-plans") or rel.startswith("docs/evals"):
            continue
        yield rel, p


def _wt_display(dirname: str) -> str:
    """A worktree dir name (an employee id like ``hire_diego`` / ``e_lead_priya`` / ``casey``) as a
    human display name — strip the id prefix, split, title-case (``hire_diego`` -> ``Diego``)."""
    name = dirname
    for pref in ("hire_", "e_lead_", "lead_", "e_"):
        if name.startswith(pref):
            name = name[len(pref):]
            break
    return " ".join(w.capitalize() for w in name.replace("-", "_").split("_") if w) or dirname


def contributors_by_path(base: Path) -> dict[str, list[str]]:
    """Map each deliverable path -> the employees who created/edited it.

    Attribution is by PRESENCE in an employee's own git worktree: every employee works in its own
    ``worktrees/<id>`` checkout, so a file that exists there is one that employee authored or touched
    on their branch (before it was merged to main). A file can have several contributors (e.g. a
    shared ``findings.md`` present in two worktrees)."""
    wt = base / "worktrees"
    out: dict[str, set[str]] = {}
    if not wt.exists():
        return {}
    for d in sorted(wt.iterdir()):
        if not d.is_dir():
            continue
        who = _wt_display(d.name)
        for rel, _p in _iter_repo_files(d):
            out.setdefault(rel, set()).add(who)
    return {rel: sorted(names) for rel, names in out.items()}


def gather_deliverables(company_id: str) -> list[dict]:
    """Collect the INTEGRATED product from the merged ``main`` branch (``repo/``) — ONCE.

    Every employee works on its own ``chorus/<name>`` branch in a worktree and the engine merges each
    into ``repo`` (main). The worktrees are near-identical copies of main, so inlining all of them (as
    an earlier version did) repeated every file 5-6x. The canonical deliverable is main; per-employee
    contribution is summarised separately by :func:`gather_git` (branch + commits + diffstat), not by
    re-inlining content. Falls back to worktrees only if no merged ``repo`` exists yet. Each file is
    tagged with the employees who created/edited it (:func:`contributors_by_path`).
    """
    base = WORKDIR / company_id / "work" / company_id
    groups: list[dict] = []
    seen: set[str] = set()
    contributors = contributors_by_path(base)

    def collect(root: Path, label: str, kind: str):
        if not root.exists():
            return
        files = []
        for rel, p in _iter_repo_files(root):
            key = rel
            if key in seen:
                continue
            seen.add(key)
            try:
                text = p.read_text(encoding="utf-8", errors="replace")
            except Exception:
                continue
            lines = text.splitlines()
            truncated = len(lines) > MAX_LINES
            body = "\n".join(lines[:MAX_LINES])
            files.append({
                "path": rel, "lines": len(lines), "bytes": p.stat().st_size,
                "content": body, "truncated": truncated,
                "contributors": contributors.get(rel, []),
            })
        if files:
            groups.append({"label": label, "kind": kind, "files": files})

    repo = base / "repo"
    if repo.exists():
        collect(repo, "main (merged, integrated product)", "repo")
        return groups
    # no merged main yet — fall back to per-worktree so the report is never empty
    wt = base / "worktrees"
    if wt.exists():
        for d in sorted(wt.iterdir()):
            if d.is_dir() and d.name != "casey":
                collect(d, f"worktree/{d.name}", "worktree")
    return groups


def _git(root: Path, *args: str) -> str:
    """Run a git command in ``root``; return stdout (empty on any failure — the report is best-effort)."""
    import subprocess

    try:
        r = subprocess.run(
            ["git", "-C", str(root), *args], capture_output=True, text=True, timeout=30
        )
        return r.stdout.strip() if r.returncode == 0 else ""
    except Exception:
        return ""


def gather_git(company_id: str) -> dict:
    """The state of ``main`` and each employee's contribution to it.

    Returns ``{branches, log:[(sha,subject)], contributions:[{branch,commits,files,insertions,
    deletions}]}`` read straight from the merged repo — this is what makes 'the status of main'
    concrete: the merge history and how much each person's branch changed.
    """
    repo = WORKDIR / company_id / "work" / company_id / "repo"
    if not repo.exists():
        return {}
    branches = [
        b.strip().lstrip("* ").strip()
        for b in _git(repo, "branch", "-a").splitlines()
        if b.strip()
    ]
    log_raw = _git(repo, "log", "--pretty=format:%h\x1f%s", "-40")
    log = []
    for line in log_raw.splitlines():
        if "\x1f" in line:
            sha, subject = line.split("\x1f", 1)
            log.append((sha, subject))
    contributions = []
    for b in branches:
        if not b.startswith("chorus/"):
            continue
        # diffstat of this employee's branch vs the repo's first commit (its whole contribution)
        stat = _git(repo, "diff", "--shortstat", f"main...{b}")
        commits = _git(repo, "rev-list", "--count", f"main..{b}") or "0"
        merged_commits = _git(
            repo, "log", "--oneline", "--grep", f"merge {b}", "main"
        ).count("\n")
        contributions.append({
            "branch": b,
            "employee": b.split("/", 1)[1],
            "ahead": commits,
            "merges": merged_commits + (1 if _git(repo, "log", "--oneline", "--grep", f"merge {b}", "main") else 0),
            "shortstat": stat,
        })
    return {"branches": branches, "log": log, "contributions": contributions}




def embed_screenshots() -> list[dict]:
    shots = []
    if SHOTS_DIR.exists():
        for p in sorted(SHOTS_DIR.glob("*.png")):
            data = base64.b64encode(p.read_bytes()).decode()
            caption = p.stem.replace("-", " ").replace("_", " ")
            shots.append({"caption": caption, "data": data})
    return shots


# ---- tiny markdown renderer (headings/bold/lists/hr/code) --------------------

def md_to_html(md: str) -> str:
    out: list[str] = []
    in_ul = False
    for raw in md.splitlines():
        line = raw.rstrip()
        if line.strip() == "---":
            if in_ul:
                out.append("</ul>"); in_ul = False
            out.append("<hr>")
            continue
        h = 0
        while h < len(line) and line[h] == "#":
            h += 1
        if 0 < h <= 6 and h < len(line) and line[h] == " ":
            if in_ul:
                out.append("</ul>"); in_ul = False
            out.append(f"<h{h}>{_inline(line[h+1:])}</h{h}>")
            continue
        stripped = line.lstrip()
        if stripped.startswith(("- ", "* ")):
            if not in_ul:
                out.append("<ul>"); in_ul = True
            out.append(f"<li>{_inline(stripped[2:])}</li>")
            continue
        if in_ul:
            out.append("</ul>"); in_ul = False
        if not line.strip():
            out.append("")
        else:
            out.append(f"<p>{_inline(line)}</p>")
    if in_ul:
        out.append("</ul>")
    return "\n".join(out)


def _inline(s: str) -> str:
    s = html.escape(s)
    # bold **x**
    while "**" in s:
        s = s.replace("**", "<b>", 1)
        s = s.replace("**", "</b>", 1) if "**" in s else s
    # code `x`
    parts = s.split("`")
    if len(parts) > 1:
        s = "".join(
            (p if i % 2 == 0 else f"<code>{p}</code>") for i, p in enumerate(parts)
        )
    return s


# ---- direction decisions (horizon DecisionStore) ----------------------------

def _load_direction_decisions(company_id: str, emps: list[dict]) -> list[dict]:
    """The CEO's formal decisions live in horizon's DecisionStore (``decisions.json`` in the
    company workdir), not the task-level ``decision_record`` ledger table. Load them so the report
    reflects the decision the CEO actually recorded (statement + rationale + owner + status)."""
    path = WORKDIR / company_id / "decisions.json"
    if not path.exists():
        return []
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return []
    by_id = {e["id"]: e.get("name") for e in emps}
    out: list[dict] = []
    for dec in raw.values() if isinstance(raw, dict) else []:
        if not isinstance(dec, dict):
            continue
        owner = dec.get("owner")
        out.append({
            "id": dec.get("id"),
            "option": dec.get("statement") or "decision",
            "rationale": dec.get("rationale") or "",
            "status": dec.get("status") or "",
            "goal_ids": dec.get("goal_ids") or [],
            "by_name": by_id.get(owner, owner) if owner else "founder objective",
            "source": "direction",
        })
    return out


# ---- DB ---------------------------------------------------------------------

async def fetch(company_id: str) -> dict:
    conn = await asyncpg.connect(DSN)
    try:
        async def rows(q, *a):
            return [dict(r) for r in await conn.fetch(q, *a)]

        company = await conn.fetchrow(
            "select * from companies where id=$1", _u(company_id))
        emps = await rows(
            "select e.*, mp.can_lead, mp.max_team_size, mp.max_delegation_depth, "
            "mp.spend_limit_cents "
            "from employee e left join management_profile mp "
            "on mp.employee_id=e.id and mp.company_id=e.company_id and mp.active "
            "where e.company_id=$1 order by e.created_at", _u(company_id))
        goals = await rows(
            "select * from goal where company_id=$1 order by created_at", _u(company_id))
        tasks = await rows(
            "select * from task where company_id=$1 order by depth, created_at",
            _u(company_id))
        beats = await rows(
            "select r.*, e.name as emp_name, e.role as emp_role, t.intent as task_intent "
            "from run r left join employee e on e.id=r.employee_id "
            "left join task t on t.id=r.task_id "
            "where r.company_id=$1 order by r.created_at", _u(company_id))
        arts = await rows(
            "select a.*, t.intent as task_intent, e.name as by_name "
            "from artifact a left join task t on t.id=a.task_id "
            "left join employee e on e.id=t.assignee_employee_id "
            "where a.company_id=$1 order by a.created_at", _u(company_id))
        decisions = await rows(
            "select d.*, t.intent as task_intent, e.name as by_name "
            "from decision_record d left join task t on t.id=d.task_id "
            "left join employee e on e.id=t.assignee_employee_id "
            "where d.company_id=$1 order by d.created_at", _u(company_id))
        plans = await rows(
            "select * from workforce_plan where company_id=$1 order by created_at, revision",
            _u(company_id))
        plan_emps = await rows(
            "select * from workforce_plan_employee where company_id=$1 order by position",
            _u(company_id))
        plan_grants = await rows(
            "select * from workforce_plan_management_grant where company_id=$1 order by position",
            _u(company_id))
        approvals = await rows(
            "select * from approval where company_id=$1 order by created_at", _u(company_id))
        teams = await rows(
            "select tm.*, e.name as lead_name, g.title as goal_title "
            "from team tm left join employee e on e.id=tm.lead_employee_id "
            "left join goal g on g.id=tm.goal_id where tm.company_id=$1 order by tm.created_at",
            _u(company_id))
        staffing = await rows(
            "select * from staffing_request where company_id=$1 order by created_at",
            _u(company_id))
        cost_by_emp = await rows(
            "select employee_id, sum(cost_cents) c, sum(input_tokens) it, "
            "sum(output_tokens) ot, count(*) n from cost_event where company_id=$1 "
            "group by employee_id", _u(company_id))
        total_cost = await conn.fetchval(
            "select coalesce(sum(cost_cents),0) from cost_event where company_id=$1",
            _u(company_id))
        # The CEO's formal decisions are recorded in horizon's DecisionStore (decisions.json),
        # not the task-level decision_record ledger table — merge them so "CEO records decisions"
        # reflects the decision the CEO actually made, with its rationale.
        direction_decisions = _load_direction_decisions(company_id, emps)
        return {
            "company": dict(company) if company else {},
            "employees": emps, "goals": goals, "tasks": tasks, "beats": beats,
            "artifacts": arts, "decisions": direction_decisions + list(decisions),
            "task_decisions": decisions, "plans": plans,
            "plan_emps": plan_emps, "plan_grants": plan_grants,
            "approvals": approvals, "teams": teams, "staffing": staffing,
            "cost_by_emp": {str(r["employee_id"]): r for r in cost_by_emp},
            "total_cost": int(total_cost or 0),
        }
    finally:
        await conn.close()


def _u(s: str):
    import uuid
    return uuid.UUID(s)


# ---- helpers ----------------------------------------------------------------

def esc(s) -> str:
    return html.escape("" if s is None else str(s))


def jget(v, *keys, default=None):
    if isinstance(v, str):
        try:
            v = json.loads(v)
        except Exception:  # noqa: BLE001
            return default
    if isinstance(v, dict):
        for k in keys:
            if k in v:
                return v[k]
    return default


def fmt_dt(v) -> str:
    if isinstance(v, datetime):
        return v.strftime("%H:%M:%S")
    return str(v or "")


# ---- HTML rendering ---------------------------------------------------------

CSS = """
:root{--bg:#f6f8fa;--card:#fff;--ink:#1f2328;--muted:#57606a;--line:#d0d7de;--accent:#0d9488;--accent-soft:#ecfeff}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif}
.wrap{max-width:1040px;margin:0 auto;padding:32px 20px 80px}
header.top{background:linear-gradient(135deg,#ecfeff,#f6f8fa);border:1px solid var(--line);border-radius:16px;padding:28px 28px 22px;margin-bottom:22px}
header.top .eyebrow{color:var(--accent);font-weight:700;letter-spacing:.06em;text-transform:uppercase;font-size:12px}
header.top h1{margin:.2em 0 .3em;font-size:26px}
header.top .intent{color:var(--muted);margin:0}
header.top .intent b{color:var(--ink)}
.badge{font-size:12px;font-weight:700;padding:3px 10px;border-radius:999px}
.badge.ok{background:#dcfce7;color:#166534}
.badge.warn{background:#fef9c3;color:#854d0e}
.badge.err{background:#fee2e2;color:#b91c1c}
.badge.bad{background:#fee2e2;color:#b91c1c}
.badge.info{background:var(--accent-soft);color:#0e7490}
.reverify{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:6px 20px 18px;margin:22px 0}
.reverify h2{font-size:13px;text-transform:uppercase;letter-spacing:.05em;color:var(--muted);margin:16px 0 6px}
.vrow{display:flex;align-items:center;gap:12px;padding:8px 0;border-bottom:1px dashed var(--line)}
.vrow:last-of-type{border-bottom:none}
.vlabel{min-width:230px;font-weight:600;font-size:14px}
.vrow .muted{color:var(--muted);font-size:12.5px}
.dot{width:9px;height:9px;border-radius:50%;display:inline-block;flex:0 0 auto}
.overall{display:flex;align-items:center;gap:14px;margin-top:14px;padding:14px 16px;border-radius:12px}
.overall.ok{background:#f0fdf4;border:1px solid #bbf7d0}
.overall.warn{background:#fffbeb;border:1px solid #fde68a}
.overall .big{font-size:18px;font-weight:800;letter-spacing:.02em}
.overall.ok .big{color:#166534}
.overall.warn .big{color:#854d0e}
.overall p{margin:0;color:var(--muted);font-size:13px}
.chips{display:grid;grid-template-columns:repeat(auto-fit,minmax(120px,1fr));gap:12px;margin:22px 0}
.chip{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:14px 16px;color:var(--muted);font-size:12.5px}
.chip span{display:block;font-size:24px;font-weight:750;color:var(--ink);line-height:1.1}
h2.sec{font-size:15px;text-transform:uppercase;letter-spacing:.05em;color:var(--muted);margin:38px 0 12px;border-bottom:1px solid var(--line);padding-bottom:6px}
.card{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:16px 18px;margin-bottom:14px}
.pod{border-left:4px solid var(--accent)}
.pod h3{margin:0 0 2px;font-size:16px}
.pod .who{color:var(--muted);font-size:13px}
table{width:100%;border-collapse:collapse;font-size:13px}
th,td{text-align:left;padding:7px 9px;border-bottom:1px solid var(--line);vertical-align:top}
th{color:var(--muted);font-weight:600;font-size:11.5px;text-transform:uppercase;letter-spacing:.04em}
tr:last-child td{border-bottom:none}
.mono{font:12.5px/1.5 ui-monospace,SFMono-Regular,Menlo,Consolas,monospace}
.tree{list-style:none;padding-left:0;margin:0}
.tree ul{list-style:none;margin:4px 0 4px 22px;padding-left:14px;border-left:1px dashed var(--line)}
.tree li{margin:4px 0}
.node{display:flex;gap:9px;align-items:baseline;flex-wrap:wrap}
.node .t{font-weight:600}
.pill{font-size:11px;font-weight:700;padding:1px 8px;border-radius:999px;background:#eef2ff;color:#3730a3}
.pill.done{background:#dcfce7;color:#166534}
.pill.active,.pill.in_progress{background:#dbeafe;color:#1e40af}
.pill.blocked{background:#fef3c7;color:#92400e}
.pill.rejected,.pill.failed,.pill.cancelled{background:#fee2e2;color:#b91c1c}
.pill.todo,.pill.proposed{background:#f3f4f6;color:#374151}
.pill.role{background:#f3e8ff;color:#6b21a8}
.shot{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:12px;margin-bottom:16px}
.shot img{width:100%;border-radius:8px;border:1px solid var(--line);display:block}
.shot .cap{color:var(--muted);font-size:13px;margin-top:8px;text-transform:capitalize}
details.art{background:var(--card);border:1px solid var(--line);border-radius:12px;margin-bottom:8px}
details.art>summary{cursor:pointer;padding:12px 16px;font-weight:600;display:flex;gap:10px;align-items:center}
details.art>summary .meta{margin-left:auto;color:var(--muted);font-size:12px;font-weight:400}
details.art pre{margin:0;padding:0 16px 16px;white-space:pre-wrap;word-wrap:break-word;font:12px/1.5 ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;color:#24292f;max-height:560px;overflow:auto}
.docprev{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:6px 22px 18px}
.docprev h1{font-size:20px}.docprev h2{font-size:16px;border:none;text-transform:none;letter-spacing:0;color:var(--ink);margin:16px 0 6px}
.docprev h3{font-size:14px}.docprev code{background:#f3f4f6;padding:1px 5px;border-radius:5px;font-size:12.5px}
.grpname{font-weight:700;color:var(--accent);margin:14px 0 6px;font-size:13px;text-transform:uppercase;letter-spacing:.04em}
.outcome{font-size:11.5px;color:var(--muted)}
footer{color:var(--muted);font-size:12px;text-align:center;margin-top:44px}
.legend{color:var(--muted);font-size:12.5px;margin-bottom:10px}
"""


def compute_health(data: dict) -> list[dict]:
    """Auto-audit: flag the anti-patterns from AUDIT.md straight from the ledger, so every report
    self-checks whether the company is behaving like a real one. Each finding is
    ``{level: ok|warn|bad, label, detail}``."""
    emps = [e for e in data["employees"] if str(e.get("status")) != "terminated"]
    by_id = {str(e["id"]): e for e in emps}
    ceo = next((e for e in emps if e["role"] == "ceo"), None)
    tasks = data["tasks"]
    beats = data["beats"]
    goals = data["goals"]
    findings: list[dict] = []

    # A1 duplicate identities
    from collections import Counter
    name_counts = Counter((e["name"] or "").strip().casefold() for e in emps)
    dups = [n for n, c in name_counts.items() if n and c > 1]
    findings.append({
        "level": "bad" if dups else "ok",
        "label": "Duplicate employees",
        "detail": ("none — every person is unique" if not dups
                   else f"{len(dups)} duplicated identity(ies): " + ", ".join(sorted(dups))),
    })

    # A2 placeholder (role-as-name) names
    role_words = ("engineer", "analyst", "designer", "marketer", "lead", "manager", "pod", "qa", "pm")
    placeholders = [e["name"] for e in emps
                    if e["role"] != "ceo" and any(w in (e["name"] or "").lower() for w in role_words)]
    findings.append({
        "level": "warn" if placeholders else "ok",
        "label": "Real person names",
        "detail": ("every hire has a real given name" if not placeholders
                   else f"{len(placeholders)} role-as-name placeholder(s): " + ", ".join(placeholders[:6])),
    })

    # A3 idle employees (no task assigned AND no beat)
    assigned = {str(t["assignee_employee_id"]) for t in tasks if t.get("assignee_employee_id")}
    beated = {str(b["employee_id"]) for b in beats if b.get("employee_id")}
    idle = [e for e in emps if e["role"] != "ceo"
            and str(e["id"]) not in assigned and str(e["id"]) not in beated]
    findings.append({
        "level": "bad" if len(idle) >= 3 else ("warn" if idle else "ok"),
        "label": "Idle / stale employees",
        "detail": (f"none — all {len(emps)-1} non-CEO staff have work"
                   if not idle else f"{len(idle)} with 0 tasks and 0 beats: "
                   + ", ".join((e["name"] or "?") for e in idle[:6])),
    })

    # A5 IC directly under the CEO
    ic_under_ceo = [e for e in emps if ceo is not None and str(e.get("reports_to")) == str(ceo["id"])
                    and not e.get("can_lead")]
    findings.append({
        "level": "bad" if ic_under_ceo else "ok",
        "label": "Only leads report to the CEO",
        "detail": ("yes — CEO's only reports are pod leads" if not ic_under_ceo
                   else f"{len(ic_under_ceo)} IC(s) hang directly off the CEO: "
                   + ", ".join((e["name"] or "?") for e in ic_under_ceo[:6])),
    })

    # B1 goals closing
    done_goals = [g for g in goals if str(g["status"]) == "done"]
    findings.append({
        "level": "ok" if done_goals else ("warn" if goals else "ok"),
        "label": "Goals completing (roll-up)",
        "detail": f"{len(done_goals)} of {len(goals)} goals reached done"
                  + (" — roadmap is advancing" if done_goals else " — none closed yet"),
    })

    # B2 redone work — duplicate delivery subtask intents
    intents = Counter((t["intent"] or "").strip().lower()[:60] for t in tasks
                      if t.get("depth", 0) >= 1 and t.get("execution_mode") == "delivery")
    redone = [i for i, c in intents.items() if c > 1]
    findings.append({
        "level": "warn" if redone else "ok",
        "label": "No redone work",
        "detail": ("each subtask is done once" if not redone
                   else f"{len(redone)} subtask(s) re-decomposed/redone"),
    })

    # B3 stranded delegations (blocked with ALL children terminal yet the goal never closed — the
    # lead gave up, not merely parked mid-flight waiting on children).
    def _children_of(tid):
        return [t for t in tasks if str(t.get("parent_id")) == str(tid)]
    goal_status = {str(g["id"]): str(g["status"]) for g in goals}
    stranded = []
    for t in tasks:
        if (t.get("execution_mode") == "delegation" and str(t["status"]) == "blocked"
                and t.get("parent_id") is None):
            kids = _children_of(t["id"])
            all_terminal = bool(kids) and all(
                str(k["status"]) in ("done", "rejected", "cancelled") for k in kids)
            if all_terminal and goal_status.get(str(t.get("goal_id"))) != "done":
                stranded.append(t)
    findings.append({
        "level": "bad" if stranded else "ok",
        "label": "Delegations converge (not stranded)",
        "detail": ("no delegation is stranded — blocked ones are mid-flight waiting on children"
                   if not stranded else f"{len(stranded)} delegation(s) gave up with work unfinished"),
    })

    # C2 CEO decision record
    _decs = data.get("decisions") or []
    _ceo_decs = [d for d in _decs if d.get("source") == "direction" and d.get("by_name") != "founder objective"]
    _with_rationale = [d for d in _ceo_decs if (d.get("rationale") or "").strip()]
    if _ceo_decs:
        _detail = (f"{len(_ceo_decs)} CEO decision(s) recorded, "
                   f"{len(_with_rationale)} with a rationale"
                   + (f" (e.g. \u201c{(_with_rationale[0]['rationale'] or '')[:90]}\u2026\u201d)"
                      if _with_rationale else " \u2014 rationale still empty"))
        _level = "ok" if _with_rationale else "warn"
    elif _decs:
        _detail = f"{len(_decs)} decision record(s)"
        _level = "ok"
    else:
        _detail = "no formal decision recorded (formation beats failing?)"
        _level = "warn"
    findings.append({
        "level": _level,
        "label": "CEO records decisions",
        "detail": _detail,
    })

    return findings


def render(data: dict, deliverables: list[dict], shots: list[dict], git: dict | None = None) -> str:
    git = git or {}
    emps = data["employees"]
    by_id = {str(e["id"]): e for e in emps}
    ceo = next((e for e in emps if e["role"] == "ceo"), None)
    leads = [e for e in emps if e.get("can_lead") and e["role"] != "ceo"]
    goals = data["goals"]
    tasks = data["tasks"]
    beats = data["beats"]
    arts = data["artifacts"]

    n_ok = sum(1 for b in beats if b["status"] == "succeeded")
    n_fail = sum(1 for b in beats if b["status"] == "failed")
    n_run = sum(1 for b in beats if b["status"] == "running")
    spend = data["total_cost"] / 100.0

    P = []
    A = P.append
    A(f"<!doctype html><html lang=en><head><meta charset=utf-8>")
    A('<meta name="viewport" content="width=device-width, initial-scale=1">')
    A("<title>Lumen — Company Run Report</title>")
    A(f"<style>{CSS}</style></head><body><div class=wrap>")

    # header
    mission = jget(data["company"].get("brief"), "text") or data["company"].get("brief") or \
        "Found and operate 'Lumen', a calm-productivity startup — a suite of small, privacy-first web apps on a shared calm design system."
    A('<header class="top"><div class="eyebrow">Chorus · Company Run · Lumen</div>')
    A("<h1>Company Run Report</h1>")
    A(f'<p class="intent"><b>Mission:</b> {esc(mission)}</p>')
    A(f'<p class="intent" style="margin-top:8px"><b>Company:</b> <span class="mono">{esc(data["company"].get("id"))}</span></p></header>')

    # chips
    A('<div class="chips">')
    for label, val in [
        ("Employees", len(emps)), ("Pod leads", len(leads)), ("Goals", len(goals)),
        ("Tasks", len(tasks)), ("Beats", len(beats)), ("Succeeded", n_ok),
        ("Failed", n_fail), ("Artifacts", len(arts)), ("Spend", f"${spend:,.2f}"),
    ]:
        A(f'<div class="chip"><span>{val}</span>{label}</div>')
    A("</div>")

    # ---- health panel (auto-audit against AUDIT.md) ----
    health = compute_health(data)
    n_bad = sum(1 for h in health if h["level"] == "bad")
    n_warn = sum(1 for h in health if h["level"] == "warn")
    overall = "bad" if n_bad else ("warn" if n_warn else "ok")
    badge = {"ok": "badge ok", "warn": "badge warn", "bad": "badge bad"}
    verdict = {
        "ok": "BEHAVING LIKE A REAL COMPANY",
        "warn": "MOSTLY HEALTHY — a few rough edges",
        "bad": "STRUCTURAL ISSUES REMAIN",
    }[overall]
    A('<div class="reverify"><h2>Company health — automatic audit</h2>')
    for h in health:
        dot = {"ok": "#1a7f37", "warn": "#9a6700", "bad": "#cf222e"}[h["level"]]
        A(f'<div class="vrow"><span class="dot" style="background:{dot}"></span>'
          f'<span class="vlabel">{esc(h["label"])}</span>'
          f'<span class="muted">{esc(h["detail"])}</span></div>')
    ov_cls = "overall ok" if overall == "ok" else "overall warn"
    A(f'<div class="{ov_cls}"><span class="big">{verdict}</span>'
      f'<p>{n_bad} blocking · {n_warn} warnings across {len(health)} checks — '
      f'the report re-audits AUDIT.md every time it is generated.</p></div></div>')

    # ---- org ----
    A('<h2 class="sec">The company — cross-functional pods</h2>')
    A('<div class="legend">CEO &rarr; pod leads &rarr; ICs. Each row shows the person, their profession, and their beat record.</div>')
    # group by manager
    reports: dict[str, list] = {}
    for e in emps:
        reports.setdefault(str(e["reports_to"]) if e["reports_to"] else "root", []).append(e)

    def beat_line(eid: str) -> str:
        bs = [b for b in beats if str(b["employee_id"]) == eid]
        if not bs:
            return '<span class="muted">no beats</span>'
        ok = sum(1 for b in bs if b["status"] == "succeeded")
        fa = sum(1 for b in bs if b["status"] == "failed")
        ru = sum(1 for b in bs if b["status"] == "running")
        c = data["cost_by_emp"].get(eid)
        cost = f" · ${c['c']/100:.2f}" if c else ""
        parts = []
        if ok:
            parts.append(f'<span class="pill done">{ok} ok</span>')
        if fa:
            parts.append(f'<span class="pill failed">{fa} fail</span>')
        if ru:
            parts.append(f'<span class="pill in_progress">{ru} run</span>')
        return " ".join(parts) + cost

    def emp_card(e, is_lead):
        eid = str(e["id"])
        team = reports.get(eid, [])
        cls = "card pod" if is_lead else "card"
        A(f'<div class="{cls}">')
        role = esc(e["role"])
        lead_badge = ' <span class="pill role">pod lead</span>' if is_lead else ''
        A(f'<div class="node"><span class="t">{esc(e["name"])}</span> '
          f'<span class="pill role">{role}</span>{lead_badge} '
          f'<span style="margin-left:auto">{beat_line(eid)}</span></div>')
        if is_lead and e.get("max_team_size"):
            A(f'<div class="who">team size {esc(e["max_team_size"])} · depth {esc(e.get("max_delegation_depth"))}</div>')
        if team:
            A('<table style="margin-top:10px"><tr><th>IC</th><th>Profession</th><th>Beats</th></tr>')
            for m in team:
                mid = str(m["id"])
                A(f'<tr><td>{esc(m["name"])}</td><td><span class="pill role">{esc(m["role"])}</span></td>'
                  f'<td>{beat_line(mid)}</td></tr>')
            A("</table>")
        A("</div>")

    if ceo:
        emp_card(ceo, False)
        # leads under ceo
        for e in reports.get(str(ceo["id"]), []):
            if e["role"] == "ceo":
                continue
            emp_card(e, bool(e.get("can_lead")))

    # ---- goal tree ----
    A('<h2 class="sec">Goal tree</h2>')
    goal_children: dict[str, list] = {}
    roots = []
    gids = {str(g["id"]) for g in goals}
    for g in goals:
        pid = str(g["parent_id"]) if g["parent_id"] else None
        if pid and pid in gids:
            goal_children.setdefault(pid, []).append(g)
        else:
            roots.append(g)
    # map goal -> team/lead
    team_by_goal = {str(t["goal_id"]): t for t in data["teams"] if t["goal_id"]}
    A('<ul class="tree">')

    def render_goal(g):
        gid = str(g["id"])
        tm = team_by_goal.get(gid)
        lead = f' &rarr; <span class="pill role">{esc(tm["lead_name"])}</span>' if tm and tm.get("lead_name") else ""
        st = esc(g["status"])
        A(f'<li><div class="node"><span class="t">{esc(g["title"])}</span>'
          f'<span class="pill {st}">{st}</span>{lead}</div>')
        kids = goal_children.get(gid, [])
        if kids:
            A('<ul>')
            for k in kids:
                render_goal(k)
            A('</ul>')
        A('</li>')

    for g in roots:
        render_goal(g)
    A('</ul>')

    # ---- task tree (decomposition) ----
    A('<h2 class="sec">Task decomposition</h2>')
    A('<div class="legend">Delegation tasks are what a pod lead owns; delivery tasks are IC work items the lead decomposed them into.</div>')
    tchildren: dict[str, list] = {}
    troots = []
    tids = {str(t["id"]) for t in tasks}
    for t in tasks:
        pid = str(t["parent_id"]) if t["parent_id"] else None
        if pid and pid in tids:
            tchildren.setdefault(pid, []).append(t)
        else:
            troots.append(t)
    A('<ul class="tree">')

    def render_task(t):
        tid = str(t["id"])
        who = by_id.get(str(t["assignee_employee_id"]))
        who_s = f' <span class="pill role">{esc(who["name"])}</span>' if who else ""
        st = esc(t["status"])
        mode = esc(t.get("execution_mode") or "")
        intent = esc((t["intent"] or "").split("\n")[0][:110])
        A(f'<li><div class="node"><span class="t">{intent}</span>'
          f'<span class="pill {st}">{st}</span>'
          f'<span class="outcome">{mode}</span>{who_s}</div>')
        kids = tchildren.get(tid, [])
        if kids:
            A('<ul>')
            for k in kids:
                render_task(k)
            A('</ul>')
        A('</li>')

    for t in troots:
        render_task(t)
    A('</ul>')

    # ---- beats ----
    A('<h2 class="sec">Beats — every heartbeat that ran</h2>')
    A('<div class="card"><table><tr><th>#</th><th>When</th><th>Employee</th><th>Task</th><th>Status</th><th>Outcome</th></tr>')
    for i, b in enumerate(beats, 1):
        st = esc(b["status"])
        oc = b.get("outcome")
        if isinstance(oc, str):
            try:
                oc = json.loads(oc)
            except Exception:  # noqa: BLE001
                oc = {}
        oc = oc or {}
        if "error" in oc:
            octxt = f'error: {esc(str(oc.get("error"))[:70])}'
        elif "sprint_outcomes" in oc:
            octxt = f'{esc(oc.get("sprint_outcomes"))} · done {oc.get("steps_done","?")}/{oc.get("steps_total","?")} · {oc.get("cost_cents",0)}¢'
        else:
            octxt = esc(json.dumps(oc)[:70]) if oc else ""
        intent = esc((b.get("task_intent") or "").split("\n")[0][:44])
        A(f'<tr><td>{i}</td><td class="mono">{fmt_dt(b.get("created_at"))}</td>'
          f'<td>{esc(b.get("emp_name"))}</td><td>{intent}</td>'
          f'<td><span class="pill {st}">{st}</span></td><td class="outcome">{octxt}</td></tr>')
    A("</table></div>")

    # ---- decisions ----
    A('<h2 class="sec">Decisions &amp; executive directives</h2>')
    if data["decisions"]:
        for d in data["decisions"]:
            A('<div class="card">')
            if d.get("source") == "direction":
                st = esc(d.get("status") or "")
                A(f'<div class="node"><span class="t">{esc(d.get("option") or "decision")}</span>'
                  + (f'<span class="pill info">{st}</span>' if st else "")
                  + f'<span class="outcome" style="margin-left:auto">{esc(d.get("by_name"))}</span></div>')
                if d.get("rationale"):
                    A(f'<p class="who" style="margin:8px 0 0">{esc(d["rationale"])[:800]}</p>')
                if d.get("goal_ids"):
                    A(f'<p class="outcome" style="margin:6px 0 0">drives {len(d["goal_ids"])} goal(s)</p>')
            else:
                A(f'<div class="node"><span class="t">{esc(d.get("option") or "decision")}</span>'
                  f'<span class="pill info">conf {esc(d.get("confidence"))}</span>'
                  f'<span class="outcome" style="margin-left:auto">{esc(d.get("by_name"))}</span></div>')
                if d.get("rationale"):
                    A(f'<p class="who" style="margin:8px 0 0">{esc(d["rationale"])[:800]}</p>')
                if d.get("outcome_metric"):
                    A(f'<p class="outcome" style="margin:6px 0 0">metric: {esc(d["outcome_metric"])}</p>')
                if d.get("rejected_alternatives"):
                    A(f'<p class="outcome">rejected: {esc(d["rejected_alternatives"])[:300]}</p>')
            A('</div>')
    else:
        A('<div class="card"><span class="muted">No structured decision records; the CEO'
          "'s executive review beat completed (see the beats table).</span></div>")

    # ---- approvals / workforce plans ----
    A('<h2 class="sec">Approvals — workforce plans</h2>')
    A('<div class="legend">Each plan revision is a hiring proposal the operator auto-approved to grow the org.</div>')
    A('<div class="card"><table><tr><th>Plan</th><th>Rev</th><th>Status</th><th>Hires</th><th>Lead grants</th><th>Rationale</th></tr>')
    grants_by_plan: dict[str, int] = {}
    for g in data["plan_grants"]:
        if g.get("can_lead"):
            grants_by_plan[str(g["plan_id"])] = grants_by_plan.get(str(g["plan_id"]), 0) + 1
    emps_by_plan: dict[str, int] = {}
    for pe in data["plan_emps"]:
        emps_by_plan[str(pe["plan_id"])] = emps_by_plan.get(str(pe["plan_id"]), 0) + 1
    for p in data["plans"]:
        pid = str(p["id"])
        st = esc(p["status"])
        A(f'<tr><td class="mono">{pid[:8]}</td><td>{esc(p.get("revision"))}</td>'
          f'<td><span class="pill {st}">{st}</span></td>'
          f'<td>{emps_by_plan.get(pid,0)}</td><td>{grants_by_plan.get(pid,0)}</td>'
          f'<td class="outcome">{esc((p.get("rationale") or "")[:120])}</td></tr>')
    A("</table></div>")
    if data["approvals"]:
        A('<div class="card"><table><tr><th>Subject</th><th>Gate</th><th>Action</th><th>Status</th><th>When</th></tr>')
        for ap in data["approvals"]:
            st = esc(ap["status"])
            A(f'<tr><td>{esc(ap.get("subject_kind"))}</td><td>{esc(ap.get("gate_kind"))}</td>'
              f'<td>{esc(ap.get("action"))}</td><td><span class="pill {st}">{st}</span></td>'
              f'<td class="mono">{fmt_dt(ap.get("created_at"))}</td></tr>')
        A("</table></div>")

    # ---- screenshots ----
    if shots:
        A('<h2 class="sec">Preview — the shipped apps running</h2>')
        for s in shots:
            A('<div class="shot">')
            A(f'<img alt="{esc(s["caption"])}" src="data:image/png;base64,{s["data"]}">')
            A(f'<div class="cap">{esc(s["caption"])}</div></div>')

    # ---- brand guide rendered preview ----
    brand = None
    for grp in deliverables:
        for f in grp["files"]:
            if f["path"].lower().endswith("brand_voice_guide.md") or "brand" in f["path"].lower() and f["path"].endswith(".md"):
                brand = f
                break
    if brand:
        A('<h2 class="sec">Preview — Lumen Brand &amp; Voice Guide (rendered)</h2>')
        A(f'<div class="docprev">{md_to_html(brand["content"])}</div>')

    # ---- main branch status ----
    if git.get("log") or git.get("contributions"):
        A('<h2 class="sec">Main branch — the integrated product</h2>')
        A('<div class="legend">Each employee works on its own <code>chorus/&lt;name&gt;</code> branch in an '
          'isolated git worktree, then the engine merges each branch into <code>main</code>. This is why '
          'files look "repeated" across worktrees — they are per-person copies of the same tree; the '
          'single source of truth is <code>main</code> below.</div>')
        contribs = git.get("contributions") or []
        if contribs:
            A('<div class="card"><table><tr><th>Employee branch</th><th>Merged to main</th>'
              '<th>Commits ahead</th><th>Change</th></tr>')
            for c in contribs:
                merged = "yes" if c.get("merges") else "—"
                stat = esc(c.get("shortstat") or "no net diff vs main")
                A(f'<tr><td class="mono">{esc(c["branch"])}</td>'
                  f'<td><span class="pill {"done" if c.get("merges") else "todo"}">{merged}</span></td>'
                  f'<td>{esc(c.get("ahead"))}</td><td class="outcome">{stat}</td></tr>')
            A("</table></div>")
        log = git.get("log") or []
        if log:
            A('<div class="card"><div class="grpname">Merge history on main (most recent first)</div>')
            A('<table>')
            for sha, subject in log[:30]:
                A(f'<tr><td class="mono">{esc(sha)}</td><td>{esc(subject)}</td></tr>')
            A("</table></div>")

    # ---- files ----
    A('<h2 class="sec">Files generated</h2>')
    A('<div class="legend">The integrated product on <code>main</code> after every employee branch was '
      'merged — each file shown ONCE (no per-worktree duplication). The chips name the employee(s) who '
      'created or edited each file (by presence in their worktree; a file can have several). Click to '
      'expand.</div>')
    for grp in deliverables:
        A(f'<div class="grpname">{esc(grp["label"])} — {len(grp["files"])} files</div>')
        for f in grp["files"]:
            trunc = ' · truncated' if f["truncated"] else ''
            who = f.get("contributors") or []
            chips = "".join(f'<span class="pill role">{esc(n)}</span>' for n in who)
            by = f'<span class="meta">{chips}</span>' if chips else ''
            A('<details class="art"><summary>')
            A(f'{esc(f["path"])}{by}<span class="meta">{f["lines"]} lines{trunc}</span></summary>')
            A(f'<pre>{esc(f["content"])}</pre></details>')

    A('<footer>Generated by scripts/build_lumen_report.py from the Lumen ledger + git worktrees · model gpt-5.2 (Azure)</footer>')
    A("</div></body></html>")
    return "\n".join(P)


async def main():
    company_id = sys.argv[1] if len(sys.argv) > 1 else "019f78e6-e1a1-7bc0-a48b-43b170529cfb"
    data = await fetch(company_id)
    deliverables = gather_deliverables(company_id)
    shots = embed_screenshots()
    git = gather_git(company_id)
    out = render(data, deliverables, shots, git)
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    dest = OUT_DIR / "company-report.html"
    dest.write_text(out, encoding="utf-8")
    print(f"wrote {dest} ({len(out):,} bytes)")
    print(f"  employees={len(data['employees'])} goals={len(data['goals'])} "
          f"tasks={len(data['tasks'])} beats={len(data['beats'])} "
          f"artifacts={len(data['artifacts'])} plans={len(data['plans'])} "
          f"deliverable-groups={len(deliverables)} screenshots={len(shots)}")


if __name__ == "__main__":
    asyncio.run(main())
