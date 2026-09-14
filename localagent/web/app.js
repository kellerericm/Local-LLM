"use strict";

// ---------- helpers ----------
const $ = (sel, el = document) => el.querySelector(sel);

function h(tag, attrs = {}, ...children) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === undefined || v === null || v === false) continue;
    if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else if (k === "class") el.className = v;
    else if (k === "html") el.innerHTML = v;
    else el.setAttribute(k, v === true ? "" : v);
  }
  for (const c of children.flat()) {
    if (c === null || c === undefined || c === false) continue;
    el.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return el;
}

async function api(method, url, body) {
  const res = await fetch(url, {
    method,
    headers: body !== undefined ? { "Content-Type": "application/json" } : {},
    body: body !== undefined ? JSON.stringify(body) : undefined,
  });
  if (!res.ok) {
    let msg = res.statusText;
    try { const j = await res.json(); msg = typeof j.detail === "string" ? j.detail : JSON.stringify(j.detail); } catch {}
    throw new Error(msg);
  }
  return res.json();
}

function toast(text, ms = 3500) {
  const t = $("#toast");
  t.textContent = text;
  t.hidden = false;
  clearTimeout(toast._t);
  toast._t = setTimeout(() => (t.hidden = true), ms);
}

const escapeHtml = (s) => s.replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

// Minimal, safe markdown: code fences, inline code, bold, headings.
function renderMarkdown(src) {
  const parts = (src || "").split(/```/);
  return parts.map((part, i) => {
    if (i % 2 === 1) {
      const nl = part.indexOf("\n");
      const code = nl >= 0 && /^[\w+-]*$/.test(part.slice(0, nl).trim()) ? part.slice(nl + 1) : part;
      return `<pre><code>${escapeHtml(code.replace(/\n$/, ""))}</code></pre>`;
    }
    return escapeHtml(part)
      .replace(/`([^`\n]+)`/g, "<code>$1</code>")
      .replace(/\*\*([^*\n]+)\*\*/g, "<strong>$1</strong>")
      .replace(/^#{1,6} (.+)$/gm, "<h4>$1</h4>");
  }).join("");
}

const store = {
  get(k, d) { try { const v = localStorage.getItem(k); return v === null ? d : JSON.parse(v); } catch { return d; } },
  set(k, v) { try { localStorage.setItem(k, JSON.stringify(v)); } catch {} },
};

// ---------- state ----------
const S = {
  projects: [],
  chats: [],
  toolsets: [],
  running: new Set(),
  chatState: {},          // chat_id -> {state, outcome}
  approvals: new Map(),   // id -> approval
  currentChatId: store.get("currentChatId", null),
  messages: [],
  tasks: [],
  collapsed: store.get("collapsedProjects", {}),
  streamEl: null,
  streamText: "",
};

const STATE_LABELS = {
  running: "Working", generating: "Thinking", queued: "Waiting for model", loading_model: "Loading model…",
  paused_gpu_busy: "Paused: GPU busy",
};
const OUTCOME_LABELS = {
  done: null, waiting_user: "Waiting for your reply", cancelled: "Stopped", needs_help: "Needs your help",
  step_limit: "Step limit reached", error: "Error",
};

// ---------- sidebar ----------
function chatItem(c) {
  const st = S.chatState[c.id];
  const running = S.running.has(c.id);
  const needsAttention = [...S.approvals.values()].some((a) => a.chat_id === c.id);
  return h("li", {
    class: "chat-item" + (c.id === S.currentChatId ? " active" : ""),
    title: c.title,
    onclick: () => selectChat(c.id),
  },
  needsAttention ? h("span", { class: "dot attn", title: "Waiting for approval" }) : running ? h("span", { class: "dot", title: STATE_LABELS[st?.state] || "Running" }) : null,
  h("span", { class: "name" }, c.title));
}

function renderSidebar() {
  const general = $("#general-chats");
  general.replaceChildren(...S.chats.filter((c) => !c.project_id).map(chatItem));
  if (!general.children.length) general.append(h("li", { class: "muted", style: "padding:4px 8px;font-size:12px" }, "No chats yet"));

  const wrap = $("#projects");
  wrap.replaceChildren(...S.projects.map((p) => {
    const chats = S.chats.filter((c) => c.project_id === p.id);
    const collapsed = !!S.collapsed[p.id];
    return h("div", { class: "project" },
      h("div", { class: "project-head", title: p.workspace_path, onclick: () => { S.collapsed[p.id] = !collapsed; store.set("collapsedProjects", S.collapsed); renderSidebar(); } },
        h("span", { class: "caret" }, collapsed ? "▸" : "▾"),
        h("span", { class: "name" }, p.name),
        h("button", { class: "icon-btn", title: "New chat in project", onclick: (e) => { e.stopPropagation(); newChat(p.id); } }, "＋"),
        h("button", { class: "icon-btn", title: "Project settings", onclick: (e) => { e.stopPropagation(); projectDialog(p); } }, "⋯")),
      collapsed ? null : chats.length ? h("ul", { class: "chat-list" }, chats.map(chatItem)) : h("div", { class: "empty-note" }, "No chats"));
  }));
  if (!S.projects.length) wrap.append(h("div", { class: "muted", style: "padding:4px 8px;font-size:12px" }, "No projects yet"));
}

async function refreshState() {
  const st = await api("GET", "/api/state");
  S.projects = st.projects;
  S.chats = st.chats;
  S.toolsets = st.toolsets;
  S.running = new Set(st.running);
  S.approvals = new Map(st.pending_approvals.map((a) => [a.id, a]));
  if (S.currentChatId && !S.chats.some((c) => c.id === S.currentChatId)) {
    S.currentChatId = null;
    store.set("currentChatId", null);
    renderChat();
  }
  renderSidebar();
  renderHeader();
  showNextApproval();
}

// ---------- chat view ----------
async function selectChat(id) {
  S.currentChatId = id;
  store.set("currentChatId", id);
  removeStream();
  const data = await api("GET", `/api/chats/${id}`);
  S.messages = data.messages;
  S.tasks = data.tasks;
  if (data.running) S.running.add(id); else S.running.delete(id);
  renderSidebar();
  renderChat();
  scrollToBottom(true);
  $("#input").focus();
}

function currentChat() { return S.chats.find((c) => c.id === S.currentChatId); }

function renderHeader() {
  const chat = currentChat();
  $("#chat-header").hidden = !chat;
  if (!chat) { $("#send").hidden = false; $("#stop").hidden = true; return; }
  $("#chat-title").textContent = chat.title;
  const overrides = chat.gen_overrides || {};
  const custom = Object.keys(overrides).length > 0;
  const genBtn = $("#chat-gen");
  genBtn.classList.toggle("custom", custom);
  $(".label", genBtn).textContent = custom ? presetLabel(overrides.preset, S.profile) : "Default";
  genBtn.title = custom ? "This chat has its own model settings" : "Model settings for this chat (using app defaults)";
  renderContextMeter();
  const project = S.projects.find((p) => p.id === chat.project_id);
  $("#chat-project").textContent = project ? project.name : "General";
  const pill = $("#run-state");
  const st = S.chatState[chat.id];
  const running = S.running.has(chat.id);
  let label = null, cls = "pill";
  if (running) {
    label = STATE_LABELS[st?.state] || "Working";
    if (st?.state === "paused_gpu_busy") cls += " warn";
  } else if (st?.outcome) {
    label = OUTCOME_LABELS[st.outcome];
    if (st.outcome === "error") cls += " bad";
    else if (["needs_help", "waiting_user", "step_limit"].includes(st.outcome)) cls += " warn";
  }
  if ([...S.approvals.values()].some((a) => a.chat_id === chat.id)) { label = "Waiting for approval"; cls = "pill warn"; }
  pill.hidden = !label;
  pill.textContent = label || "";
  pill.className = cls;
  $("#send").hidden = running;
  $("#stop").hidden = !running;
}

function renderContextMeter(usage = latestUsage()) {
  const meter = $("#context-meter");
  if (!usage || !usage.context_tokens) { meter.hidden = true; return; }
  const used = (usage.prompt_tokens || 0) + (usage.completion_tokens || 0);
  const pct = Math.min(100, Math.round((used / usage.context_tokens) * 100));
  meter.hidden = false;
  $(".fill", meter).style.width = `${pct}%`;
  $(".label", meter).textContent = `${fmtTokens(used)} / ${fmtTokens(usage.context_tokens)}`;
  meter.classList.toggle("high", pct >= 70 && pct < 90);
  meter.classList.toggle("full", pct >= 90);
  meter.title = `Context used on the last reply: ${used} of ${usage.context_tokens} tokens (${pct}%). ` +
    "When it fills up, older tool output is shortened and the oldest turns are dropped.";
}

function renderChat() {
  const chat = currentChat();
  $("#empty").hidden = !!chat;
  $("#messages").hidden = !chat;
  $("#composer").hidden = false;     // on the landing page, sending starts a new chat
  renderHeader();
  renderTasks();
  if (!chat) return;
  const box = $("#messages");
  box.replaceChildren();
  const results = new Map(S.messages.filter((m) => m.role === "tool").map((m) => [m.tool_call_id, m]));
  for (const m of S.messages) {
    const el = renderMessage(m, results);
    if (el) box.append(el);
  }
}

function argSummary(name, args) {
  if (!args || typeof args !== "object") return "";
  const pick = args.command ?? args.path ?? args.pattern ?? args.name ?? args.question ?? (args.code ? args.code.split("\n")[0] : null);
  if (name === "update_tasks" && Array.isArray(args.tasks)) return `${args.tasks.length} tasks`;
  return pick != null ? String(pick) : JSON.stringify(args);
}

function toolCard(call, result) {
  if (call.name === "ask_user") {
    return h("div", { class: "ask", "data-call": call.id }, h("strong", {}, "Question for you"), call.arguments?.question || "");
  }
  const state = result ? (result.ok ? ["ok", "✓"] : ["fail", "✗"]) : ["run", "…"];
  const details = h("details", { class: "tool", "data-call": call.id },
    h("summary", {},
      h("span", { class: "tname" }, call.name),
      h("span", { class: "targs" }, argSummary(call.name, call.arguments)),
      h("span", { class: `tstate ${state[0]}` }, state[1])),
    h("pre", { class: "args" }, JSON.stringify(call.arguments, null, 2)),
    result ? h("pre", { class: "result" }, result.content || "") : null);
  if (result && !result.ok) details.open = false;
  return details;
}

function renderMessage(m, results) {
  if (m.role === "tool") {
    // Normally shown inside its tool card; show standalone only if the call is missing.
    const hasCall = S.messages.some((x) => x.tool_calls?.some((c) => c.id === m.tool_call_id));
    return hasCall ? null : h("div", { class: "msg coordinator" }, `${m.name}: ${m.content}`);
  }
  if (m.kind === "error") return h("div", { class: "msg error" }, m.content);
  if (m.kind === "coordinator") return h("div", { class: "msg coordinator" }, m.content);
  if (m.role === "user") return h("div", { class: "msg user" }, h("div", { class: "bubble" }, m.content));
  if (m.role === "assistant") {
    const el = h("div", { class: "msg assistant", "data-id": m.id });
    if (m.reasoning) el.append(h("details", { class: "reasoning" }, h("summary", {}, "Reasoning"), h("div", {}, m.reasoning)));
    if (m.content) el.append(h("div", { class: "text", html: renderMarkdown(m.content) }));
    for (const c of m.tool_calls || []) el.append(toolCard(c, results?.get(c.id)));
    const u = usageLine(m.usage);
    if (u) el.append(u);
    return el;
  }
  return null;
}

function nearBottom() { const b = $("#messages"); return b.scrollHeight - b.scrollTop - b.clientHeight < 120; }
function scrollToBottom(force = false) { const b = $("#messages"); if (force || nearBottom()) b.scrollTop = b.scrollHeight; }

function ensureStream() {
  if (S.streamEl) return;
  S.streamText = "";
  S.streamEl = h("div", { class: "msg assistant streaming" }, h("div", { class: "text" }), h("span", { class: "cursor" }));
  $("#messages").append(S.streamEl);
}

function updateStream() {
  if (!S.streamEl) return;
  let text = S.streamText;
  let thinking = "";
  const close = text.indexOf("</think>");
  if (close >= 0) { thinking = text.slice(0, close).replace("<think>", ""); text = text.slice(close + 8); }
  else if (text.trimStart().startsWith("<think>")) { thinking = text.replace("<think>", ""); text = ""; }
  text = text.replace(/<tool_call>[\s\S]*?(<\/tool_call>|$)/g, "\n[preparing tool call…]\n");
  const el = S.streamEl.querySelector(".text");
  el.innerHTML = (thinking && !text.trim() ? `<span class="muted">${escapeHtml(thinking.slice(-600))}</span>` : "") + renderMarkdown(text);
}

function removeStream() {
  if (S.streamEl) S.streamEl.remove();
  S.streamEl = null;
  S.streamText = "";
}

function renderTasks() {
  const panel = $("#tasks-panel");
  const chat = currentChat();
  panel.hidden = !chat || !S.tasks.length;
  const marks = { pending: "○", in_progress: "◐", completed: "●", blocked: "!" };
  $("#tasks").replaceChildren(...S.tasks.map((t) => h("li", { class: t.status }, h("span", { class: "mark" }, marks[t.status] || "○"), h("span", {}, t.content))));
}

// ---------- events ----------
function handleEvent(ev) {
  const isCurrent = ev.chat_id && ev.chat_id === S.currentChatId;
  switch (ev.type) {
    case "state_changed":
      refreshState();
      break;
    case "message": {
      if (!isCurrent) break;
      const m = ev.message;
      S.messages.push(m);
      if (m.role === "tool") {
        const card = $(`#messages [data-call="${CSS.escape(m.tool_call_id)}"]`);
        const call = S.messages.flatMap((x) => x.tool_calls || []).find((c) => c.id === m.tool_call_id);
        if (card && call) card.replaceWith(toolCard(call, m));
        else { const el = renderMessage(m); if (el) $("#messages").append(el); }
      } else {
        if (m.role === "assistant") removeStream();
        const el = renderMessage(m, new Map());
        if (el) $("#messages").append(el);
      }
      scrollToBottom();
      break;
    }
    case "generation_start":
      if (isCurrent) { ensureStream(); scrollToBottom(); }
      break;
    case "token":
      if (isCurrent) { ensureStream(); S.streamText += ev.text; updateStream(); scrollToBottom(); }
      break;
    case "status":
      S.chatState[ev.chat_id] = { state: ev.state, outcome: ev.outcome };
      if (ev.state === "idle") {
        S.running.delete(ev.chat_id);
        if (isCurrent) removeStream();
        if (ev.outcome === "needs_help" || ev.outcome === "waiting_user") {
          const c = S.chats.find((x) => x.id === ev.chat_id);
          if (!isCurrent && c) toast(`"${c.title}" is waiting for you`);
        }
      } else S.running.add(ev.chat_id);
      renderSidebar();
      renderHeader();
      break;
    case "tasks":
      if (isCurrent) { S.tasks = ev.tasks; renderTasks(); }
      break;
    case "usage":
      if (isCurrent) renderContextMeter(ev.usage);
      break;
    case "settings":
      S.settings = ev.settings;
      break;
    case "approval_request":
      S.approvals.set(ev.approval.id, ev.approval);
      renderSidebar(); renderHeader(); showNextApproval();
      break;
    case "approval_resolved":
      S.approvals.delete(ev.approval_id);
      renderSidebar(); renderHeader();
      if (S.shownApproval === ev.approval_id) { $("#modal").close(); S.shownApproval = null; }
      showNextApproval();
      break;
    case "model_status":
    case "resources":
      refreshStatus();
      break;
  }
}

let ws, wsDelay = 500;
function connect() {
  ws = new WebSocket(`${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/ws`);
  ws.onopen = async () => {
    wsDelay = 500;
    await refreshState();
    if (S.currentChatId) await selectChat(S.currentChatId);
    refreshStatus();
  };
  ws.onmessage = (e) => handleEvent(JSON.parse(e.data));
  ws.onclose = () => { setTimeout(connect, wsDelay); wsDelay = Math.min(wsDelay * 2, 8000); };
}

// ---------- status footer ----------
async function refreshStatus() {
  try {
    const st = await api("GET", "/api/status");
    const m = st.model;
    let text = `Model: ${m.model_id || "?"} — `;
    text += m.loading ? "loading…" : m.loaded ? (m.busy ? "generating" : "loaded") : "not loaded";
    if (m.last_error) text += " (last load failed)";
    const pl = m.placement || {};
    if (pl.offloaded) text += ` · ${pl.offloaded_pct}% offloaded to RAM (slower)`;
    $("#model-status").textContent = text;
    $("#model-status").classList.toggle("warn-text", !!pl.offloaded);
    $("#model-status").title = m.last_error || "";
    const r = st.resources;
    const bits = [];
    if (r.gpu_busy) bits.push("paused: GPU busy");
    if (r.nvml && r.other_mem_gb) bits.push(`other apps VRAM ${r.other_mem_gb} GB`);
    if (!r.nvml) bits.push("GPU monitor unavailable");
    $("#resource-status").textContent = bits.join(" · ");
  } catch {}
}

// ---------- actions ----------
async function newChat(projectId = null) {
  const chat = await api("POST", "/api/chats", { project_id: projectId });
  S.chats.unshift(chat);
  if (projectId) { S.collapsed[projectId] = false; store.set("collapsedProjects", S.collapsed); }
  await selectChat(chat.id);
}

async function send() {
  const input = $("#input");
  const text = input.value.trim();
  if (!text || send.busy) return;
  send.busy = true;
  try {
    if (!S.currentChatId) {
      // Landing page: start a new general chat. The server titles it from this first message.
      const chat = await api("POST", "/api/chats", { project_id: null });
      S.chats.unshift(chat);
      S.currentChatId = chat.id;
      store.set("currentChatId", chat.id);
      S.messages = [];
      S.tasks = [];
      renderSidebar();
      renderChat();
    }
    await api("POST", `/api/chats/${S.currentChatId}/send`, { text });
    input.value = "";
    S.running.add(S.currentChatId);
    renderHeader();
  } catch (e) { toast(e.message); }
  finally { send.busy = false; }
}

function openMenu(anchor, items) {
  const menu = $("#menu");
  menu.replaceChildren(...items.map((it) => it === "-" ? h("hr") : h("button", { class: it.danger ? "danger" : "", onclick: () => { menu.hidden = true; it.action(); } }, it.label)));
  const r = anchor.getBoundingClientRect();
  menu.hidden = false;
  menu.style.top = `${r.bottom + 4}px`;
  menu.style.left = `${Math.max(8, r.right - menu.offsetWidth)}px`;
}
document.addEventListener("click", (e) => { if (!e.target.closest("#menu") && !e.target.closest("#chat-menu")) $("#menu").hidden = true; });

async function renameChat() {
  const chat = currentChat();
  const title = prompt("Rename chat", chat.title);
  if (title && title.trim()) await api("PATCH", `/api/chats/${chat.id}`, { title: title.trim() });
}

function chatMenu() {
  const chat = currentChat();
  if (!chat) return;
  const items = [{ label: "Rename", action: renameChat }];
  const moves = S.projects.filter((p) => p.id !== chat.project_id).map((p) => ({
    label: `Move to ${p.name}`, action: () => api("PATCH", `/api/chats/${chat.id}`, { project_id: p.id }).catch((e) => toast(e.message)),
  }));
  if (chat.project_id) moves.unshift({ label: "Move to General", action: () => api("PATCH", `/api/chats/${chat.id}`, { move_to_general: true }) });
  if (moves.length) items.push("-", ...moves);
  items.push("-", {
    label: "Delete chat", danger: true, action: async () => {
      if (!confirm(`Delete "${chat.title}"? This cannot be undone.`)) return;
      try { await api("DELETE", `/api/chats/${chat.id}`); } catch (e) { toast(e.message); }
    },
  });
  openMenu($("#chat-menu"), items);
}

// ---------- dialogs ----------
function field(label, input, hint) {
  return h("div", { class: "field" }, h("label", {}, label), input, hint ? h("div", { class: "hint" }, hint) : null);
}

function openModal(...children) {
  const body = $("#modal-body");
  body.replaceChildren(...children);
  const dlg = $("#modal");
  if (!dlg.open) dlg.showModal();
  return body;
}

function showNextApproval() {
  const dlg = $("#modal");
  if (dlg.open) return;
  const approval = [...S.approvals.values()][0];
  if (!approval) return;
  S.shownApproval = approval.id;
  const chat = S.chats.find((c) => c.id === approval.chat_id);
  const project = chat && S.projects.find((p) => p.id === chat.project_id);
  const decide = async (decision) => {
    try { await api("POST", `/api/approvals/${approval.id}`, { decision }); }
    catch (e) { toast(e.message); }
    S.approvals.delete(approval.id);
    dlg.close();
    S.shownApproval = null;
    renderSidebar(); renderHeader();
    setTimeout(showNextApproval, 50);
  };
  openModal(
    h("h3", {}, `Approval needed: ${approval.summary}`),
    h("div", { class: "approval-meta" }, `Chat: ${chat ? chat.title : "unknown"} · ${project ? `Project: ${project.name}` : "General chats"}`),
    h("pre", { class: "approval-detail" }, approval.detail),
    h("div", { class: "modal-actions" },
      chat && chat.id !== S.currentChatId ? h("button", { type: "button", class: "btn ghost", onclick: () => selectChat(chat.id) }, "Go to chat") : null,
      h("span", { class: "spacer" }),
      h("button", { type: "button", class: "btn danger", onclick: () => decide("deny") }, "Deny"),
      h("button", { type: "button", class: "btn", title: approval.keys.join("\n"), onclick: () => decide("always") }, project ? "Always allow in this project" : "Always allow in general chats"),
      h("button", { type: "button", class: "btn primary", onclick: () => decide("once") }, "Allow once")));
  dlg.oncancel = (e) => e.preventDefault(); // must choose
}

function projectDialog(project = null) {
  const name = h("input", { type: "text", value: project?.name || "" });
  const ws = h("input", { type: "text", value: project?.workspace_path || "", placeholder: "D:\\Projects\\my-project" });
  const env = h("input", { type: "text", value: project?.env_path || "", placeholder: "(use the default environment)" });
  const desc = h("textarea", { rows: 2 }, project?.description || "");
  const enabled = new Set(project?.toolsets || S.toolsets);
  const boxes = S.toolsets.map((t) => {
    const cb = h("input", { type: "checkbox", value: t });
    cb.checked = enabled.has(t);
    return h("label", { class: "check" }, cb, t);
  });
  const dlg = $("#modal");
  dlg.oncancel = null;
  openModal(
    h("h3", {}, project ? "Project settings" : "New project"),
    h("p", {}, "A project is a workspace folder with its own chats. The agent works inside this folder."),
    field("Name", name),
    field("Workspace folder", ws, "Absolute path. Created if it doesn't exist."),
    field("Python environment (optional)", env, "Folder containing python.exe. Leave empty to use the default from Settings."),
    field("Description", desc, "Shown to the agent as context."),
    field("Tools available to the agent", h("div", { class: "grid2" }, boxes)),
    h("div", { class: "modal-actions" },
      project ? h("button", {
        type: "button", class: "btn danger", onclick: async () => {
          if (!confirm(`Delete project "${project.name}" and its chats? Files in the workspace folder are not touched.`)) return;
          try { await api("DELETE", `/api/projects/${project.id}`); dlg.close(); } catch (e) { toast(e.message); }
        },
      }, "Delete project") : null,
      h("span", { class: "spacer" }),
      h("button", { type: "button", class: "btn ghost", onclick: () => dlg.close() }, "Cancel"),
      h("button", {
        type: "button", class: "btn primary", onclick: async () => {
          const body = {
            name: name.value.trim(), workspace_path: ws.value.trim(), env_path: env.value.trim() || null,
            description: desc.value.trim(), toolsets: boxes.map((b) => b.querySelector("input")).filter((i) => i.checked).map((i) => i.value),
          };
          try {
            if (project) await api("PATCH", `/api/projects/${project.id}`, body);
            else { const p = await api("POST", "/api/projects", body); await refreshState(); await newChat(p.id); }
            dlg.close();
          } catch (e) { toast(e.message); }
        },
      }, project ? "Save" : "Create project")));
}

// ---------- model / generation settings ----------
async function loadProfile(modelId) {
  const q = modelId ? `?model_id=${encodeURIComponent(modelId)}` : "";
  return api("GET", `/api/model/profile${q}`);
}

const PRESET_FIELDS = ["thinking", "temperature", "top_p", "top_k", "min_p", "presence_penalty", "repetition_penalty"];

// Editor for the generation settings, shared by the app Settings and per-chat settings.
// `values` holds the current settings; `allowDefault` adds a "use app default" choice (per-chat).
function generationEditor(values, profile, { allowDefault = false, defaultLabel = "" } = {}) {
  const docs = profile.docs;
  const inputs = {};
  const numField = (key, label, step) => {
    const [lo, hi] = profile.limits[key] || [];
    inputs[key] = h("input", { type: "number", step, min: lo, max: hi, value: values[key] ?? "" });
    inputs[key].addEventListener("input", () => { presetSel.value = "custom"; showNote(); });
    return field(label, inputs[key], docs[key]);
  };
  const presetSel = h("select", {},
    allowDefault ? h("option", { value: "__default__" }, `Use app default${defaultLabel ? ` (${defaultLabel})` : ""}`) : null,
    Object.entries(profile.presets).map(([id, p]) => h("option", { value: id }, p.label)),
    h("option", { value: "custom" }, "Custom"));
  presetSel.value = values.__default__ ? "__default__" : (values.preset in profile.presets || values.preset === "custom") ? values.preset : "custom";
  const note = h("div", { class: "hint" });
  const showNote = () => {
    const p = profile.presets[presetSel.value];
    note.textContent = p ? p.note : presetSel.value === "__default__" ? "This chat follows the app-wide settings." : "Hand-tuned values.";
  };
  const thinking = h("input", { type: "checkbox" });
  thinking.checked = !!values.thinking;
  thinking.addEventListener("change", () => { presetSel.value = "custom"; showNote(); syncBudget(); });
  inputs.thinking = thinking;
  const fill = (v) => {
    thinking.checked = !!v.thinking;
    for (const k of PRESET_FIELDS.slice(1)) inputs[k].value = v[k];
    syncBudget();
  };
  presetSel.addEventListener("change", () => {
    const p = profile.presets[presetSel.value];
    if (p) fill(p);
    showNote();
  });

  const budgetField = numField("thinking_budget", "Reasoning budget (tokens, 0 = no limit)", "256");
  const syncBudget = () => { inputs.thinking_budget.disabled = !thinking.checked; };
  const el = h("div", { class: "gen-editor" },
    h("div", { class: "grid2" },
      field("Preset", presetSel, docs.preset), h("div", { class: "field" }, h("label", {}, " "), note)),
    h("div", { class: "grid2" },
      field("Reasoning (thinking)", h("label", { class: "check" }, thinking, "think before answering"), docs.thinking),
      budgetField,
      numField("temperature", "Temperature", "0.05"),
      numField("max_new_tokens", "Max tokens per reply", "256")),
    h("details", { class: "advanced" }, h("summary", {}, "Advanced sampling"),
      h("div", { class: "grid2" },
        numField("top_p", "Top-p", "0.05"),
        numField("top_k", "Top-k", "1"),
        numField("min_p", "Min-p", "0.01"),
        numField("presence_penalty", "Presence penalty", "0.1"),
        numField("repetition_penalty", "Repetition penalty", "0.01"))));
  showNote();
  syncBudget();
  return {
    el,
    useDefault: () => presetSel.value === "__default__",
    read: () => {
      const out = { preset: presetSel.value, thinking: thinking.checked };
      for (const k of ["thinking_budget", "temperature", "max_new_tokens", "top_p", "top_k", "min_p", "presence_penalty", "repetition_penalty"]) {
        out[k] = Number(inputs[k].value);
      }
      return out;
    },
  };
}

async function chatSettingsDialog() {
  const chat = currentChat();
  if (!chat) return;
  const [s, profile] = await Promise.all([api("GET", "/api/settings"), loadProfile()]);
  const overrides = chat.gen_overrides || {};
  const hasOverrides = Object.keys(overrides).length > 0;
  const values = hasOverrides ? { ...s, ...overrides } : { ...s, __default__: true };
  const editor = generationEditor(values, profile, { allowDefault: true, defaultLabel: presetLabel(s.preset, profile) });
  const dlg = $("#modal");
  dlg.oncancel = null;
  openModal(
    h("h3", {}, "Model settings for this chat"),
    h("p", {}, `Applies only to "${chat.title}". App-wide defaults are in Settings. Model: ${s.model_id}.`),
    editor.el,
    h("div", { class: "modal-actions" },
      h("span", { class: "spacer" }),
      h("button", { type: "button", class: "btn ghost", onclick: () => dlg.close() }, "Cancel"),
      h("button", {
        type: "button", class: "btn primary", onclick: async () => {
          try {
            const updated = await api("PATCH", `/api/chats/${chat.id}`, { gen_overrides: editor.useDefault() ? {} : editor.read() });
            Object.assign(chat, updated);
            dlg.close();
            renderHeader();
            toast("Chat settings saved");
          } catch (e) { toast(e.message); }
        },
      }, "Save")));
}

function presetLabel(id, profile) {
  return profile?.presets?.[id]?.label || (id === "custom" ? "Custom" : id || "Default");
}

function fmtTokens(n) { return n >= 1000 ? `${(n / 1000).toFixed(n >= 10000 ? 0 : 1)}k` : String(n); }

function usageLine(u) {
  if (!u) return null;
  const parts = [];
  if (u.thinking_tokens) parts.push(`thinking ${fmtTokens(u.thinking_tokens)}`);
  parts.push(`answer ${fmtTokens(u.answer_tokens ?? u.completion_tokens)} tokens`);
  if (u.tokens_per_s) parts.push(`${u.tokens_per_s} tok/s`);
  if (u.seconds) parts.push(`${u.seconds}s`);
  return h("div", { class: "usage", title: `Prompt ${u.prompt_tokens} tokens · completion ${u.completion_tokens} tokens` },
    parts.join(" · "), u.thinking_budget_hit ? h("span", { class: "budget-hit" }, " · reasoning budget reached") : null);
}

function latestUsage() {
  for (let i = S.messages.length - 1; i >= 0; i--) if (S.messages[i].usage) return S.messages[i].usage;
  return null;
}

async function settingsDialog() {
  const s = await api("GET", "/api/settings");
  const rules = await api("GET", "/api/approval_rules");
  let profile = await loadProfile(s.model_id);
  const inputs = {};
  const text = (key, obj = s) => (inputs[key] = h("input", { type: "text", value: obj[key] ?? "" }));
  const num = (key, obj = s, step = "1") => (inputs[key] = h("input", { type: "number", step, value: obj[key] ?? "" }));
  const check = (key, obj = s) => { const cb = h("input", { type: "checkbox" }); cb.checked = !!obj[key]; inputs[key] = cb; return cb; };
  const quant = h("select", {}, ["4bit", "8bit", "none"].map((q) => h("option", { value: q, selected: s.quantization === q }, q)));
  const r = s.resources;
  const dlg = $("#modal");
  dlg.oncancel = null;
  const scopeName = (scope) => scope === "general" ? "General chats" : (S.projects.find((p) => p.id === scope)?.name || scope);
  let editor = generationEditor(s, profile);
  const genWrap = h("div", {}, editor.el);
  const contextHint = h("div", { class: "hint" });
  const familyLine = h("div", { class: "hint" });
  const showProfile = () => {
    contextHint.textContent = `${profile.docs.context_tokens} This model supports up to ${fmtTokens(profile.context_max)}.`;
    familyLine.replaceChildren(`Recognized as: ${profile.family}. `,
      profile.source ? h("a", { href: profile.source, target: "_blank", rel: "noopener" }, "Model card") : "");
  };
  showProfile();
  const modelInput = text("model_id");
  modelInput.addEventListener("change", async () => {
    // A different model has different recommended settings; switch the editor to them.
    try {
      profile = await loadProfile(modelInput.value.trim());
      const preset = profile.presets[profile.default_preset];
      editor = generationEditor({ ...s, ...preset, preset: profile.default_preset }, profile);
      genWrap.replaceChildren(editor.el);
      showProfile();
      toast(`Applied recommended settings for ${profile.family}`);
    } catch (e) { toast(e.message); }
  });
  openModal(
    h("h3", {}, "Settings"),
    h("div", { class: "fieldset-title" }, "Model"),
    h("div", { class: "grid2" },
      field("Model (Hugging Face id or local folder)", modelInput, familyLine),
      field("Quantization", quant, profile.docs.quantization),
      field("Context window (tokens)", num("context_tokens", s, "1024"), contextHint),
      field("Tool-call format", (inputs.tool_call_format = h("select", {}, ["auto", "hermes", "qwen3_coder"].map((f) => h("option", { value: f, selected: s.tool_call_format === f }, f)))), profile.docs.tool_call_format),
      field("Models folder (download cache)", text("models_dir")),
      field("Default Python environment", text("env_path"))),
    h("div", { class: "fieldset-title" }, "Generation (app default)"),
    h("p", {}, "Each chat can override these from the ⚙ button in its header."),
    genWrap,
    h("div", { class: "fieldset-title" }, "Agent"),
    h("div", { class: "grid2" },
      field("Max steps per turn", num("max_steps")),
      field("Failures in a row before asking for help", num("max_consecutive_failures")),
      field("Default tool timeout (seconds)", num("tool_timeout_s"))),
    h("div", { class: "fieldset-title" }, "Resources"),
    h("div", { class: "grid2" },
      field("VRAM limit (GB)", num("max_vram_gb", r, "0.5")),
      field("GPU offload", (inputs.offload = h("select", {},
        h("option", { value: "auto", selected: r.offload !== "gpu_only" }, "Auto: use system RAM if it doesn't fit"),
        h("option", { value: "gpu_only", selected: r.offload === "gpu_only" }, "GPU only: fail if it doesn't fit"))),
        profile.docs.offload),
      field("System RAM for offloaded layers (GB)", num("max_cpu_ram_gb", r, "1"), "Only used in Auto mode."),
      field("CPU threads", num("cpu_threads", r)),
      field("Unload model after idle (minutes, 0 = never)", num("idle_unload_minutes", r, "1")),
      field("Pause when other apps use the GPU", h("label", { class: "check" }, check("pause_when_gpu_busy", r), "enabled")),
      field("…when their GPU use exceeds (%)", num("gpu_busy_util_pct", r)),
      field("…or their VRAM use exceeds (GB)", num("gpu_busy_mem_gb", r, "0.5")),
      field("Background hours for long tasks", text("background_hours", r), "e.g. 22-7. Empty = any time.")),
    h("div", { class: "fieldset-title" }, "Saved approvals"),
    rules.length ? h("div", {}, rules.map((rule) => h("div", { class: "rule-row" },
      h("span", {}, `${scopeName(rule.scope)} — ${rule.key}`),
      h("button", { type: "button", class: "icon-btn", title: "Remove", onclick: async (e) => { await api("DELETE", `/api/approval_rules?scope=${encodeURIComponent(rule.scope)}&key=${encodeURIComponent(rule.key)}`); e.target.closest(".rule-row").remove(); } }, "✕"))))
      : h("p", {}, "None. \"Always allow\" choices appear here."),
    h("div", { class: "modal-actions" },
      h("button", { type: "button", class: "btn", onclick: async () => { const x = await api("POST", "/api/model/unload"); toast(x.ok ? "Model unloaded" : "Model is busy"); refreshStatus(); } }, "Unload model now"),
      h("span", { class: "spacer" }),
      h("button", { type: "button", class: "btn ghost", onclick: () => dlg.close() }, "Cancel"),
      h("button", {
        type: "button", class: "btn primary", onclick: async () => {
          const val = (k) => inputs[k].type === "checkbox" ? inputs[k].checked : inputs[k].type === "number" ? Number(inputs[k].value) : inputs[k].value;
          const patch = { quantization: quant.value, resources: {}, ...editor.read() };
          for (const k of ["model_id", "models_dir", "env_path", "context_tokens", "tool_call_format", "max_steps", "max_consecutive_failures", "tool_timeout_s"]) patch[k] = val(k);
          for (const k of ["max_vram_gb", "offload", "max_cpu_ram_gb", "cpu_threads", "idle_unload_minutes", "pause_when_gpu_busy", "gpu_busy_util_pct", "gpu_busy_mem_gb", "background_hours"]) patch.resources[k] = val(k);
          try {
            S.settings = await api("PUT", "/api/settings", patch);
            S.profile = profile;
            dlg.close(); toast("Settings saved"); refreshStatus(); renderHeader();
          } catch (e) { toast(e.message); }
        },
      }, "Save")));
}

// ---------- wiring ----------
$("#new-chat").onclick = () => newChat(null);
$("#new-project").onclick = () => projectDialog(null);
$("#open-settings").onclick = settingsDialog;
$("#chat-menu").onclick = chatMenu;
$("#chat-gen").onclick = chatSettingsDialog;
loadProfile().then((p) => { S.profile = p; renderHeader(); }).catch(() => {});
$("#chat-title").ondblclick = renameChat;
$("#composer").onsubmit = (e) => { e.preventDefault(); send(); };
$("#stop").onclick = () => S.currentChatId && api("POST", `/api/chats/${S.currentChatId}/cancel`);
$("#input").addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); send(); }
});
$("#modal").addEventListener("close", () => { S.shownApproval = null; setTimeout(showNextApproval, 50); });

connect();
setInterval(refreshStatus, 5000);
