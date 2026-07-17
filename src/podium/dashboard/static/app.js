/* podium cockpit — one SSE connection, client-side demux into projections (OBS P4).
 *
 * Concurrency rules (OBS P1/P3): lanes update independently; the raw tail interleaves but every
 * row is lane-tagged; nothing draws cross-lane sequence as causality. Reconnect resumes from
 * Last-Event-ID (native EventSource behaviour — the server replays from the cursor), so N
 * mid-flight employees rebuild as N live lanes, never a merged timeline.
 */
"use strict";

const state = {
  ctx: null, // {workspaceId, companyId, token}
  lanes: new Map(), // employee_id -> {status, taskId, runId, lastEvents: []}
  stream: null,
};

const $ = (id) => document.getElementById(id);
const api = (path) =>
  fetch(`/v1/workspaces/${state.ctx.workspaceId}/companies/${state.ctx.companyId}${path}`, {
    headers: { Authorization: `Bearer ${state.ctx.token}` },
  }).then((r) => {
    if (!r.ok) throw new Error(`${path} -> ${r.status}`);
    return r.json();
  });

/* ---------- tabs ---------- */
document.querySelectorAll("#tabs button").forEach((button) => {
  button.addEventListener("click", () => {
    document.querySelectorAll("#tabs button, .tab").forEach((el) => el.classList.remove("active"));
    button.classList.add("active");
    $(`tab-${button.dataset.tab}`).classList.add("active");
    if (button.dataset.tab === "work") refreshWork();
    if (button.dataset.tab === "costs") refreshCosts();
  });
});

/* ---------- connect ---------- */
$("connect-form").addEventListener("submit", (event) => {
  event.preventDefault();
  state.ctx = {
    workspaceId: $("workspace-id").value.trim(),
    companyId: $("company-id").value.trim(),
    token: $("token").value.trim(),
  };
  localStorage.setItem(
    "podium.ctx",
    JSON.stringify({ workspaceId: state.ctx.workspaceId, companyId: state.ctx.companyId })
  );
  connect();
});

(function restore() {
  const saved = localStorage.getItem("podium.ctx");
  if (!saved) return;
  const { workspaceId, companyId } = JSON.parse(saved);
  $("workspace-id").value = workspaceId || "";
  $("company-id").value = companyId || "";
})();

async function connect() {
  setStatus("connecting");
  try {
    await seedOrg();
    openStream();
  } catch (error) {
    setStatus("error", String(error));
  }
}

function setStatus(stateName, detail) {
  const el = $("connection-status");
  el.dataset.state = stateName;
  el.textContent = detail ? `${stateName}: ${detail}` : stateName;
}

/* ---------- Org: workforce lanes (seed from snapshot, live from the spine) ---------- */
async function seedOrg() {
  const roster = await api("/workforce");
  state.lanes.clear();
  for (const member of roster) {
    state.lanes.set(member.id, {
      name: member.name,
      role: member.role,
      status: member.status,
      taskId: null,
      runId: null,
      lastEvents: [],
    });
  }
  renderLanes();
}

function renderLanes() {
  const container = $("lanes");
  container.replaceChildren();
  for (const [employeeId, lane] of state.lanes) {
    const el = document.createElement("article");
    el.className = `lane state-${lane.status}`;
    el.innerHTML = `
      <header><strong>${lane.name}</strong> <em>${lane.role}</em>
        <span class="chip">${lane.status}</span></header>
      <div class="lane-task">${lane.taskId ? `task ${lane.taskId.slice(0, 8)}…` : "—"}</div>
      <ol class="lane-events">${lane.lastEvents
        .map((e) => `<li><code>${e.type}</code></li>`)
        .join("")}</ol>`;
    el.dataset.employee = employeeId;
    container.appendChild(el);
  }
}

/* ---------- the spine: one SSE connection, demuxed ----------
 * The stream emits NAMED events (`event: run.text`), which EventSource.onmessage never
 * delivers (found by live visual test: a silent dashboard over a talking stream). A small
 * fetch-based reader handles every name, sends the token as a header (never in the URL),
 * and resumes from Last-Event-ID on reconnect. */
let lastEventId = null;
let streamGeneration = 0;

async function openStream() {
  const generation = ++streamGeneration; // a reconnect invalidates older readers
  setStatus("connecting");
  while (generation === streamGeneration) {
    try {
      const headers = { Authorization: `Bearer ${state.ctx.token}` };
      if (lastEventId !== null) headers["Last-Event-ID"] = String(lastEventId);
      const response = await fetch(`/v1/companies/${state.ctx.companyId}/stream`, { headers });
      if (!response.ok) throw new Error(`stream -> ${response.status}`);
      setStatus("live");
      const reader = response.body.getReader();
      const decoder = new TextDecoder();
      let buffer = "";
      for (;;) {
        const { value, done } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
        let boundary;
        while ((boundary = buffer.indexOf("\n\n")) !== -1) {
          const frame = buffer.slice(0, boundary);
          buffer = buffer.slice(boundary + 2);
          handleFrame(frame);
        }
      }
    } catch {
      // fall through to retry
    }
    if (generation !== streamGeneration) return;
    setStatus("reconnecting");
    await new Promise((resolve) => setTimeout(resolve, 2000));
  }
}

function handleFrame(frame) {
  let data = "";
  for (const line of frame.split("\n")) {
    if (line.startsWith("id:")) lastEventId = line.slice(3).trim();
    else if (line.startsWith("data:")) data += line.slice(5).trim();
  }
  if (!data) return; // comments / keepalives
  let event;
  try {
    event = JSON.parse(data);
  } catch {
    return; // never let one bad frame kill the demux
  }
  project(event);
}

/* Every view is a fold over the same events (OBS P4) — add a projection, not a collector. */
function project(event) {
  tail(event);
  laneFold(event);
  opsFold(event);
}

const LANE_STATES = {
  "wake.claimed": "claimed",
  "task.status": "running",
  "run.started": "running",
  "run.tool_use": "tool",
  "run.text": "running",
  "run.done": "idle",
  "run.stalled": "stalled",
  "budget.hard_stop": "blocked",
};

function laneFold(event) {
  if (!event.employee_id) return; // company-level event — system row, never a lane default (P2)
  let lane = state.lanes.get(event.employee_id);
  if (!lane) {
    // An employee hired after the snapshot (e.g. the conductor's default worker) gets a lane
    // on first sight — live truth beats the seed (found by the live e2e: Ace had no lane).
    lane = { name: event.employee_id, role: "", status: "idle", taskId: null, runId: null, lastEvents: [] };
    state.lanes.set(event.employee_id, lane);
  }
  const mapped = LANE_STATES[event.type];
  if (mapped) lane.status = mapped;
  if (event.task_id) lane.taskId = event.task_id;
  if (event.run_id) lane.runId = event.run_id;
  lane.lastEvents = [{ type: event.type }, ...lane.lastEvents].slice(0, 5);
  renderLanes();
}

function tail(event) {
  const li = document.createElement("li");
  li.textContent = `${event.seq ?? ""} [${event.employee_id ?? "system"}] ${event.type}`;
  const list = $("raw-tail");
  list.prepend(li);
  while (list.children.length > 200) list.removeChild(list.lastChild);
}

function opsFold(event) {
  if (event.type !== "run.stalled") return;
  const li = document.createElement("li");
  li.className = "stalled";
  li.textContent = `${event.employee_id}: run ${event.run_id} (${event.payload?.reason ?? ""})`;
  $("stalled").prepend(li);
}

/* ---------- Work: allocation board snapshot ---------- */
async function refreshWork() {
  const board = await api("/allocation");
  fillList("queued", board.queued, (w) => `${w.employee_id} ← ${w.reason} (${w.coalesced})`);
  fillList(
    "running",
    board.running,
    (r) => `${r.employee_id} · run ${r.run_id.slice(0, 8)}… lease ${r.lease_expires_at ?? "—"}`
  );
  fillList("blocked", board.blocked, (b) => `${b.task_id.slice(0, 8)}… ${b.intent_excerpt}`);
}

function fillList(id, rows, render) {
  const list = $(id);
  list.replaceChildren();
  for (const row of rows) {
    const li = document.createElement("li");
    li.textContent = render(row);
    list.appendChild(li);
  }
}

/* ---------- Runs: per-run trace ---------- */
$("load-run").addEventListener("click", async () => {
  const runId = $("run-id").value.trim();
  if (!runId) return;
  const page = await fetch(`/v1/runs/${runId}/events?limit=200`, {
    headers: { Authorization: `Bearer ${state.ctx.token}` },
  }).then((r) => r.json());
  const trace = $("run-trace");
  trace.replaceChildren();
  for (const event of page.data ?? []) {
    const li = document.createElement("li");
    li.className = event.type === "memory.retrieved" ? "memory" : event.type.replace(/\W/g, "-");
    li.textContent = `${event.seq} ${event.type} ${JSON.stringify(event.payload).slice(0, 120)}`;
    trace.appendChild(li);
  }
});

/* ---------- Costs ---------- */
async function refreshCosts() {
  const by = $("costs-by").value;
  const rows = await api(`/costs?by=${by}`);
  const body = $("costs-table").querySelector("tbody");
  body.replaceChildren();
  for (const row of rows) {
    const tr = document.createElement("tr");
    tr.innerHTML = `<td>${row.key}</td><td>${(row.cost_cents / 100).toFixed(2)}</td>
      <td>${row.input_tokens}</td><td>${row.output_tokens}</td><td>${row.events}</td>`;
    body.appendChild(tr);
  }
}
$("costs-by").addEventListener("change", refreshCosts);
