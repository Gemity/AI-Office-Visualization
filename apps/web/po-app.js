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
  waiting: "chờ phụ thuộc", pending: "sẵn sàng", active: "đang chạy", paused: "tạm dừng",
  blocked: "bị chặn", completed: "hoàn thành", failed: "thất bại", cancelled: "đã hủy",
};
const KIND_ICON = { work: "🛠", meeting: "👥", dm: "💬", plan: "📋" };
const IV_ACTION = { pause: "tạm dừng", resume: "tiếp tục", cancel: "hủy", redirect: "chuyển hướng" };
const IV_STATUS = { requested: "đã yêu cầu — chờ xác nhận", applied: "đã áp dụng", rejected: "bị từ chối" };
const EVENT_LABEL = {
  "project.created": "Tạo project", "agent.assigned": "Gán agent", "task.created": "Tạo task",
  "task.started": "Bắt đầu task", "task.blocked": "Task bị chặn", "task.completed": "Hoàn thành",
  "message.sent": "Tin nhắn", "meeting.started": "Bắt đầu họp", "meeting.ended": "Kết thúc họp",
  "decision.recorded": "Quyết định", "artifact.created": "Artifact", "approval.requested": "Xin duyệt",
  "approval.resolved": "Đã duyệt", "run.failed": "Run lỗi", "intervention.requested": "Yêu cầu can thiệp",
  "intervention.applied": "Can thiệp xử lý",
};
const ADAPTER_COLOR = { "claude-agent-sdk": "#d97757", "codex-cli": "#10a37f", mock: "#7aa2f7" };
const ERROR_SHOW_MS = 8000;
const TIMELINE_MAX = 1500;

const state = {
  token: null, projects: [], pid: null, project: null,
  agents: [], tasks: [], convs: [], meetings: [], interventions: [], artifacts: [], adapters: [],
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
    const label = run ? (run.purpose === "orchestrator_turn" ? "đang trả lời" : run.purpose === "conversation_turn" ? "đang phát biểu" : "đang làm task") : st === "error" ? "lỗi" : "rảnh";
    chip.append(el("span", "rs " + st, label));
    chip.onclick = () => selectAgent(a.id);
    host.append(chip);
  }
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
  });
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
  },
  tasks: async () => { state.tasks = await api("GET", P("/tasks")); renderTasks(); renderAgentDetail(); },
  convs: async () => { state.convs = await api("GET", P("/conversations")); await loadChat(); renderTalks(); },
  meetings: async () => { state.meetings = await api("GET", P("/meetings")); syncOffice(); renderTalks(); },
  interventions: async () => { state.interventions = await api("GET", P("/interventions")); renderTasks(); },
  artifacts: async () => { state.artifacts = await api("GET", P("/artifacts")); renderTasks(); },
};

async function loadAll() {
  await LOADERS.agents(); // names first: everything else renders agent names
  await Promise.all(["project", "tasks", "convs", "meetings", "interventions", "artifacts"].map((k) => LOADERS[k]()));
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
}

// ---------------------------------------------------------------- top bar

function renderProjectBar() {
  const btn = $("projectPause");
  btn.hidden = !state.project;
  if (!state.project) return;
  const paused = state.project.status === "paused";
  btn.textContent = paused ? "▶ Tiếp tục project" : "⏸ Tạm dừng project";
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
  if (t.blocked_reason) body.append(el("div", "po-muted", "Lý do: " + t.blocked_reason));
  if (t.retry_at && t.status === "pending") body.append(el("div", "po-muted", "Thử lại lúc " + fmtTime(t.retry_at)));
  if (t.result_preview) { body.append(el("div", "po-muted", "Kết quả:"), el("pre", null, t.result_preview)); }

  if (t.runs.length) {
    body.append(el("div", "po-muted", "Runs:"));
    for (const r of t.runs) {
      const err = r.error ? ` — ${r.error.code}: ${r.error.message}` : "";
      const cost = r.cost != null ? ` · $${r.cost.toFixed(4)}` : "";
      body.append(el("div", null, `#${r.attempt} ${nameOf(r.agent_id)} · ${r.status}${cost}${err}`));
    }
  }
  const arts = state.artifacts.filter((a) => a.task_id === t.id);
  if (arts.length) {
    body.append(el("div", "po-muted", "Artifacts:"));
    for (const a of arts) body.append(el("div", null, `[${a.type}] ${decodeURI(a.uri.replace(/^file:\/\//, ""))}`));
  }

  const actions = el("div", "actions");
  const btn = (label, fn, cls = "") => { const b = el("button", "po-btn small " + cls, label); b.type = "button"; b.disabled = !!iv; b.onclick = fn; actions.append(b); };
  if (["pending", "waiting", "active"].includes(t.status)) btn("⏸ Tạm dừng", () => intervene("pause", t.id));
  if (t.status === "paused") btn("▶ Tiếp tục", () => intervene("resume", t.id));
  if (["pending", "waiting", "active", "paused"].includes(t.status)) {
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
  const msgs = await api("GET", P(`/conversations/${convId}/messages`));
  if (state.talk !== convId) return;
  view.replaceChildren();
  const meeting = state.meetings.find((m) => m.conversation_id === convId);
  if (meeting) {
    view.append(el("h4", null, `${KIND_ICON.meeting} ${meeting.title || "Cuộc họp"} — chủ trì: ${nameOf(meeting.moderator_id)}`));
    view.append(el("div", "po-muted", "Chương trình:"), el("pre", "po-pre", meeting.agenda));
    if (meeting.decisions.length) { view.append(el("div", "po-muted", "Quyết định:")); for (const d of meeting.decisions) view.append(el("div", null, "✔ " + d)); }
  } else if (c) view.append(el("h4", null, c.type === "dm" ? `${KIND_ICON.dm} DM: ${c.participants.map(nameOf).join(" ↔ ")}` : `${KIND_ICON.work} Task thread`));
  if (c?.visibility === "participants") view.append(el("div", "po-muted", "Riêng tư giữa các bên tham gia; Owner xem với quyền audit."));
  const log = el("div", "po-log");
  if (!msgs.length) log.append(el("p", "po-empty", "Chưa có tin nhắn."));
  for (const m of msgs) {
    const box = el("div", "po-msg");
    box.append(el("span", "who", `${nameOf(m.sender_id)} · ${fmtTime(m.created_at)}`), document.createTextNode(m.content ?? ""));
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
