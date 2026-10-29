// AI Project Office UI: a projection of the project's REST resources and event
// stream (spec §12). Nothing here invents state: the office shows an agent as
// working only while the API reports a run, speech balloons only show real
// message text, and the meeting room fills only during a live meeting (BR-09, AC-05).
"use strict";

const $ = (id) => document.getElementById(id);

function el(tag, cls, text) {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text != null) n.textContent = text;
  return n;
}

const TASK_STATUS = {
  waiting: "chờ phụ thuộc", pending: "sẵn sàng", awaiting_approval: "chờ duyệt", active: "đang chạy", paused: "tạm dừng",
  blocked: "bị chặn", completed: "hoàn thành", failed: "thất bại", cancelled: "đã hủy",
};
const KIND_ICON = { work: "🛠", meeting: "👥", dm: "💬", plan: "📋" };
const IV_ACTION = { pause: "tạm dừng", resume: "tiếp tục", cancel: "hủy", redirect: "chuyển hướng" };
const APPROVAL_STATUS = { pending: "chờ duyệt", approved: "đã duyệt", rejected: "bị từ chối", expired: "hết hạn", cancelled: "đã rút lại" };
const RISK_LABEL = { deploy_production: "deploy production", payment: "thanh toán", delete_data: "xóa dữ liệu", export_sensitive_data: "xuất dữ liệu nhạy cảm" };
const IV_STATUS = { requested: "đã yêu cầu — chờ xác nhận", applied: "đã áp dụng", rejected: "bị từ chối" };
const EVENT_LABEL = {
  "project.created": "Tạo project", "agent.assigned": "Gán agent", "task.created": "Tạo task",
  "task.started": "Bắt đầu task", "task.blocked": "Task bị chặn", "task.completed": "Hoàn thành",
  "message.sent": "Tin nhắn", "meeting.started": "Bắt đầu họp", "meeting.ended": "Kết thúc họp",
  "decision.recorded": "Quyết định", "artifact.created": "Artifact", "approval.requested": "Xin duyệt",
  "approval.resolved": "Kết quả duyệt", "run.failed": "Run lỗi", "intervention.requested": "Yêu cầu can thiệp",
  "intervention.applied": "Can thiệp xử lý",
};
const ADAPTER_COLOR = { "claude-agent-sdk": "#d97757", "codex-cli": "#10a37f", mock: "#7aa2f7", "mock-suno": "#c792ea", "mock-tripo": "#89ddff" };
const JOB_POLL_MS = 2000; // async jobs report progress through the API, not through events
const ERROR_SHOW_MS = 8000;
const TIMELINE_MAX = 1500;

const state = {
  token: null, projects: [], pid: null, project: null,
  agents: [], tasks: [], convs: [], meetings: [], interventions: [], artifacts: [], adapters: [],
  approvals: [], metrics: null, artifact: null, artifactUrl: null, jobPoll: null,
  events: [], lastSeq: 0, chat: [], ws: null, wsTimer: null,
  openTasks: new Set(), talk: null, selectedAgent: null, errorUntil: new Map(), filter: "",
};

// ---------------------------------------------------------------- API

const uuid = () => (crypto.randomUUID ? crypto.randomUUID() : String(Date.now()) + Math.random().toString(16).slice(2));

async function api(method, path, body) {
  const headers = { Authorization: `Bearer ${state.token}` };
  if (body !== undefined) Object.assign(headers, { "Content-Type": "application/json", "Idempotency-Key": uuid() });
  const res = await fetch(path, { method, headers, body: body === undefined ? undefined : JSON.stringify(body) });
  const data = await res.json().catch(() => ({}));
  if (res.status === 401) { logout(); throw new Error("unauthenticated"); }
  if (!res.ok) {
    const err = new Error(data.error?.message || res.statusText);
    err.code = data.error?.code;
    throw err;
  }
  return data;
}
const P = (path = "") => `/projects/${state.pid}${path}`;

// ---------------------------------------------------------------- lookups

const agentById = (id) => state.agents.find((a) => a.id === id);
const taskById = (id) => state.tasks.find((t) => t.id === id);
const nameOf = (id) => agentById(id)?.name || (id && id.startsWith("usr_") ? "Bạn" : id === "system" ? "hệ thống" : id || "?");
const orchestrator = () => state.agents.find((a) => a.is_active_orchestrator);
const pendingIv = (taskId) => state.interventions.find((i) => i.task_id === taskId && i.status === "requested");
const fmtTime = (iso) => new Date(iso).toLocaleTimeString("vi-VN", { hour12: false });

function agentStatus(a) {
  if (a.current_runs.length) return "working";
  if ((state.errorUntil.get(a.id) || 0) > Date.now()) return "error";
  return "idle";
}

// ---------------------------------------------------------------- office

const office = new Office($("office"), $("labels"));
office.observeOnly = true;
office.start();
office.onClick((id) => selectAgent(id));

function syncOffice() {
  office.setEmployees(
    state.agents.map((a) => ({
      id: a.id,
      name: a.name,
      role: a.is_active_orchestrator ? "điều phối" : a.role === "orchestrator" ? "điều phối (cũ)" : a.capabilities.join(", "),
      color: a.is_active_orchestrator ? "#ffd166" : ADAPTER_COLOR[a.adapter_type] || "#9aa4b2",
    })),
  );
  for (const a of state.agents) office.setStatus(a.id, agentStatus(a));
  const inRoom = new Set();
  for (const m of state.meetings.filter((m) => m.live)) {
    m.participants.forEach((p) => inRoom.add(p));
    inRoom.add(m.moderator_id);
  }
  office.setMeeting([...inRoom]);
  office.fit();
  $("officeNote").textContent = state.agents.length
    ? "Office chỉ hiển thị trạng thái có thật từ API/event; không có hoạt động giả lập."
    : "Chưa có agent. Thêm ở tab Agent.";
  renderRoster();
}

function renderRoster() {
  const host = $("roster");
  host.replaceChildren();
  for (const a of state.agents) {
    const st = agentStatus(a);
    const chip = el("button", "roster-chip" + (state.selectedAgent === a.id ? " selected" : ""));
    chip.type = "button";
    chip.append(el("span", "po-dot " + st), el("span", "rn", a.name));
    const run = a.current_runs[0];
    const label = run ? runLabel(run) : st === "error" ? "lỗi" : "rảnh";
    chip.append(el("span", "rs " + st, label));
    chip.onclick = () => selectAgent(a.id);
    host.append(chip);
  }
}

function runLabel(run) {
  if (run.purpose === "orchestrator_turn") return "đang trả lời";
  if (run.purpose === "conversation_turn") return "đang phát biểu";
  if (run.provider_ref) return run.progress != null ? `job ${Math.round(run.progress * 100)}%` : "job đang chờ";
  return "đang làm task";
}

// ---------------------------------------------------------------- loading

async function loadProjects(selectId) {
  state.projects = await api("GET", "/projects");
  const sel = $("projectSelect");
  sel.replaceChildren(...state.projects.map((p) => Object.assign(el("option", null, p.name), { value: p.id })));
  const want = selectId || localStorageGet("po.project");
  const pick = state.projects.find((p) => p.id === want) || state.projects[0];
  if (pick) { sel.value = pick.id; await openProject(pick.id); }
  else { $("officeNote").textContent = "Chưa có project. Bấm “+ Project” để tạo."; }
}

async function openProject(pid) {
  closeStream();
  Object.assign(state, {
    pid, events: [], lastSeq: 0, chat: [], talk: null, selectedAgent: null,
    agents: [], tasks: [], convs: [], meetings: [], interventions: [], artifacts: [],
    approvals: [], metrics: null, artifact: null,
  });
  $("artifactView").replaceChildren();
  state.openTasks.clear();
  state.errorUntil.clear();
  localStorageSet("po.project", pid);
  $("agentTabBtn").hidden = true;
  await loadAll(); // agent names first: the timeline renders them
  await loadEventsBackfill();
  openStream();
}

const LOADERS = {
  project: async () => { state.project = await api("GET", P()); renderProjectBar(); },
  agents: async () => {
    const known = state.agents.length;
    state.agents = await api("GET", P("/agents"));
    syncOffice(); renderAgents(); renderAgentDetail(); renderBusy();
    if (state.agents.length !== known) renderTimeline(); // new names for already-rendered rows
    scheduleJobPoll();
  },
  tasks: async () => { state.tasks = await api("GET", P("/tasks")); renderTasks(); renderAgentDetail(); },
  convs: async () => { state.convs = await api("GET", P("/conversations")); await loadChat(); renderTalks(); },
  meetings: async () => { state.meetings = await api("GET", P("/meetings")); syncOffice(); renderTalks(); },
  interventions: async () => { state.interventions = await api("GET", P("/interventions")); renderTasks(); renderProjectBar(); },
  artifacts: async () => { state.artifacts = await api("GET", P("/artifacts")); renderTasks(); renderArtifacts(); },
  approvals: async () => { state.approvals = await api("GET", P("/approvals")); renderApprovals(); renderTasks(); },
  metrics: async () => { state.metrics = await api("GET", P("/metrics")); renderMetrics(); },
};

// Job progress has no event of its own: while a provider job runs, re-read it.
function scheduleJobPoll() {
  const jobs = state.agents.some((a) => a.current_runs.some((r) => r.provider_ref));
  if (!jobs || state.jobPoll) return;
  state.jobPoll = setTimeout(() => { state.jobPoll = null; markDirty("agents", "tasks"); }, JOB_POLL_MS);
}

async function loadAll() {
  await LOADERS.agents(); // names first: everything else renders agent names
  await Promise.all(["project", "tasks", "convs", "meetings", "interventions", "artifacts", "approvals"].map((k) => LOADERS[k]()));
}

async function loadChat() {
  const conv = state.convs.find((c) => c.type === "owner_chat");
  state.chat = conv ? await api("GET", P(`/conversations/${conv.id}/messages`)) : [];
  renderChat();
}

async function loadEventsBackfill() {
  let after = 0;
  for (;;) {
    const page = await api("GET", P(`/events?after_seq=${after}&limit=1000`));
    state.events.push(...page.events);
    after = page.next_after_seq;
    if (page.events.length < 1000) break;
  }
  state.events = state.events.slice(-TIMELINE_MAX);
  state.lastSeq = after;
  renderTimeline();
}

// Debounced refresh of the resources an event touches.
const dirty = new Set();
let dirtyTimer = null;
function markDirty(...keys) {
  keys.forEach((k) => dirty.add(k));
  if ($("tab-metrics").classList.contains("active")) dirty.add("metrics");
  clearTimeout(dirtyTimer);
  dirtyTimer = setTimeout(async () => {
    const keysNow = [...dirty];
    dirty.clear();
    try { await Promise.all(keysNow.map((k) => LOADERS[k]())); }
    catch (e) { console.warn("refresh failed", e); }
    if (state.talk && keysNow.includes("convs")) loadTalk(state.talk);
  }, 250);
}

// ---------------------------------------------------------------- realtime

function openStream() {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  const url = `${proto}://${location.host}${P("/stream")}?token=${encodeURIComponent(state.token)}&after_seq=${state.lastSeq}`;
  const pid = state.pid;
  const ws = new WebSocket(url);
  state.ws = ws;
  setConn("wait", "đang kết nối");
  ws.onopen = () => setConn("on", "live");
  ws.onmessage = (m) => handleEvent(JSON.parse(m.data));
  ws.onclose = (ev) => {
    if (state.ws !== ws) return; // replaced by another project/stream
    state.ws = null;
    if (ev.code === 4401) return logout();
    setConn("off", "mất kết nối — thử lại…");
    state.wsTimer = setTimeout(() => { if (state.pid === pid) openStream(); }, 1500);
  };
}

function closeStream() {
  clearTimeout(state.wsTimer);
  const ws = state.ws;
  state.ws = null;
  if (ws) ws.close();
}

function setConn(cls, text) {
  const pill = $("connPill");
  pill.className = "po-pill " + cls;
  pill.textContent = text;
}

function handleEvent(e) {
  if (e.sequence <= state.lastSeq) return; // replay overlap: de-dup by sequence
  state.lastSeq = e.sequence;
  state.events.push(e);
  if (state.events.length > TIMELINE_MAX) state.events.shift();
  appendTimeline(e);

  const t = e.type;
  if (t === "message.sent") {
    if (agentById(e.actor_id) && e.payload.preview) office.speak(e.actor_id, e.payload.preview);
    markDirty("convs", "agents");
  } else if (t.startsWith("task.")) markDirty("tasks", "agents");
  else if (t === "run.failed") {
    if (!e.payload.will_retry) state.errorUntil.set(e.actor_id, Date.now() + ERROR_SHOW_MS);
    setTimeout(syncOffice, ERROR_SHOW_MS + 50);
    markDirty("tasks", "agents");
  } else if (t.startsWith("meeting.") || t === "decision.recorded") markDirty("meetings", "convs", "tasks", "agents");
  else if (t === "agent.assigned") markDirty("agents");
  else if (t.startsWith("intervention.")) markDirty("interventions", "tasks", "project", "agents");
  else if (t === "artifact.created") markDirty("artifacts");
  else if (t.startsWith("approval.")) markDirty("approvals", "tasks");
}

// ---------------------------------------------------------------- top bar

function renderProjectBar() {
  const btn = $("projectPause");
  btn.hidden = !state.project;
  if (!state.project) return;
  const paused = state.project.status === "paused";
  // Who paused it: the latest applied project-level pause (system = budget circuit breaker).
  const pause = paused && state.interventions.filter((i) => !i.task_id && i.action === "pause" && i.status === "applied").at(-1);
  const bySystem = pause && pause.requested_by === "system";
  btn.textContent = paused ? `▶ Tiếp tục project${bySystem ? " (đã dừng: vượt ngân sách)" : ""}` : "⏸ Tạm dừng project";
  btn.title = bySystem ? pause.outcome || "" : "";
  btn.classList.toggle("danger", !paused);
}

$("projectPause").onclick = async () => {
  const action = state.project.status === "paused" ? "resume" : "pause";
  try { await api("POST", P("/interventions"), { action }); } catch (e) { alert(e.message); }
  markDirty("project", "interventions");
};

$("projectSelect").onchange = (ev) => openProject(ev.target.value);
$("newProject").onclick = async () => {
  const name = prompt("Tên project mới:");
  if (!name) return;
  const res = await api("POST", "/projects", { name });
  await loadProjects(res.data.id);
};

// ---------------------------------------------------------------- chat

function renderChat() {
  const log = $("chatLog");
  const atBottom = log.scrollHeight - log.scrollTop - log.clientHeight < 40;
  log.replaceChildren();
  if (!state.chat.length) log.append(el("p", "po-empty", "Chưa có hội thoại. Gửi yêu cầu cho Điều phối viên để bắt đầu."));
  for (const m of state.chat) {
    const isAgent = !!agentById(m.sender_id);
    const box = el("div", "po-msg" + (isAgent ? "" : " owner"));
    box.append(el("span", "who", `${nameOf(m.sender_id)} · ${fmtTime(m.created_at)}`), document.createTextNode(m.content ?? "(nội dung không còn)"));
    log.append(box);
  }
  if (atBottom) log.scrollTop = log.scrollHeight;
}

function renderBusy() {
  const o = orchestrator();
  const busy = o && o.current_runs.some((r) => r.purpose === "orchestrator_turn");
  $("orchBusy").hidden = !busy;
  $("orchBusy").textContent = busy ? `${o.name} đang soạn trả lời…` : "";
}

$("chatForm").onsubmit = async (ev) => {
  ev.preventDefault();
  const text = $("chatInput").value.trim();
  if (!text) return;
  $("chatError").hidden = true;
  try {
    await api("POST", P("/orchestrator/messages"), { content: text });
    $("chatInput").value = "";
    markDirty("convs", "agents");
  } catch (e) {
    $("chatError").hidden = false;
    $("chatError").textContent = e.code === "no_orchestrator" ? "Project chưa có Điều phối viên. Thêm agent vai trò orchestrator ở tab Agent." : e.message;
  }
};
$("chatInput").onkeydown = (ev) => { if (ev.key === "Enter" && (ev.ctrlKey || ev.metaKey)) $("chatForm").requestSubmit(); };

// ---------------------------------------------------------------- tasks

function statusBadge(status, label) {
  return el("span", `po-badge st-${status}`, label || TASK_STATUS[status] || status);
}

function renderTasks() {
  const host = $("taskBoard");
  host.replaceChildren();
  const plans = state.tasks.filter((t) => t.kind === "plan").reverse();
  if (!plans.length) { host.append(el("p", "po-empty", "Chưa có task. Điều phối viên tạo task khi bạn giao việc.")); return; }
  for (const plan of plans) {
    const box = el("div", "po-plan");
    const h = el("h4");
    h.append(`${KIND_ICON.plan} ${plan.title} `, statusBadge(plan.status));
    box.append(h);
    for (const t of state.tasks.filter((x) => x.parent_id === plan.id)) box.append(taskCard(t));
    host.append(box);
  }
}

function taskCard(t) {
  const card = el("div", "po-task" + (state.openTasks.has(t.id) ? " open" : ""));
  const head = el("div", "head");
  const who = t.kind === "work" ? (t.assignee_id ? nameOf(t.assignee_id) : "chưa giao") : t.participants.map(nameOf).join(" ↔ ");
  head.append(el("span", null, KIND_ICON[t.kind] || "•"), el("span", "title", t.title), el("span", "po-muted", who), statusBadge(t.status));
  head.onclick = () => { state.openTasks.has(t.id) ? state.openTasks.delete(t.id) : state.openTasks.add(t.id); card.classList.toggle("open"); };
  const body = el("div", "body");

  const iv = pendingIv(t.id);
  if (iv) {
    const line = el("div", "po-iv");
    line.append(statusBadge("requested", `⏳ yêu cầu ${IV_ACTION[iv.action]} — chờ xác nhận`));
    head.append(el("span", "po-badge st-requested", "⏳"));
    body.append(line);
  }
  const last = [...state.interventions].reverse().find((i) => i.task_id === t.id && i.status !== "requested");
  if (last) body.append(el("div", "po-iv po-muted", `Can thiệp gần nhất: ${IV_ACTION[last.action]} — ${IV_STATUS[last.status]}${last.outcome ? ` (${last.outcome})` : ""}`));

  body.append(el("pre", null, t.description || "(không có mô tả)"));
  if (t.kind === "work" && t.required_capabilities.length) body.append(el("div", "po-muted", "Cần: " + t.required_capabilities.join(", ")));
  if (t.risk_actions.length) body.append(el("div", "po-muted", "⚠ Rủi ro cao: " + t.risk_actions.map((r) => RISK_LABEL[r] || r).join(", ")));
  for (const a of state.approvals.filter((x) => x.task_id === t.id)) {
    const line = el("div", "po-iv");
    line.append(statusBadge(a.status === "pending" ? "pending-approval" : a.status, `Duyệt: ${APPROVAL_STATUS[a.status] || a.status}`), ` ${a.actions.map((r) => RISK_LABEL[r] || r).join(", ")} · ${nameOf(a.agent_id)}`);
    body.append(line);
  }
  if (t.blocked_reason) body.append(el("div", "po-muted", "Lý do: " + t.blocked_reason));
  if (t.retry_at && t.status === "pending") body.append(el("div", "po-muted", "Thử lại lúc " + fmtTime(t.retry_at)));
  if (t.result_preview) { body.append(el("div", "po-muted", "Kết quả:"), el("pre", null, t.result_preview)); }

  if (t.runs.length) {
    body.append(el("div", "po-muted", "Runs:"));
    for (const r of t.runs) {
      const err = r.error ? ` — ${r.error.code}: ${r.error.message}` : "";
      const cost = r.cost != null ? ` · $${r.cost.toFixed(4)}` : "";
      const job = r.provider_ref ? ` · job ${r.provider_ref}` : "";
      body.append(el("div", null, `#${r.attempt} ${nameOf(r.agent_id)} · ${r.status}${job}${cost}${err}`));
      if (r.provider_ref && r.progress != null && !r.finished_at) {
        const bar = el("div", "po-progress");
        bar.append(Object.assign(el("span"), { style: `width:${Math.round(r.progress * 100)}%` }));
        body.append(bar);
      }
    }
  }
  const arts = state.artifacts.filter((a) => a.task_id === t.id);
  if (arts.length) {
    body.append(el("div", "po-muted", "Artifacts:"));
    for (const a of arts) {
      const row = el("div", "po-link", `[${a.type}] ${a.name} v${a.version}`);
      row.onclick = () => { switchTab("artifacts"); openArtifact(a.id); };
      body.append(row);
    }
  }

  const actions = el("div", "actions");
  const btn = (label, fn, cls = "") => { const b = el("button", "po-btn small " + cls, label); b.type = "button"; b.disabled = !!iv; b.onclick = fn; actions.append(b); };
  if (["pending", "waiting", "awaiting_approval", "active"].includes(t.status)) btn("⏸ Tạm dừng", () => intervene("pause", t.id));
  if (t.status === "paused") btn("▶ Tiếp tục", () => intervene("resume", t.id));
  if (["pending", "waiting", "awaiting_approval", "active", "paused"].includes(t.status)) {
    if (t.kind === "work") btn("↪ Chuyển hướng", () => { const s = prompt("Chỉ dẫn mới cho task:"); if (s) intervene("redirect", t.id, s); });
    btn("✕ Hủy", () => { if (confirm(`Hủy task “${t.title}”?`)) intervene("cancel", t.id); }, "danger");
  }
  const thread = state.convs.find((c) => c.task_id === t.id);
  if (thread) { const b = el("button", "po-btn small ghost", "Xem hội thoại"); b.type = "button"; b.onclick = () => { switchTab("talks"); loadTalk(thread.id); }; actions.append(b); }
  body.append(actions);
  card.append(head, body);
  return card;
}

async function intervene(action, taskId, instructions) {
  try { await api("POST", P("/interventions"), { action, task_id: taskId, instructions: instructions || null }); }
  catch (e) { alert(e.message); }
  markDirty("interventions", "tasks");
}

// ---------------------------------------------------------------- approvals (BR-07)

function renderApprovals() {
  const pending = state.approvals.filter((a) => a.status === "pending");
  const pill = $("approvalPill");
  pill.hidden = !pending.length;
  pill.textContent = `⚠ ${pending.length} chờ duyệt`;
  const box = $("approvalBox");
  box.replaceChildren();
  for (const a of pending) {
    const task = a.task_id && taskById(a.task_id);
    const card = el("div", "po-approval");
    card.append(
      el("h4", null, `Cần duyệt: ${a.actions.map((r) => RISK_LABEL[r] || r).join(", ")}`),
      el("div", null, a.summary || (task ? task.title : "")),
      el("div", "po-muted", `Agent: ${nameOf(a.agent_id)} · hết hạn lúc ${fmtTime(a.expires_at)}`),
    );
    const actions = el("div", "actions");
    const approve = el("button", "po-btn small primary", "✔ Duyệt");
    const reject = el("button", "po-btn small danger", "✕ Từ chối");
    for (const [b, decision] of [[approve, "approve"], [reject, "reject"]]) {
      b.type = "button";
      b.onclick = async () => {
        if (decision === "approve" && !confirm(`Cho phép ${nameOf(a.agent_id)} thực hiện: ${a.actions.map((r) => RISK_LABEL[r] || r).join(", ")}?`)) return;
        approve.disabled = reject.disabled = true;
        try { await api("POST", P(`/approvals/${a.id}/decision`), { decision }); }
        catch (e) { alert(e.message); approve.disabled = reject.disabled = false; }
        markDirty("approvals", "tasks");
      };
    }
    actions.append(approve, reject);
    card.append(actions);
    box.append(card);
  }
}
$("approvalPill").onclick = () => switchTab("chat");

// ---------------------------------------------------------------- artifacts

const fmtSize = (n) => (n == null ? "?" : n < 1024 ? `${n} B` : n < 1048576 ? `${(n / 1024).toFixed(1)} KB` : `${(n / 1048576).toFixed(1)} MB`);
const TEXT_TYPES = new Set(["code", "document", "log"]);
const TEXT_PREVIEW_MAX = 200_000;

function renderArtifacts() {
  const host = $("artifactList");
  host.replaceChildren();
  if (!state.artifacts.length) { host.append(el("p", "po-empty", "Chưa có artifact. File do worker tạo trong workspace của task sẽ xuất hiện ở đây.")); return; }
  for (const a of [...state.artifacts].reverse()) {
    const task = a.task_id && taskById(a.task_id);
    const item = el("div", "po-list-item" + (state.artifact === a.id ? " selected" : ""));
    item.append(
      el("div", null, `[${a.type}] ${a.name} · v${a.version}`),
      el("div", "po-muted", `${nameOf(a.agent_id)} · ${task ? `“${task.title}”` : "—"} · ${fmtSize(a.size_bytes)} · ${fmtTime(a.created_at)}`),
    );
    item.onclick = () => openArtifact(a.id);
    host.append(item);
  }
}

async function artifactBlob(a) {
  const res = await fetch(P(`/artifacts/${a.id}/content`), { headers: { Authorization: `Bearer ${state.token}` } });
  if (!res.ok) {
    const data = await res.json().catch(() => ({}));
    throw new Error(data.error?.message || res.statusText);
  }
  return res.blob();
}

async function openArtifact(id) {
  state.artifact = id;
  renderArtifacts();
  const a = state.artifacts.find((x) => x.id === id);
  const view = $("artifactView");
  view.className = "po-art-view";
  view.replaceChildren(el("p", "po-muted", "Đang tải…"));
  if (!a) return;
  let blob;
  try { blob = await artifactBlob(a); }
  catch (e) { view.replaceChildren(el("p", "po-error", `Không đọc được artifact: ${e.message}`)); return; }
  if (state.artifact !== id) return;
  const task = a.task_id && taskById(a.task_id);
  view.replaceChildren(
    el("h4", null, a.name),
    el("div", "po-muted", `Nguồn: ${nameOf(a.agent_id)} · task ${task ? `“${task.title}”` : "—"} · run ${a.run_id || "—"}`),
    el("div", "po-muted", `${a.mime_type || "?"} · ${fmtSize(a.size_bytes)} · ${a.checksum || ""}`),
  );
  // Agent output is untrusted: only media types the browser renders inertly get an
  // object URL; everything else is shown as text (never as HTML) or downloaded.
  if (state.artifactUrl) URL.revokeObjectURL(state.artifactUrl);
  const url = (state.artifactUrl = URL.createObjectURL(blob));
  if (a.type === "audio" && /^audio\//.test(a.mime_type || "")) {
    view.append(Object.assign(document.createElement("audio"), { controls: true, src: url }));
  } else if (a.type === "image" && /^image\/(png|jpeg|gif|webp)$/.test(a.mime_type || "")) {
    view.append(Object.assign(document.createElement("img"), { src: url, alt: a.name }));
  } else if (a.type === "3d" && a.name.toLowerCase().endsWith(".obj")) {
    const text = await blob.text();
    const canvas = document.createElement("canvas");
    view.append(canvas);
    drawObj(canvas, text, id);
  } else if (TEXT_TYPES.has(a.type) && blob.size <= TEXT_PREVIEW_MAX) {
    view.append(el("pre", "po-pre", await blob.text()));
  } else {
    view.append(el("p", "po-muted", "Không có bản xem trước cho loại file này."));
  }
  const dl = Object.assign(el("a", "po-btn small", "⬇ Tải về"), { href: url, download: a.name });
  view.append(dl);
}

// Minimal OBJ wireframe viewer (vertices + faces), slowly rotating.
function drawObj(canvas, text, artifactId) {
  const verts = [], edges = [];
  for (const line of text.split("\n")) {
    const p = line.trim().split(/\s+/);
    if (p[0] === "v") verts.push(p.slice(1, 4).map(Number));
    if (p[0] === "f") {
      const idx = p.slice(1).map((x) => parseInt(x, 10) - 1);
      idx.forEach((v, i) => edges.push([v, idx[(i + 1) % idx.length]]));
    }
  }
  if (!verts.length) return;
  const center = [0, 1, 2].map((k) => verts.reduce((s, v) => s + v[k], 0) / verts.length);
  const radius = Math.max(...verts.map((v) => Math.hypot(v[0] - center[0], v[1] - center[1], v[2] - center[2]))) || 1;
  const ctx = canvas.getContext("2d");
  const t0 = performance.now();
  const frame = () => {
    if (!canvas.isConnected || state.artifact !== artifactId) return;
    const w = (canvas.width = canvas.clientWidth), h = (canvas.height = canvas.clientHeight);
    const a = (performance.now() - t0) / 2000, tilt = 0.45;
    const scale = Math.min(w, h) * 0.38 / radius;
    const proj = verts.map(([x, y, z]) => {
      [x, y, z] = [x - center[0], y - center[1], z - center[2]];
      const x1 = x * Math.cos(a) + z * Math.sin(a), z1 = -x * Math.sin(a) + z * Math.cos(a);
      const y1 = y * Math.cos(tilt) - z1 * Math.sin(tilt);
      return [w / 2 + x1 * scale, h / 2 - y1 * scale];
    });
    ctx.clearRect(0, 0, w, h);
    ctx.strokeStyle = "#89ddff";
    ctx.beginPath();
    for (const [i, j] of edges) {
      if (!proj[i] || !proj[j]) continue;
      ctx.moveTo(...proj[i]);
      ctx.lineTo(...proj[j]);
    }
    ctx.stroke();
    requestAnimationFrame(frame);
  };
  frame();
}

// ---------------------------------------------------------------- metrics / audit

function renderMetrics() {
  const host = $("metricsView");
  const m = state.metrics;
  if (host.contains(document.activeElement) && document.activeElement.tagName === "INPUT") return; // don't wipe an edit
  host.replaceChildren();
  if (!m) return;
  const cards = el("div", "po-cards");
  const stat = (label, value) => { const c = el("div", "po-card-stat"); c.append(el("b", null, String(value)), el("span", "po-muted", label)); cards.append(c); };
  stat("chi phí đã báo (USD)", `$${m.cost_usd.toFixed(4)}`);
  stat("run không báo chi phí", m.runs_without_cost);
  stat("tổng run", m.runs);
  stat("lần thử lại", m.retries);
  stat("run lỗi", m.run_failures);
  stat("artifact", Object.values(m.artifacts_by_type).reduce((a, b) => a + b, 0));
  stat("sự kiện", m.events);
  host.append(cards);
  renderBudget(host, m);

  const kv = (title, obj, labels = {}) => {
    const entries = Object.entries(obj);
    host.append(el("h4", null, title));
    host.append(el("div", "po-muted", entries.length ? entries.map(([k, v]) => `${labels[k] || k}: ${v}`).join(" · ") : "—"));
  };
  kv("Task", m.tasks_by_status, TASK_STATUS);
  kv("Phê duyệt", m.approvals_by_status, APPROVAL_STATUS);
  kv("Can thiệp", m.interventions_by_status, { requested: "đã yêu cầu", applied: "đã áp dụng", rejected: "bị từ chối" });
  kv("Artifact theo loại", m.artifacts_by_type);

  host.append(el("h4", null, "Theo agent"));
  const table = el("table", "po-table");
  const head = el("tr");
  for (const [h, num] of [["Agent", false], ["Run", true], ["OK", true], ["Lỗi", true], ["Thử lại", true], ["Giây", true], ["USD", true]]) head.append(el("th", num ? "num" : null, h));
  table.append(head);
  for (const a of m.agents) {
    const row = el("tr");
    const finished = a.runs - (a.runs_by_status.running || 0) - (a.runs_by_status.queued || 0);
    const cost = !a.cost_usd && a.runs_without_cost >= finished ? "không rõ"
      : `$${a.cost_usd.toFixed(4)}${a.runs_without_cost ? ` (+${a.runs_without_cost} không rõ)` : ""}`;
    row.append(
      el("td", null, `${a.name} · ${a.adapter_type}`),
      el("td", "num", a.runs), el("td", "num", a.runs_by_status.succeeded || 0), el("td", "num", a.runs_by_status.failed || 0),
      el("td", "num", a.retries), el("td", "num", a.busy_seconds), el("td", "num", cost),
    );
    table.append(row);
  }
  host.append(table);
  host.append(el("p", "po-muted", "Chỉ cộng chi phí do provider/adapter báo; run không báo chi phí được đếm riêng, không coi là $0."));
}

function renderBudget(host, m) {
  host.append(el("h4", null, "Ngân sách"));
  if (m.over_budget) {
    host.append(el("div", "po-approval", "⛔ Đã chạm trần ngân sách project: hệ thống tạm dừng dispatch. Nâng trần rồi tiếp tục project."));
  }
  const form = el("form", "po-budget");
  const field = (label, value, id) => {
    const input = el("input");
    Object.assign(input, { type: "number", min: "0", step: "0.01", placeholder: "không giới hạn", value: value ?? "" });
    input.dataset.agent = id || "";
    const row = el("label", "po-row");
    row.append(el("span", null, label), input);
    form.append(row);
  };
  field("Trần project (USD)", m.budget.max_cost_usd);
  for (const a of m.agents) field(`${a.name}`, m.budget.agents[a.agent_id], a.agent_id);
  const save = el("button", "po-btn primary small", "Lưu ngân sách");
  save.type = "submit";
  form.append(save, el("span", "po-muted", " Để trống = không giới hạn. Chi phí chỉ biết khi run kết thúc nên run đang chạy có thể vượt trần."));
  form.onsubmit = async (ev) => {
    ev.preventDefault();
    const body = { max_cost_usd: null, agents: {} };
    for (const input of form.querySelectorAll("input")) {
      if (input.value === "") continue;
      if (input.dataset.agent) body.agents[input.dataset.agent] = Number(input.value);
      else body.max_cost_usd = Number(input.value);
    }
    save.disabled = true;
    try { await api("PUT", P("/budget"), body); } catch (e) { alert(e.message); }
    document.activeElement.blur();
    markDirty("metrics", "project");
  };
  host.append(form);
}

// ---------------------------------------------------------------- meetings & DMs

function renderTalks() {
  const host = $("talkList");
  host.replaceChildren();
  const talks = state.convs.filter((c) => c.type !== "owner_chat").reverse();
  if (!talks.length) { host.append(el("p", "po-empty", "Chưa có cuộc họp, DM hay task thread nào.")); return; }
  for (const c of talks) {
    const meeting = state.meetings.find((m) => m.conversation_id === c.id);
    const task = c.task_id && taskById(c.task_id);
    const item = el("div", "po-list-item" + (state.talk === c.id ? " selected" : ""));
    const icon = c.type === "group_meeting" ? KIND_ICON.meeting : c.type === "dm" ? KIND_ICON.dm : KIND_ICON.work;
    const title = el("div");
    title.append(`${icon} ${task ? task.title : c.type} `);
    if (meeting) title.append(meeting.live ? el("span", "po-live", "● LIVE") : el("span", "po-muted", `đã kết thúc (${meeting.outcome || meeting.status})`));
    else if (c.type === "dm") title.append(el("span", "po-muted", c.status === "open" ? "đang diễn ra" : "đã kết thúc"));
    item.append(title, el("div", "po-muted", c.participants.map(nameOf).join(", ")));
    item.onclick = () => loadTalk(c.id);
    host.append(item);
  }
}

async function loadTalk(convId) {
  state.talk = convId;
  const c = state.convs.find((x) => x.id === convId);
  const view = $("talkView");
  let msgs, hidden = false;
  try { msgs = await api("GET", P(`/conversations/${convId}/messages`)); } catch (e) {
    if (e.code !== "transcript_private") throw e;
    msgs = []; hidden = true;
  }
  if (state.talk !== convId) return;
  view.replaceChildren();
  const meeting = state.meetings.find((m) => m.conversation_id === convId);
  if (meeting) {
    view.append(el("h4", null, `${KIND_ICON.meeting} ${meeting.title || "Cuộc họp"} — chủ trì: ${nameOf(meeting.moderator_id)}`));
    view.append(el("div", "po-muted", "Chương trình:"), el("pre", "po-pre", meeting.agenda));
    if (meeting.decisions.length) { view.append(el("div", "po-muted", "Quyết định:")); for (const d of meeting.decisions) view.append(el("div", null, "✔ " + d)); }
  } else if (c) view.append(el("h4", null, c.type === "dm" ? `${KIND_ICON.dm} DM: ${c.participants.map(nameOf).join(" ↔ ")}` : `${KIND_ICON.work} Task thread`));
  if (c?.visibility === "participants") view.append(el("div", "po-muted", hidden ? "Transcript riêng tư: vai trò của bạn không được đọc (policy transcripts)." : "Riêng tư giữa các bên tham gia; bạn xem với quyền audit."));
  const log = el("div", "po-log");
  if (!msgs.length && !hidden) log.append(el("p", "po-empty", "Chưa có tin nhắn."));
  for (const m of msgs) {
    const box = el("div", "po-msg");
    box.append(el("span", "who", `${nameOf(m.sender_id)} · ${fmtTime(m.created_at)}`), document.createTextNode(m.content ?? "(nội dung đã hết hạn lưu trữ)"));
    log.append(box);
  }
  view.append(log);
  renderTalks();
}

// ---------------------------------------------------------------- timeline

const TL_TYPES = Object.keys(EVENT_LABEL);
$("tlFilter").replaceChildren(
  Object.assign(el("option", null, "Tất cả sự kiện"), { value: "" }),
  ...TL_TYPES.map((t) => Object.assign(el("option", null, EVENT_LABEL[t]), { value: t })),
);
$("tlFilter").onchange = (ev) => { state.filter = ev.target.value; renderTimeline(); };

function eventDetail(e) {
  const p = e.payload || {};
  const task = e.task_id && taskById(e.task_id);
  const parts = [];
  if (task) parts.push(`“${task.title}”`);
  if (p.preview) parts.push(p.preview);
  if (p.title && !task) parts.push(p.title);
  if (p.reason) parts.push("lý do: " + p.reason);
  if (p.decision) parts.push(p.decision);
  if (p.error) parts.push(`${p.error.code}${p.will_retry ? " (sẽ thử lại)" : ""}`);
  if (p.action) parts.push(`${IV_ACTION[p.action] || p.action}${p.status ? " → " + (IV_STATUS[p.status] || p.status) : ""}`);
  if (p.outcome && !p.action) parts.push(p.outcome);
  if (p.uri) parts.push(p.uri.split("/").pop());
  if (p.name) parts.push(p.name);
  return parts.join(" · ");
}

function timelineItem(e) {
  const li = el("li");
  li.append(el("span", "t", fmtTime(e.occurred_at)));
  const body = el("span");
  body.append(el("span", "ty", EVENT_LABEL[e.type] || e.type), ` ${nameOf(e.actor_id)} `, el("span", "po-muted", eventDetail(e)));
  li.append(body);
  return li;
}

function renderTimeline() {
  const list = $("timeline");
  const shown = state.events.filter((e) => !state.filter || e.type === state.filter).slice(-400).reverse();
  list.replaceChildren(...shown.map(timelineItem));
  $("tlCount").textContent = `${state.events.length} sự kiện · seq ${state.lastSeq}`;
}

function appendTimeline(e) {
  $("tlCount").textContent = `${state.events.length} sự kiện · seq ${state.lastSeq}`;
  if (state.filter && e.type !== state.filter) return;
  $("timeline").prepend(timelineItem(e));
}

// ---------------------------------------------------------------- agents

function renderAgents() {
  const host = $("agentList");
  host.replaceChildren();
  if (!state.agents.length) host.append(el("p", "po-empty", "Chưa có agent."));
  for (const a of state.agents) {
    const item = el("div", "po-list-item po-agent");
    item.append(el("span", "po-dot " + agentStatus(a)));
    const info = el("div");
    info.append(el("div", null, `${a.name}${a.is_active_orchestrator ? " ★ điều phối" : ""}`), el("div", "po-muted", `${a.adapter_type} · ${a.role} · ${a.capabilities.join(", ")}`));
    item.append(info);
    item.onclick = () => selectAgent(a.id);
    host.append(item);
  }
}

async function loadAdapters() {
  state.adapters = await api("GET", "/adapters");
  const sel = $("agAdapter");
  sel.replaceChildren(...state.adapters.map((a) => Object.assign(el("option", null, a.adapter_type), { value: a.adapter_type })));
  sel.onchange = () => {
    const a = state.adapters.find((x) => x.adapter_type === sel.value);
    $("agCaps").value = a ? a.default_manifest.capabilities.join(", ") : "";
  };
  sel.onchange();
}

$("agentForm").onsubmit = async (ev) => {
  ev.preventDefault();
  $("agentError").hidden = true;
  try {
    const config = JSON.parse($("agConfig").value || "{}");
    const caps = $("agCaps").value.split(",").map((s) => s.trim()).filter(Boolean);
    if (caps.length) config.capabilities = caps;
    await api("POST", P("/agents"), { name: $("agName").value.trim(), adapter_type: $("agAdapter").value, role: $("agRole").value, config });
    $("agName").value = "";
    markDirty("agents");
  } catch (e) {
    $("agentError").hidden = false;
    $("agentError").textContent = e instanceof SyntaxError ? "Config không phải JSON hợp lệ" : e.message;
  }
};

function selectAgent(id) {
  state.selectedAgent = id;
  office.setSelected(id);
  $("agentTabBtn").hidden = false;
  switchTab("agent");
  renderAgentDetail();
  renderRoster();
}

function renderAgentDetail() {
  const host = $("agentDetail");
  const a = state.selectedAgent && agentById(state.selectedAgent);
  host.replaceChildren();
  if (!a) return;
  host.append(el("h3", null, a.name), el("div", "po-muted", `${a.adapter_type} · ${a.provider} · ${a.role}${a.is_active_orchestrator ? " (điều phối hiện tại)" : ""}`));
  host.append(el("div", "po-muted", "Capabilities: " + a.capabilities.join(", ")));

  host.append(el("h4", null, "Đang làm"));
  if (!a.current_runs.length) host.append(el("p", "po-empty", "Không có run nào đang chạy."));
  for (const r of a.current_runs) {
    const t = r.task_id && taskById(r.task_id);
    host.append(el("div", null, `${r.purpose} · ${r.status}${t ? ` · “${t.title}”` : ""}`));
  }

  host.append(el("h4", null, "Task liên quan"));
  const mine = state.tasks.filter((t) => t.assignee_id === a.id || t.participants.includes(a.id) || t.runs.some((r) => r.agent_id === a.id));
  if (!mine.length) host.append(el("p", "po-empty", "Chưa có task nào."));
  for (const t of mine.slice(-15).reverse()) {
    const row = el("div", "po-list-item");
    row.append(statusBadge(t.status), ` ${KIND_ICON[t.kind] || ""} ${t.title}`);
    row.onclick = () => { state.openTasks.add(t.id); switchTab("tasks"); renderTasks(); };
    host.append(row);
  }

  host.append(el("h4", null, "Tin nhắn gần đây (từ event log)"));
  const said = state.events.filter((e) => e.type === "message.sent" && e.actor_id === a.id).slice(-10).reverse();
  if (!said.length) host.append(el("p", "po-empty", "Chưa có tin nhắn được ghi nhận."));
  for (const e of said) {
    const conv = state.convs.find((c) => c.id === e.conversation_id);
    const row = el("div", "po-msg");
    row.append(el("span", "who", `${fmtTime(e.occurred_at)} · ${conv ? conv.type : ""}`), document.createTextNode(e.payload.preview || ""));
    if (conv) { row.style.cursor = "pointer"; row.onclick = () => { if (conv.type === "owner_chat") switchTab("chat"); else { switchTab("talks"); loadTalk(conv.id); } }; }
    host.append(row);
  }
}

// ---------------------------------------------------------------- tabs & auth

function switchTab(name) {
  if (name === "metrics" && state.pid) markDirty("metrics");
  document.querySelectorAll("#tabs button").forEach((b) => b.classList.toggle("active", b.dataset.tab === name));
  document.querySelectorAll(".po-tab").forEach((s) => s.classList.toggle("active", s.id === "tab-" + name));
}
document.querySelectorAll("#tabs button").forEach((b) => (b.onclick = () => switchTab(b.dataset.tab)));

function localStorageGet(k) { try { return localStorage.getItem(k); } catch { return null; } }
function localStorageSet(k, v) { try { localStorage.setItem(k, v); } catch {} }

function logout() {
  closeStream();
  state.token = null;
  try { localStorage.removeItem("po.token"); } catch {}
  $("login").hidden = false;
}
$("logout").onclick = logout;

$("loginForm").onsubmit = async (ev) => {
  ev.preventDefault();
  state.token = $("tokenInput").value.trim();
  $("loginError").hidden = true;
  try {
    await start();
    localStorageSet("po.token", state.token);
    $("login").hidden = true;
  } catch (e) {
    $("loginError").hidden = false;
    $("loginError").textContent = "Token không hợp lệ hoặc server không phản hồi.";
  }
};

async function start() {
  await loadAdapters();
  await loadProjects();
}

(async () => {
  state.token = localStorageGet("po.token");
  if (!state.token) { $("login").hidden = false; return; }
  try { await start(); } catch { logout(); }
})();
