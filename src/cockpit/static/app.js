/* arceus cockpit — waku-shaped observability over the five-repo engine (design doc §4).
 * One snapshot API feeds every nav count; one SSE connection lights the architecture map and
 * the lanes; each section is a projection (OBS P4). Internal components render read-only. */
"use strict";

const state = {
  ctx: null,
  lanes: new Map(),
  snapshot: null,
  feeds: { memory: [], stalled: [], tail: [] },
  runLog: [],
};
const $ = (id) => document.getElementById(id);
const esc = (t) => String(t ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
const api = (path) =>
  fetch(`/v1/workspaces/${state.ctx.workspaceId}/companies/${state.ctx.companyId}${path}`, {
    headers: { Authorization: `Bearer ${state.ctx.token}` },
  }).then((r) => { if (!r.ok) throw new Error(`${path} -> ${r.status}`); return r.json(); });

/* ================= connect + bootstrap ================= */
$("connect-form").addEventListener("submit", (e) => {
  e.preventDefault();
  state.ctx = { workspaceId: $("workspace-id").value.trim(), companyId: $("company-id").value.trim(), token: $("token").value.trim() };
  localStorage.setItem("arceus.ctx", JSON.stringify({ workspaceId: state.ctx.workspaceId, companyId: state.ctx.companyId }));
  connect();
});
const restored = (function restore() {
  const saved = localStorage.getItem("arceus.ctx");
  if (!saved) return false;
  const { workspaceId, companyId } = JSON.parse(saved);
  $("workspace-id").value = workspaceId || "";
  $("company-id").value = companyId || "";
  // Dev-minted tokens live in sessionStorage only (this tab, until it closes);
  // tokens the operator types are never stored anywhere.
  const devToken = sessionStorage.getItem("arceus.dev.token");
  if (workspaceId && companyId && devToken) {
    $("token").value = devToken;
    $("connect-form").requestSubmit();
    return true;
  }
  return false;
})();
const playgroundButton = $("new-playground");
(async function probeBootstrap() {
  if (restored) { playgroundButton.hidden = false; return; } // don't mint a company per reload
  try {
    const probe = await fetch("/v1/dev/bootstrap", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ name: "playground" }) });
    if (probe.status === 404) return;
    playgroundButton.hidden = false;
    if (probe.ok) autoConnect(await probe.json());
  } catch { /* manual connect */ }
})();
playgroundButton.addEventListener("click", async () => {
  const r = await fetch("/v1/dev/bootstrap", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ name: "playground" }) });
  if (r.ok) autoConnect(await r.json());
});
function autoConnect(minted) {
  $("workspace-id").value = minted.workspace_id;
  $("company-id").value = minted.company_id;
  $("token").value = minted.token;
  sessionStorage.setItem("arceus.dev.token", minted.token);
  $("connect-form").requestSubmit();
}

async function connect() {
  setStatus("connecting");
  try {
    await refreshSnapshot();
    await seedLanes();
    openStream();
    render();
    setInterval(refreshSnapshot, 15000); // nav counts stay honest even without events
  } catch (err) { setStatus("error"); console.error(err); }
}
function setStatus(s) { $("conn").dataset.state = s; $("conn").textContent = s; }

/* ================= snapshot → nav counts ================= */
async function refreshSnapshot() {
  state.snapshot = await api("/cockpit/snapshot");
  const c = state.snapshot.components;
  const set = (id, v) => { const el = $(id); if (el) el.textContent = v || ""; };
  set("n-goals", c.horizon.goals);
  set("n-org", c.chorus.employees);
  set("n-work", c.chorus.open_tasks);
  set("n-teams", c.delegation.pending_plans ? `${c.delegation.pending_plans}!` : c.delegation.teams);
  set("n-episodic", c.episodic.records);
  set("n-semantic", c.semantic.atoms);
  set("n-skills", c.skills.heads);
  set("n-llmops", c.llmops.spend_cents ? `${(c.llmops.spend_cents / 100).toFixed(2)}` : "");
  set("n-ops", state.feeds.stalled.length || "");
  if ((location.hash.slice(1) || "overview") === "overview") render(); // refresh map counts; other views keep their form state
}

/* ================= lanes ================= */
async function seedLanes() {
  const roster = await api("/workforce");
  state.lanes.clear();
  for (const m of roster) state.lanes.set(m.id, { name: m.name, role: m.role, status: m.status, taskId: null, runId: null, lastEvents: [] });
}
const LANE_STATES = { "wake.claimed": "claimed", "task.status": "running", "run.started": "running", "run.tool_use": "tool", "run.text": "running", "run.done": "idle", "run.stalled": "stalled", "budget.hard_stop": "blocked" };
function laneFold(ev) {
  if (!ev.employee_id) return;
  let lane = state.lanes.get(ev.employee_id);
  if (!lane) { lane = { name: ev.employee_id, role: "", status: "idle", taskId: null, runId: null, lastEvents: [] }; state.lanes.set(ev.employee_id, lane); }
  const mapped = LANE_STATES[ev.type];
  if (mapped) lane.status = mapped;
  if (ev.task_id) lane.taskId = ev.task_id;
  if (ev.run_id) lane.runId = ev.run_id;
  lane.lastEvents = [{ type: ev.type }, ...lane.lastEvents].slice(0, 5);
}

/* ================= the spine (fetch-SSE, named events) ================= */
let lastEventId = null, streamGeneration = 0;
async function openStream() {
  const generation = ++streamGeneration;
  while (generation === streamGeneration) {
    try {
      const headers = { Authorization: `Bearer ${state.ctx.token}` };
      if (lastEventId !== null) headers["Last-Event-ID"] = String(lastEventId);
      const response = await fetch(`/v1/companies/${state.ctx.companyId}/stream`, { headers });
      if (!response.ok) throw new Error(`stream ${response.status}`);
      setStatus("live");
      const reader = response.body.getReader(), decoder = new TextDecoder();
      let buffer = "";
      for (;;) {
        const { value, done } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
        let b;
        while ((b = buffer.indexOf("\n\n")) !== -1) { handleFrame(buffer.slice(0, b)); buffer = buffer.slice(b + 2); }
      }
    } catch { /* retry */ }
    if (generation !== streamGeneration) return;
    setStatus("reconnecting");
    await new Promise((r) => setTimeout(r, 2000));
  }
}
function handleFrame(frame) {
  let data = "";
  for (const line of frame.split("\n")) {
    if (line.startsWith("id:")) lastEventId = line.slice(3).trim();
    else if (line.startsWith("data:")) data += line.slice(5).trim();
  }
  if (!data) return;
  let ev; try { ev = JSON.parse(data); } catch { return; }
  project(ev);
}

/* Every view folds from the same spine (OBS P4). */
let renderQueued = false;
function project(ev) {
  laneFold(ev);
  state.feeds.tail = [ev, ...state.feeds.tail].slice(0, 200);
  if (ev.type === "memory.retrieved") state.feeds.memory = [ev, ...state.feeds.memory].slice(0, 50);
  if (ev.type === "run.stalled") { state.feeds.stalled = [ev, ...state.feeds.stalled].slice(0, 50); $("n-ops").textContent = state.feeds.stalled.length; }
  const view = location.hash.slice(1) || "overview";
  if (view === "overview") { lightMap(ev); return; } // mutate the SVG in place — a rebuild would wipe the pulse
  if (["org", "ops", "episodic"].includes(view) && !renderQueued) {
    renderQueued = true;
    setTimeout(() => { renderQueued = false; render(); }, 400); // run.text arrives in bursts
  }
}

/* ================= the run dock ================= */
$("run-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  if (!state.ctx) return;
  const directive = $("directive").value.trim();
  if (!directive) return;
  const response = await fetch(`/v1/companies/${state.ctx.companyId}/runs`, {
    method: "POST",
    headers: { Authorization: `Bearer ${state.ctx.token}`, "Content-Type": "application/json" },
    body: JSON.stringify({
      directive,
      idempotency_key: crypto.randomUUID(),
      execution_mode: $("formation-mode").checked ? "formation" : "delivery",
    }),
  });
  const run = await response.json().catch(() => ({}));
  state.runLog = [{ id: run.id, directive, status: run.status ?? `error ${response.status}` }, ...state.runLog].slice(0, 12);
  if (response.ok) $("directive").value = "";
  renderRunLog();
});
function renderRunLog() {
  $("run-log").replaceChildren(...state.runLog.map((r) => {
    const li = document.createElement("li");
    li.innerHTML = `<b>${esc((r.id || "").slice(0, 8))}</b> ${esc(r.status)}<br>${esc(r.directive.slice(0, 60))}`;
    return li;
  }));
}

/* ================= router ================= */
const VIEWS = {
  overview: { title: "Overview", sub: "the whole engine, live — every node is clickable", render: renderOverview },
  direction: { title: "Direction", sub: "horizon — the goal tree chorus executes", render: renderDirection },
  org: { title: "Org", sub: "one live lane per employee — states never imply causality", render: renderOrg },
  work: { title: "Work", sub: "allocation is observable state, never inference", render: renderWork },
  delegation: { title: "Delegation", sub: "founder intent → teams → verified completion (read-only: the kernel is internal)", render: renderDelegation },
  runs: { title: "Runs", sub: "one run's full trace off the spine", render: renderRuns },
  episodic: { title: "Episodic memory", sub: "chorus beat records + retrieval at the moment of use", render: renderEpisodic },
  semantic: { title: "Semantic memory", sub: "lattice atoms — durable facts promoted from episodes", render: renderSemantic },
  skills: { title: "Skills", sub: "lattice's procedural pillar — versioned skill HEADs", render: renderSkills },
  llmops: { title: "LLMOps", sub: "spend, tokens, and model calls off the priced ledger", render: renderLLMOps },
  ops: { title: "Ops", sub: "stalled runs and the raw spine", render: renderOps },
};
window.addEventListener("hashchange", render);
async function render() {
  const key = location.hash.slice(1) || "overview";
  const view = VIEWS[key] || VIEWS.overview;
  document.querySelectorAll("#nav a").forEach((a) => a.classList.toggle("active", a.dataset.v === key));
  $("title").textContent = view.title;
  $("sub").textContent = view.sub;
  if (!state.ctx) { $("view").innerHTML = `<div class="card">Connect (or mint a playground) to begin.</div>`; return; }
  try { $("view").innerHTML = await view.render(); } catch (err) { $("view").innerHTML = `<div class="card">${esc(err)}</div>`; }
  wireView();
}
function card(title, body) { return `<div class="card">${title ? `<h2>${title}</h2>` : ""}${body}</div>`; }
function kv(pairs) { return `<div class="kv">${pairs.map(([v, l]) => `<div><b>${esc(v)}</b><span>${esc(l)}</span></div>`).join("")}</div>`; }

/* ================= views ================= */
async function renderOverview() {
  const c = state.snapshot.components;
  return card("", archSVG(c)) + card("System counters", kv([
    [c.horizon.goals, "goal roots"], [c.chorus.employees, "employees"], [c.chorus.open_tasks, "open tasks"],
    [c.chorus.running_beats, "running beats"], [c.delegation.teams, "teams"], [c.episodic.records, "episodes"],
    [c.semantic.atoms, "semantic atoms"], [c.skills.heads, "skills"], [(c.llmops.spend_cents / 100).toFixed(2), "spend $"],
  ]));
}

async function renderDirection() {
  const tree = await api("/goals");
  const node = (g, depth) => `<tr><td style="padding-left:${depth * 18}px">${esc(g.title)}</td><td><span class="pill">${esc(g.level)}</span></td><td>${esc(g.status)}</td><td>${esc(g.owner ?? "—")}</td></tr>` + g.children.map((ch) => node(ch, depth + 1)).join("");
  return card("Goal tree", tree.length ? `<table class="t"><tr><th>goal</th><th>level</th><th>status</th><th>owner</th></tr>${tree.map((g) => node(g, 0)).join("")}</table>` : "No direction set yet — seed a goal.") +
    card("Seed direction", `<form id="goal-form"><input id="goal-title" placeholder="goal title" required /><select id="goal-level"><option>company</option><option>team</option><option>employee</option></select><button>Create goal</button></form>`);
}

async function renderOrg() {
  // The roster is the source of truth; events only animate it. An employee materialized by a
  // plan approval must appear before their first beat.
  for (const member of await api("/workforce")) {
    const lane = state.lanes.get(member.id);
    if (!lane) state.lanes.set(member.id, { name: member.name, role: member.role, status: member.status, taskId: null, runId: null, lastEvents: [] });
    else { lane.name = member.name; lane.role = member.role; }
  }
  const lanes = [...state.lanes.entries()].map(([id, lane]) => `
    <article class="lane state-${lane.status}">
      <header><strong>${esc(lane.name)}</strong> <em>${esc(lane.role)}</em><span class="chip pill">${esc(lane.status)}</span></header>
      <div class="lane-task">${lane.taskId ? `task ${esc(lane.taskId.slice(0, 8))}…` : "—"}</div>
      <ol class="lane-events">${lane.lastEvents.map((e) => `<li>${esc(e.type)}</li>`).join("")}</ol>
    </article>`).join("");
  return `<div id="lanes">${lanes || card("", "No employees yet — hire below.")}</div>` +
    card("Hire", `<form id="hire-form"><input id="hire-name" placeholder="name" required /><input id="hire-role" placeholder="role (backend_engineer, pm…)" required /><input id="hire-boss" placeholder="reports_to (blank = root)" /><button>Hire</button></form>`);
}

async function renderWork() {
  const b = await api("/allocation");
  const list = (rows, f) => rows.length ? `<table class="t">${rows.map(f).join("")}</table>` : "—";
  return `<div class="cols">` +
    card(`Queued · ${b.queued.length}`, list(b.queued, (w) => `<tr><td>${esc(w.employee_id)}</td><td>${esc(w.reason)}</td><td>×${w.coalesced}</td></tr>`)) +
    card(`Running · ${b.running.length}`, list(b.running, (r) => `<tr><td>${esc(r.employee_id)}</td><td>${esc(r.run_id.slice(0, 8))}…</td><td>${esc(r.lease_expires_at ?? "—")}</td></tr>`)) +
    card(`Blocked · ${b.blocked.length}`, list(b.blocked, (t) => `<tr><td>${esc(t.task_id.slice(0, 8))}…</td><td>${esc(t.intent_excerpt)}</td></tr>`)) + `</div>`;
}

async function renderDelegation() {
  const [teams, plans] = await Promise.all([api("/teams"), api("/plans")]);
  const pending = plans.filter((p) => p.status === "proposed");
  const planCard = (p) => card(`Pending workforce plan · rev ${p.revision} · by ${esc(p.proposed_by)}`,
    `<p>${esc(p.rationale)} <span class="pill">confidence ${p.confidence}</span></p>` +
    `<table class="t"><tr><th>ref</th><th>name</th><th>profession</th><th>reports to</th><th>budget ¢</th></tr>` +
    p.employees.map((e) => `<tr><td>${esc(e.ref)}</td><td>${esc(e.name)}</td><td>${esc(e.profession)}</td><td>${esc(e.reports_to)}</td><td>${e.budget_cents ?? "—"}</td></tr>`).join("") + `</table>` +
    `<form class="decide-form" data-plan="${esc(p.id)}" style="margin-top:10px">` +
    `<button data-decision="approve">Approve — materialize the org</button>` +
    `<button data-decision="reject" style="background:var(--bad);color:#fff">Reject</button></form>`);
  const decided = plans.filter((p) => p.status !== "proposed");
  return card("The delegation flow (read-only — the kernel is internal)", delegationSVG(teams.length, pending.length)) +
    pending.map(planCard).join("") +
    (decided.length ? card("Decided plans", `<table class="t"><tr><th>plan</th><th>status</th><th>decided by</th></tr>${decided.map((p) => `<tr><td>${esc(p.id.slice(0, 8))}… rev ${p.revision}</td><td><span class="pill">${esc(p.status)}</span></td><td>${esc(p.decided_by ?? "—")}</td></tr>`).join("")}</table>`) : "") +
    card(`Mission teams · ${teams.length}`, teams.length ? `<table class="t"><tr><th>team</th><th>lead</th><th>status</th><th>members</th></tr>${teams.map((t) => `<tr><td>${esc(t.name)}</td><td>${esc(t.lead)}</td><td><span class="pill">${esc(t.status)}</span></td><td>${esc(t.members.join(", ") || "—")}</td></tr>`).join("")}</table>` : "No delegated teams yet — submit a delegation-mode run.");
}

async function renderRuns() {
  return card("Trace a run", `<form id="trace-form"><input id="trace-id" placeholder="run id" required /><button>Load</button></form><ol id="run-trace" class="feed"></ol>`);
}

async function renderEpisodic() {
  const c = state.snapshot.components;
  return card("Episodic store", kv([[c.episodic.records, "beat records (chorus FTS5, workdir-local)"]])) +
    card("Retrieval at the moment of use — memory.retrieved", state.feeds.memory.length
      ? `<ol class="feed">${state.feeds.memory.map((e) => `<li class="memory">${esc(e.employee_id)} · ${esc(e.payload.tool)} · ${e.payload.empty ? "MISS" : `${e.payload.hit_run_ids.length} hits`}</li>`).join("")}</ol>`
      : "No retrievals yet — they appear the moment a beat reads memory (misses included).");
}

async function renderSemantic() {
  const c = state.snapshot.components;
  const employees = Object.entries(c.semantic.by_employee);
  let detail = "";
  for (const [emp, count] of employees) {
    const facts = await api(`/cockpit/semantic/${emp}`);
    detail += card(`${emp} · ${count} atoms`, `<table class="t"><tr><th>key</th><th>value</th><th>activation</th><th>sources</th></tr>${facts.map((f) => `<tr><td>${esc(f.key)}</td><td>${esc(f.value)}</td><td>${f.activation.toFixed(2)}</td><td>${f.source_run_ids.length}</td></tr>`).join("")}</table>`);
  }
  return card("Semantic memory — lattice atoms", kv([[c.semantic.atoms, "durable facts promoted from episodic traces"]])) +
    (detail || card("", "No atoms yet — lattice promotes facts once the consolidation gate opens (≥5 fresh beats)."));
}

async function renderSkills() {
  const roster = await api("/workforce");
  let out = "";
  for (const member of roster) {
    const skills = await api(`/employees/${member.id}/skills`);
    if (skills.length) out += card(`${member.name} · ${skills.length}`, `<table class="t"><tr><th>skill</th><th>origin</th><th>state</th><th>rev</th></tr>${skills.map((s) => `<tr><td>${esc(s.name)}</td><td>${esc(s.origin)}</td><td>${esc(s.state)}</td><td>r${s.revision_no}</td></tr>`).join("")}</table>`);
  }
  return out || card("Skills", "No evolved skills yet — they version as employees work (skill_manage is in-beat, internal).");
}

async function renderLLMOps() {
  const [byModel, byDay] = await Promise.all([api("/costs?by=model"), api("/costs?by=day")]);
  const table = (rows) => `<table class="t"><tr><th>key</th><th>$ spend</th><th>in tok</th><th>out tok</th><th>calls</th></tr>${rows.map((r) => `<tr><td>${esc(r.key)}</td><td>${(r.cost_cents / 100).toFixed(2)}</td><td>${r.input_tokens}</td><td>${r.output_tokens}</td><td>${r.events}</td></tr>`).join("")}</table>`;
  return `<div class="cols">${card("Spend by model", byModel.length ? table(byModel) : "—")}${card("Spend by day", byDay.length ? table(byDay) : "—")}</div>` +
    card("Live model calls", `<ol class="feed">${state.feeds.tail.filter((e) => e.type === "llm.call").slice(0, 20).map((e) => `<li>${esc(e.employee_id)} · ${esc(e.payload.model)} · ${e.payload.input_tokens}→${e.payload.output_tokens} tok</li>`).join("") || "<li>—</li>"}</ol>`);
}

async function renderOps() {
  return card(`Stalled · ${state.feeds.stalled.length}`, state.feeds.stalled.length
    ? `<ol class="feed">${state.feeds.stalled.map((e) => `<li class="stalled">${esc(e.employee_id)} · run ${esc((e.run_id || "").slice(0, 8))} · ${esc(e.payload.reason)}</li>`).join("")}</ol>` : "None — quiet lanes are healthy lanes.") +
    card("Raw spine (last 200)", `<ol class="feed">${state.feeds.tail.map((e) => `<li>${e.seq ?? ""} [${esc(e.employee_id ?? "system")}] ${esc(e.type)}</li>`).join("")}</ol>`);
}

/* ================= post-render wiring ================= */
function wireView() {
  document.querySelectorAll(".decide-form").forEach((form) => {
    form.onsubmit = (e) => e.preventDefault();
    form.querySelectorAll("button").forEach((btn) => {
      btn.onclick = async (e) => {
        e.preventDefault();
        await api2("POST", `/plans/${form.dataset.plan}/${btn.dataset.decision}`, {});
        await refreshSnapshot(); render();
      };
    });
  });
  const goal = $("goal-form");
  if (goal) goal.onsubmit = async (e) => {
    e.preventDefault();
    await api2("POST", "/goals", { title: $("goal-title").value, level: $("goal-level").value });
    render();
  };
  const hire = $("hire-form");
  if (hire) hire.onsubmit = async (e) => {
    e.preventDefault();
    const reports = $("hire-boss").value.trim();
    await api2("POST", "/employees", { name: $("hire-name").value, role: $("hire-role").value, reports_to: reports || null });
    await seedLanes(); await refreshSnapshot(); render();
  };
  const trace = $("trace-form");
  if (trace) trace.onsubmit = async (e) => {
    e.preventDefault();
    const runId = $("trace-id").value.trim();
    const page = await fetch(`/v1/runs/${runId}/events?limit=200`, { headers: { Authorization: `Bearer ${state.ctx.token}` } }).then((r) => r.json());
    $("run-trace").innerHTML = (page.data ?? []).map((ev) => `<li class="${ev.type === "memory.retrieved" ? "memory" : ""}">${ev.seq} ${esc(ev.type)} ${esc(JSON.stringify(ev.payload).slice(0, 110))}</li>`).join("");
  };
}
const api2 = (method, path, body) =>
  fetch(`/v1/workspaces/${state.ctx.workspaceId}/companies/${state.ctx.companyId}${path}`, {
    method, headers: { Authorization: `Bearer ${state.ctx.token}`, "Content-Type": "application/json" }, body: JSON.stringify(body),
  }).then((r) => { if (!r.ok) throw new Error(`${path} -> ${r.status}`); return r.json(); });

/* ================= the live architecture map (mirrors chorus-system-architecture-v2) ======= */
function archSVG(c) {
  const box = (x, y, w, h, title, sub, view, nid) => `
    <g class="node" data-node="${nid}" ${view ? `onclick="location.hash='${view}'"` : ""}>
      <rect class="bx" x="${x}" y="${y}" width="${w}" height="${h}" rx="8"/>
      <text class="nt" x="${x + 11}" y="${y + 20}">${title}</text>
      ${sub ? `<text class="ns" x="${x + 11}" y="${y + 36}">${sub}</text>` : ""}
    </g>`;
  const flow = (d, cls = "", eid = "") => `<path class="flow ${cls}" ${eid ? `data-edge="${eid}"` : ""} d="${d}"/>`;
  return `<div style="overflow-x:auto"><svg viewBox="0 0 1080 560" class="arch" role="img">
    <defs><marker id="arr" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="6.5" markerHeight="6.5" orient="auto-start-reverse"><path d="M0 0 L10 5 L0 10 z" class="head"/></marker></defs>

    <rect class="zone zone-horizon" x="12" y="16" width="250" height="240" rx="14"/>
    <text class="zlbl" x="26" y="38">HORIZON — decides what's next</text>
    ${box(28, 52, 218, 44, "Generation funnel", "briefs → proposals", "direction", "hz-funnel")}
    ${box(28, 108, 218, 44, "Decision → goal tree", `${c.horizon.goals} goal roots`, "direction", "hz-goals")}
    ${box(28, 164, 218, 44, "Intake + prioritiser", "score → task priority", "direction", "hz-intake")}
    ${flow("M137 96 L137 108")}${flow("M137 152 L137 164")}

    <rect class="zone zone-contracts" x="12" y="272" width="250" height="72" rx="14"/>
    <text class="zlbl" x="26" y="294">dream.contracts — the only seam</text>
    <text class="ns" x="28" y="316">Strategy · Delegation · Governance ports</text>
    ${flow("M137 208 L137 272", "", "e-hz-contracts")}
    ${flow("M262 308 L296 308", "", "e-contracts-chorus")}

    <rect class="zone zone-chorus" x="296" y="16" width="420" height="420" rx="14"/>
    <text class="zlbl" x="310" y="38">CHORUS — runs the org</text>
    ${box(312, 52, 180, 44, "Facade", "submit · hire · tick", "work", "ch-facade")}
    ${box(516, 52, 180, 44, "EventBus", "the telemetry spine", "ops", "ch-bus")}
    ${box(312, 116, 180, 44, "Scheduler heartbeat", `${c.chorus.running_beats} beats · ${c.chorus.queued_wakes} wakes`, "work", "ch-sched")}
    ${box(516, 116, 180, 44, "Ledger (Postgres)", `${c.chorus.open_tasks} open tasks`, "work", "ch-ledger")}
    ${box(312, 196, 180, 44, "Harness factory", "worktree · skills · TDD", "org", "ch-factory")}
    ${box(516, 196, 180, 44, "Delegation kernel", `${c.delegation.teams} teams · internal`, "delegation", "ch-delg")}
    ${box(312, 276, 180, 44, "Workforce", `${c.chorus.employees} employees`, "org", "ch-org")}
    ${box(516, 276, 180, 44, "chorus_tools", "recall · lattice · skill_manage", "episodic", "ch-tools")}
    ${box(312, 356, 384, 44, "EpisodicStore (FTS5)", `${c.episodic.records} beat records — every beat writes one`, "episodic", "ch-episodic")}
    ${flow("M402 96 L402 116", "", "e-facade-sched")}
    ${flow("M492 138 L516 138", "", "e-sched-ledger")}
    ${flow("M402 160 L402 196", "", "e-sched-factory")}
    ${flow("M606 96 L606 116", "dash", "e-bus")}
    ${flow("M402 240 L402 276")}${flow("M606 160 L606 196", "", "e-ledger-delg")}
    ${flow("M402 320 L402 356", "dash", "e-beat-episodic")}${flow("M606 320 L606 356", "dash", "e-tools-episodic")}

    <rect class="zone zone-dream" x="744" y="16" width="320" height="180" rx="14"/>
    <text class="zlbl" x="758" y="38">DREAM — the agent harness</text>
    ${box(760, 52, 288, 44, "build_harness", "sandbox · tools · hooks", "org", "dr-harness")}
    ${box(760, 116, 288, 60, "plan → generate → evaluate", `oracle verdict · ${c.llmops.spend_cents ? "$" + (c.llmops.spend_cents / 100).toFixed(2) + " spent" : "no spend yet"}`, "llmops", "dr-loop")}
    ${flow("M716 138 L760 140", "", "e-chorus-dream")}
    ${flow("M904 96 L904 116", "", "e-harness-loop")}

    <rect class="zone zone-lattice" x="744" y="216" width="320" height="220" rx="14"/>
    <text class="zlbl" x="758" y="238">LATTICE — the learning loop</text>
    ${box(760, 252, 288, 40, "EpisodicReader → gate", "≥5 beats · cluster ≥2 · internal", "episodic", "lt-gate")}
    ${box(760, 306, 138, 56, "Semantic atoms", `${c.semantic.atoms} facts`, "semantic", "lt-semantic")}
    ${box(910, 306, 138, 56, "Skill evolve", `${c.skills.heads} skills`, "skills", "lt-skills")}
    ${box(760, 380, 288, 40, "context() → beats", "retrieval at the moment of use", "episodic", "lt-retrieve")}
    ${flow("M829 292 L829 306", "", "e-gate-sem")}${flow("M979 292 L979 306", "", "e-gate-skill")}
    ${flow("M829 362 L829 380", "dash")}${flow("M696 378 L760 396", "dash", "e-episodic-lattice")}
    ${flow("M760 400 C 700 470 480 470 420 400", "dash", "e-lattice-beats")}
  </svg></div>`;
}

/* SSE event type → which edges/nodes pulse (waku's lighting pattern) */
const EDGE_MAP = {
  "task.created": ["e-hz-contracts", "e-contracts-chorus", "ch-facade"],
  "task.assigned": ["ch-facade", "e-facade-sched"],
  "wake.enqueued": ["e-facade-sched", "ch-sched"],
  "wake.claimed": ["ch-sched", "e-sched-ledger"],
  "task.status": ["ch-ledger"],
  "run.started": ["e-sched-factory", "ch-factory", "e-chorus-dream", "dr-harness"],
  "run.text": ["dr-loop", "e-harness-loop"],
  "run.tool_use": ["dr-loop"],
  "run.tool_result": ["dr-loop"],
  "llm.call": ["dr-loop"],
  "run.evaluated": ["dr-loop"],
  "run.done": ["e-beat-episodic", "ch-episodic", "e-episodic-lattice"],
  "memory.retrieved": ["ch-tools", "e-tools-episodic", "lt-retrieve", "e-lattice-beats"],
  "run.subagent_spawned": ["dr-loop"],
  "run.stalled": ["ch-sched"],
  "budget.hard_stop": ["ch-sched"],
};
function lightMap(ev) {
  const targets = EDGE_MAP[ev.type];
  if (!targets) return;
  for (const id of targets) {
    document.querySelectorAll(`[data-edge="${id}"], [data-node="${id}"]`).forEach((el) => {
      const cls = ev.type === "run.stalled" || ev.type === "budget.hard_stop" ? "bad" : "hot";
      el.classList.add(cls);
      setTimeout(() => el.classList.remove(cls), 900);
    });
  }
}

/* ================= the delegation whiteboard flow ================= */
function delegationSVG(teamsLive, pendingPlans = 0) {
  const bx = (x, y, w, t, live = "") => `
    <g class="node"><rect class="bx" x="${x}" y="${y}" width="${w}" height="40" rx="7"/>
    <text class="nt" x="${x + w / 2}" y="${y + 20}" text-anchor="middle">${t}</text>
    ${live ? `<text class="live" x="${x + w / 2}" y="${y + 34}" text-anchor="middle">${live}</text>` : ""}</g>`;
  const fl = (d) => `<path class="flow" d="${d}"/>`;
  return `<div style="overflow-x:auto"><svg viewBox="0 0 900 520" class="arch delg" role="img">
    <defs><marker id="arr2" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="6.5" markerHeight="6.5" orient="auto-start-reverse"><path d="M0 0 L10 5 L0 10 z" class="head"/></marker></defs>
    ${bx(360, 8, 180, "Founder intent")}
    ${fl("M450 48 L450 64")}
    ${bx(340, 64, 220, "Horizon evidence and goals")}
    ${fl("M450 104 L450 120")}
    ${bx(350, 120, 200, "CEO workforce proposal")}
    ${fl("M450 160 L450 176")}
    ${pendingPlans ? `<g class="node hot">` : `<g class="node">`}<rect class="bx" x="350" y="176" width="200" height="40" rx="7"/><text class="nt" x="450" y="196" text-anchor="middle">Human approve or revise</text>${pendingPlans ? `<text class="live" x="450" y="210" text-anchor="middle">${pendingPlans} waiting for you</text>` : ""}</g>
    ${fl("M420 216 C 350 240 280 240 250 256")}
    ${bx(120, 256, 260, "Permanent workforce · shallow line org")}
    ${fl("M190 296 L150 328")}${fl("M310 296 L350 328")}
    ${bx(60, 328, 160, "Goal A")}${bx(300, 328, 160, "Goal B")}
    ${fl("M140 368 L140 392")}${fl("M380 368 L380 392")}
    ${bx(60, 392, 160, "Mission Team A", teamsLive + " live")}${bx(300, 392, 160, "Mission Team B")}
    ${fl("M140 432 L140 452")}${fl("M380 432 L380 452")}
    ${bx(60, 452, 160, "Verified completion")}${bx(300, 452, 160, "Verified completion")}
    ${fl("M460 348 C 560 360 600 380 640 392")}
    <text class="fl" x="540" y="370">missing capability</text>
    ${bx(600, 392, 190, "Typed staffing request")}
    ${fl("M695 432 C 720 470 780 300 700 240 C 660 210 580 200 552 196")}
    ${bx(620, 200, 200, "CEO workforce amendment")}
  </svg></div>`;
}

/* boot */
render();
